from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.events import EventBus
from app.hardware_engine.adapters import PROFILES, identify_descriptor
from app.hardware_engine.build import BuildEngine
from app.hardware_engine.code import diagnostics, generate, repair
from app.hardware_engine.discovery import HardwareDiscovery
from app.hardware_engine.execution import quote, RecipeExecutor
from app.hardware_engine.flash import FlashEngine
from app.hardware_engine.models import GoalIntent, HardwareError, now
from app.hardware_engine.planner import understand, plan
from app.hardware_engine.projects import ProjectStore
from app.hardware_engine.service import HardwareEngineeringService
from app.world_state import WorldStateEngine
from test_hardware_grounding import service as usb_fixture, board


def profile():
    return PROFILES[0][1].model_copy(deep=True, update={'led_pin': 13, 'led_source': 'https://docs.arduino.cc/controlled-fixture'})


@pytest.mark.parametrize('text,effect', [
    ('Conectei um ESP32. Faça acender um LED nele.', 'led_on'),
    ('Faça esse LED piscar a cada dois segundos.', 'led_blink'),
    ('Descobre o que é essa placa.', 'info'),
    ('Crie um projeto para esse dispositivo.', 'project'),
    ('Mostre Olá na tela dessa placa.', 'display'),
    ('Faça esse sensor funcionar.', 'sensor'),
    ('Crie um servidor Web nesse ESP32 mostrando a temperatura.', 'web_server'),
    ('continua aquele projeto do ESP32', 'resume'),
    ('Agora adiciona um botão.', 'button'),
])
def test_natural_intents_are_typed_not_shell(text, effect):
    intent = understand(text, active_project=True)
    assert intent.effect == effect and intent.source == 'user_claim'
    assert isinstance(plan(intent), list)


def test_fast_path_and_bounded_interval():
    intent = understand('Faça esse LED piscar a cada dois segundos.')
    assert intent.interval_ms == 2000
    steps = plan(intent, runtime_capable=True)
    assert steps == ['discover', 'identify', 'serial.control', 'verify']
    assert 'flash' not in steps


@pytest.mark.asyncio
async def test_real_authority_required_and_device_revalidated(tmp_path):
    usb, discovery = await usb_fixture(tmp_path, [board()])
    engine = HardwareDiscovery(usb)
    device = await engine.resolve('ESP32')
    assert device['source'] == 'usb_discovery'
    discovery.rows = []
    with pytest.raises(HardwareError, match='DEVICE_NOT_FOUND'):
        await engine.resolve(device_id=device['device_id'])
    assert discovery.calls == 2


@pytest.mark.asyncio
async def test_no_device_and_simulation_cannot_become_presence(tmp_path):
    usb, discovery = await usb_fixture(tmp_path)
    engine = HardwareDiscovery(usb)
    with pytest.raises(HardwareError, match='DEVICE_NOT_FOUND'):
        await engine.resolve('ESP32')
    discovery.simulated = True
    with pytest.raises(HardwareError, match='SIMULATED_HARDWARE'):
        await engine.refresh()


def test_chip_is_not_board_and_friendly_alias_is_not_chip():
    chip = identify_descriptor({'device_id': 'fixture', 'name': 'ESP32-S3'})
    assert chip['chip'] == 'esp32s3' and chip['board'] is None
    serial = identify_descriptor({'device_id': 'fixture', 'name': 'CP2102 USB Serial', 'friendly_name': 'LILYGO'})
    assert serial['board'] is None and serial['chip'] is None


def test_project_create_resume_filesystem_hash_and_escape(tmp_path):
    store = ProjectStore(tmp_path / 'projects', tmp_path / 'state')
    meta = store.create(profile())
    code = generate(profile(), GoalIntent(effect='led_blink'))
    store.write(meta['project_id'], 'src/main.cpp', code)
    store.checkpoint(meta)
    store.validate_build_inputs(meta)
    resumed = ProjectStore(tmp_path / 'projects', tmp_path / 'state')
    assert resumed.active == meta['project_id'] and resumed.path(resumed.active) == store.path(meta['project_id'])
    with pytest.raises(HardwareError, match='PROJECT_FILE_NOT_ALLOWED'):
        store.write(meta['project_id'], '../escape.exe', 'no')
    with pytest.raises(HardwareError, match='PROJECT_CONTENT_REJECTED'):
        store.write(meta['project_id'], 'README.md', 'api_key=sk-' + 'x' * 40)
    store.write(meta['project_id'], 'platformio.ini', 'extra_scripts = malicious.py')
    with pytest.raises(HardwareError, match='SOURCE_CHANGED'):
        store.validate_build_inputs(meta)


def test_code_cannot_guess_undocumented_gpio_or_missing_button():
    with pytest.raises(HardwareError, match='LED_PIN_OR_DRIVER_UNVERIFIED'):
        generate(PROFILES[0][1], GoalIntent(effect='led_on'))
    with pytest.raises(HardwareError, match='BUTTON_WIRING_UNVERIFIED'):
        generate(profile(), GoalIntent(effect='button'))


def test_diagnostics_repair_only_reviewed_faults():
    text = 'src/main.cpp:3:1: error: expected ; before } token'
    findings = diagnostics(text)
    fixed, reason = repair('void setup() {\n Serial.begin(115200)\n}\n', findings, profile())
    assert 'Serial.begin(115200);' in fixed and reason
    assert repair('arbitrary broken source', findings, profile()) == (None, None)


@pytest.mark.asyncio
async def test_build_repair_uses_same_project_and_real_artifact_hash(tmp_path):
    store = ProjectStore(tmp_path / 'projects', tmp_path / 'state')
    meta = store.create(profile())
    store.write(meta['project_id'], 'src/main.cpp', 'void setup() {\n Serial.begin(115200)\n}\n')
    store.checkpoint(meta)
    calls = []
    async def run(recipe, workspace):
        calls.append(workspace)
        if len(calls) == 1:
            return {'success': False, 'stdout': 'src/main.cpp:3:1: error: expected ; before } token'}
        artifact = workspace / '.pio/build/kazumi/firmware.hex'
        artifact.parent.mkdir(parents=True)
        artifact.write_text('CONTROLLED SIMULATED FIRMWARE')
        return {'success': True}
    builder = BuildEngine(store, SimpleNamespace(run=run))
    result = await builder.build(meta)
    assert result['success'] and result['attempt'] == 2
    assert calls[0] == calls[1] and len(result['artifacts'][0]['sha256']) == 64


@pytest.mark.asyncio
async def test_build_exit_zero_without_artifact_is_not_success(tmp_path):
    store = ProjectStore(tmp_path / 'projects', tmp_path / 'state')
    meta = store.create(profile())
    store.write(meta['project_id'], 'src/main.cpp', generate(profile(), GoalIntent(effect='project')))
    store.checkpoint(meta)
    builder = BuildEngine(store, SimpleNamespace(run=AsyncMock(return_value={'success': True})))
    with pytest.raises(HardwareError, match='BUILD_ERROR'):
        await builder.build(meta)


def services_fixture(tmp_path, usb):
    intelligence = SimpleNamespace(
        tasks=SimpleNamespace(register=lambda *a, **kw: None),
        capabilities=SimpleNamespace(register=lambda *a, **kw: None),
        open_loops=SimpleNamespace(create=AsyncMock()), memory=SimpleNamespace(write=AsyncMock()))
    return SimpleNamespace(usb=usb, shell=SimpleNamespace(), world_state=WorldStateEngine(EventBus(), persistence_path=tmp_path/'world.json'),
                           intelligence=intelligence, event_bus=usb.event_bus, computer=None)


@pytest.mark.asyncio
async def test_goal_exact_no_device_no_build_no_flash_and_restart(tmp_path):
    usb, discovery = await usb_fixture(tmp_path / 'usb')
    services = services_fixture(tmp_path, usb)
    engine = HardwareEngineeringService(services, tmp_path/'engine', tmp_path/'projects')
    await engine.initialize()
    response = await engine.handle('Conectei um ESP32. Faça acender um LED nele.')
    assert 'Não encontrei nenhum ESP32' in response
    assert discovery.calls == 1 and len(engine.goals) == 1
    assert not (tmp_path/'projects').exists()
    assert services.intelligence.open_loops.create.called
    engine.configure(True)
    await engine.stop()
    restarted = HardwareEngineeringService(services, tmp_path/'engine', tmp_path/'projects')
    await restarted.initialize()
    assert restarted.full and len(restarted.goals) == 1 and not restarted.serial.handles
    await restarted.stop()


@pytest.mark.asyncio
async def test_full_off_and_quotes_cannot_grant_arbitrary_execution(tmp_path):
    executor = RecipeExecutor(SimpleNamespace(), tmp_path/'tools', tmp_path/'projects', full=False)
    with pytest.raises(HardwareError, match='HARDWARE_AUTONOMY_DISABLED'):
        await executor.run('build', workspace=tmp_path/'projects/x')
    assert quote("abc'; shell") == "'abc''; shell'"
    with pytest.raises(HardwareError, match='INVALID_TOOL_ARGUMENT'):
        quote('bad\ncommand')
