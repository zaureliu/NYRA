"""Controlled tests are NOT evidence of connected hardware or physical effects."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.hardware_engine.adapters import PROFILES
from app.hardware_engine.build import BuildEngine
from app.hardware_engine.context import ProjectContext
from app.hardware_engine.engineering import CodeEngineering, CodeChange, EngineeringPlan, CodeReview, apply_candidate
from app.hardware_engine.models import HardwareError
from app.hardware_engine.planner import understand
from app.hardware_engine.projects import ProjectStore
from app.hardware_engine.replanning import PlanRevision, reconcile_target
from app.hardware_engine.service import HardwareEngineeringService
from app.hardware_engine.technical_profile import technical_profile, profile_reply
from app.web_research.models import Source
from test_hardware_grounding import service as usb_fixture
from test_hardware_engineering import services_fixture


@pytest.mark.asyncio
async def test_dependencies_are_provenanced_source_only_not_install_scripts():
    from app.hardware_engine.dependencies import LibraryImport, LibraryFile, review_import
    base = 'https://raw.githubusercontent.com/arduino-libraries/ArduinoHttpClient/master/'
    async def document(url):
        text = ('Permission is hereby granted, free of charge' if url.endswith('LICENSE') else
                'Supports Arduino-compatible boards.' if url.endswith('README.md') else 'int fixture_function() { return 1; }')
        return Source(url=url, text=text)
    request = LibraryImport(name='http_client', version='controlled-reference', license_url=base+'LICENSE',
        compatibility_url=base+'README.md', compatibility_support='Supports Arduino-compatible boards.',
        files=[LibraryFile(url=base+'src/Client.cpp', relative='Client.cpp')])
    files, record = await review_import(request, SimpleNamespace(document=document))
    assert 'src/http_client/Client.cpp' in files and record['source_hashes']
    assert 'src/http_client/LICENSE.txt' in files
    request.files[0].relative = 'install.py'
    with pytest.raises(HardwareError, match='DEPENDENCY_SOURCE_ONLY'):
        await review_import(request, SimpleNamespace(document=document))
    request.license_url = 'https://untrusted.example/LICENSE'
    with pytest.raises(HardwareError, match='DEPENDENCY_SOURCE_UNTRUSTED'):
        await review_import(request, SimpleNamespace(document=document))


@pytest.mark.parametrize('text', ['agora adiciona um botão', 'nesse projeto muda o delay', 'continua aquele projeto',
    'faz igual no código anterior', 'muda o delay', 'coloca um botão nele', 'quando eu apertar o botão, pisca três vezes',
    'agora adiciona um servidor Web mostrando o estado'])
def test_continuations_are_software_not_unobserved_physical_actions(text):
    assert understand(text, active_project=True).project_only


def test_half_second_and_no_device_claim_remain_distinct():
    assert understand('agora muda para piscar a cada meio segundo', active_project=True).interval_ms == 500
    claim = understand('conectei um ESP32 aqui, consegue acender o LED dele?', active_project=True)
    assert not claim.project_only and claim.source == 'user_claim'


def project(tmp_path):
    store = ProjectStore(tmp_path/'projects', tmp_path/'state')
    meta = store.create(PROFILES[0][1])
    store.write(meta['project_id'], 'src/main.cpp', '#include <Arduino.h>\nvoid setup() {}\nvoid loop() {}\n')
    meta['reference'] = True
    store.checkpoint(meta)
    return store, meta


def test_context_uses_artifacts_and_explicit_name_not_new_project(tmp_path):
    store, first = project(tmp_path)
    second = store.create(PROFILES[2][1])
    context = ProjectContext(store)
    assert context.resolve('abre esse código', artifact_paths=[store.path(first['project_id'])/'src/main.cpp'])['project_id'] == first['project_id']
    assert context.resolve(second['name'])['project_id'] == second['project_id']
    assert len(store.index) == 2
    restarted = ProjectStore(store.root, store.state_root)
    assert restarted.active == second['project_id']


def test_modified_code_resolves_through_actual_artifact_context(tmp_path):
    from app.computer.artifacts import RecentArtifactMemory, parse_artifact_request
    store, meta = project(tmp_path)
    file = store.path(meta['project_id'])/'src/main.cpp'
    artifacts = RecentArtifactMemory(persistence_path=tmp_path/'artifacts.json')
    artifacts.register(str(file), exists_state='verified', source_type='modified', source_tool='hardware_code_patch')
    request = parse_artifact_request('abre o código que você mexeu')
    assert request is not None
    result = artifacts.resolve(request, conversation_id='default')
    assert result is not None and result.path == str(file)


def test_plan_invalidation_prevents_old_build_or_flash_and_keeps_history():
    revision = PlanRevision(assumptions={'chip': 'esp32'})
    old = revision.revise({}, ['identify', 'build', 'flash'], reason='initial', source='planner')
    revision.enter('identify', old)
    revision.finish('identify', old)
    new = revision.revise({'chip': 'esp32s3'}, ['research.pinout', 'build'], reason='probe_changed_target', source='serial_chip_probe')
    with pytest.raises(HardwareError, match='PLAN_REVISION_INVALIDATED'):
        revision.enter('flash', old)
    assert revision.changes[-1]['invalidated_assumptions'] == ['chip']
    assert all(s.state == 'INVALIDATED' for s in revision.invalidated_steps)
    revision.enter('research.pinout', new)
    revision.finish('research.pinout', new)
    assert revision.steps[-1].name == 'build'


def test_observed_target_updates_same_workspace_and_invalidates_environment(tmp_path):
    store, meta = project(tmp_path)
    revision = PlanRevision(assumptions={'chip': 'atmega328p'})
    old = revision.revise({}, ['flash'], reason='initial', source='planner')
    target = PROFILES[2][1]
    assert reconcile_target(store, meta, {'source': 'usb_discovery', 'chip': target.chip, 'board': target.model_dump()}, revision)
    assert meta['project_id'] == store.active and len(store.index) == 1
    assert 'board = esp32-s3-devkitc-1' in store.inspect(store.active)['platformio.ini']
    assert meta['build'] == {} and meta['flash'] == {} and meta['libraries'] == []
    with pytest.raises(HardwareError):
        revision.enter('flash', old)


def test_user_claim_cannot_retarget_project_or_grant_execution(tmp_path):
    store, meta = project(tmp_path)
    with pytest.raises(HardwareError, match='UNOBSERVED_PLAN_EVIDENCE'):
        reconcile_target(store, meta, {'source': 'user_claim', 'chip': 'esp32s3'}, PlanRevision())


@pytest.mark.parametrize('path', ['../escape.cpp', 'platformio.ini', 'scripts/install.py', 'src/payload.exe'])
def test_model_cannot_add_host_commands_or_build_hooks(path):
    change = CodeChange(edits=[{'path': path, 'before': '', 'after': 'code'}], assertions=[{'path': path, 'contains': 'code'}])
    with pytest.raises(HardwareError, match='ENGINEERING_FILE_NOT_ALLOWED'):
        apply_candidate({}, change)


def test_patch_preconditions_and_preservation():
    source = {'src/main.cpp': 'void keepFeature() {}\nvoid loop() {}'}
    change = CodeChange(edits=[{'path': 'src/main.cpp', 'before': 'void keepFeature() {}', 'after': ''}],
                        assertions=[{'path': 'src/main.cpp', 'contains': 'void loop'}])
    with pytest.raises(HardwareError, match='EXISTING_FEATURE_REMOVED'):
        apply_candidate(source, change)


class ResearchFixture:
    last = {}
    async def research(self, request):
        return {'success': True, 'sources': [{'url': 'https://docs.arduino.cc/api-fixture', 'source_type': 'manufacturer'}]}
    async def document(self, url, **kw):
        if url.endswith('uno.json'):
            return Source(url=url, source_type='official_repository', text='{"name":"Arduino Uno", "vendor":"Arduino", "build":{"mcu":"atmega328p"}, "frameworks":["arduino"], "upload":{"maximum_ram_size":2048}}')
        return Source(url=url, source_type='manufacturer', text='CONTROLLED REFERENCE DOCUMENT: use millis for elapsed time. GPIO2 input pullup design fixture.')


@pytest.mark.asyncio
async def test_reference_profile_unknowns_and_natural_reply_are_not_device_presence():
    profile = await technical_profile({'board': PROFILES[0][1].model_dump()}, ResearchFixture(), reference=True)
    response = profile_reply(profile, 'me fala as informações dessa placa')
    assert response.startswith('REFERENCE') and not profile.connected
    assert profile.facts['mcu'].value == 'atmega328p'
    assert profile.facts['gpio_voltage'] is None and profile.facts['display'] is None
    assert 'Ainda não comprovados' in response and 'dispositivo conectado' in response


@pytest.mark.asyncio
async def test_reference_project_cannot_reach_flash(tmp_path):
    from app.hardware_engine.flash import FlashEngine
    store, meta = project(tmp_path)
    discovery = SimpleNamespace(resolve=AsyncMock())
    with pytest.raises(HardwareError, match='REFERENCE_PROJECT_NOT_PHYSICAL'):
        await FlashEngine(discovery, None, store, None).flash(meta)
    discovery.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_general_compiler_api_repair_revises_remaining_plan(tmp_path):
    store, meta = project(tmp_path)
    calls = []
    async def run(recipe, workspace):
        calls.append(recipe)
        if len(calls) == 1:
            return {'success': False, 'stdout': "src/main.cpp:3:1: error: 'obsoleteTimer' was not declared"}
        binary = workspace/'.pio/build/kazumi/firmware.hex'
        binary.parent.mkdir(parents=True)
        binary.write_text('SIMULATED, NOT PHYSICAL FIRMWARE')
        return {'success': True}
    async def proposer(schema, instruction, context):
        if schema == EngineeringPlan:
            return {'feature': 'API counter', 'changes': ['add counter'], 'queries': ['Arduino millis']}
        if schema == CodeReview:
            return {'request_implemented': True, 'previous_features_preserved': True}
        before, after = ('obsoleteTimer()', 'millis()') if 'diagnostics' in context else ('void loop() {}', 'void loop() { obsoleteTimer(); }')
        return {'edits': [{'path': 'src/main.cpp', 'before': before, 'after': after}],
                'assertions': [{'path': 'src/main.cpp', 'contains': 'void loop()'}]}
    engineering = CodeEngineering(store, ResearchFixture(), BuildEngine(store, SimpleNamespace(run=run)), None, proposer=proposer)
    result = await engineering.evolve(meta, understand('agora adiciona contador', active_project=True))
    assert result['build']['attempt'] == 2
    assert result['plan_revision']['revision'] == 2
    assert result['plan_revision']['changes'][-1]['source'] == 'compiler'
    assert 'obsoleteTimer' not in store.inspect(meta['project_id'])['src/main.cpp']
    assert len(result['plan_revision']['invalidated_steps']) >= 2


@pytest.mark.asyncio
async def test_three_turns_general_edits_same_workspace_memory_artifacts_and_blockers(tmp_path):
    usb, discovery = await usb_fixture(tmp_path/'usb')
    services = services_fixture(tmp_path, usb)
    services.computer = SimpleNamespace(artifacts=SimpleNamespace(items=[], register=lambda *a, **kw: registered.append(a[0])))
    registered = []
    engine = HardwareEngineeringService(services, tmp_path/'state', tmp_path/'projects')
    await engine.initialize()
    engine.configure(True)
    meta = engine.projects.create(PROFILES[0][1])
    meta.update(reference=True, hardware_context={'button': {'pin': 2, 'source': 'REFERENCE', 'physical_verified': False}})
    engine.projects.write(meta['project_id'], 'src/main.cpp', '#include <Arduino.h>\nvoid setup() {}\nvoid loop() {}\n')
    engine.projects.checkpoint(meta)
    async def run(recipe, workspace):
        assert recipe == 'build'
        binary = workspace/'.pio/build/kazumi/firmware.hex'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text('SIMULATED BUILD ARTIFACT')
        return {'success': True}
    engine.builder = BuildEngine(engine.projects, SimpleNamespace(run=run))
    engine.research = ResearchFixture()
    engine.toolchains = SimpleNamespace(select=AsyncMock(side_effect=lambda p: p), ensure=AsyncMock())
    async def proposer(schema, instruction, context):
        if schema == EngineeringPlan:
            return {'feature': context['request'], 'changes': ['extend existing source'], 'queries': ['Arduino millis']}
        if schema == CodeReview:
            return {'request_implemented': True, 'previous_features_preserved': True, 'issues': []}
        original = context['files']['src/main.cpp']
        if 'meio' in context['request']:
            before, after = 'intervalMs=2000', 'intervalMs=500'
        elif 'botão' in context['request']:
            before, after = 'void setup() {}', 'void setup() { pinMode(2, INPUT_PULLUP); }'
        else:
            before, after = 'void loop() {}', 'unsigned long intervalMs=2000;\nvoid loop() {}'
        return {'edits': [{'path': 'src/main.cpp', 'before': before, 'after': after}], 'source_urls': [],
                'assertions': [{'path': 'src/main.cpp', 'contains': after}]}
    engine.engineering = CodeEngineering(engine.projects, engine.research, engine.builder, None, proposer=proposer)
    services.intelligence.tasks.create = AsyncMock(return_value=SimpleNamespace(task_id='controlled-task'))
    for text in ('crie um projeto para piscar o LED', 'agora muda para piscar a cada meio segundo', 'agora adiciona um botão'):
        response = await engine.handle(text)
        assert 'mesmo projeto' in response
        goal = list(engine.goals.values())[-1]
        result = await engine.run(goal.goal_id)
        assert result['success'] and not result['physical_effect_verified']
        assert goal.target_project == meta['project_id']
    assert discovery.calls == 0 and len(engine.projects.index) == 1
    final = engine.projects.inspect(meta['project_id'])['src/main.cpp']
    assert 'intervalMs=500' in final and 'INPUT_PULLUP' in final
    assert services.intelligence.memory.write.call_count == 3 and len(registered) == 3
    persisted = engine.projects.read(meta['project_id'])
    assert len(persisted['completed_features']) == 3 and persisted['last_known_good_build']['success']
    # Missing sensor is a factual block and must not overwrite existing code.
    await engine.handle('agora adiciona um sensor')
    goal = list(engine.goals.values())[-1]
    result = await engine.run(goal.goal_id)
    assert not result['success'] and goal.errors[-1] == 'COMPONENT_EVIDENCE_REQUIRED'
    assert engine.projects.inspect(meta['project_id'])['src/main.cpp'] == final
    assert services.intelligence.open_loops.create.called
    # Stop the real fetcher replaced by this controlled fixture explicitly.
    engine.research = SimpleNamespace(close=AsyncMock())
    await engine.stop()
