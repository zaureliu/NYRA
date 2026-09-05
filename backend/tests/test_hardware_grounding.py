"""Targeted regressions. Fixtures here are NOT real-device validation."""
from datetime import datetime, timedelta, timezone

import pytest

from app.events import EventBus
from app.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.tools.agent import ToolAgentLoop
from app.tools.grounding import GroundingLedger
from app.tools.models import RiskLevel, ToolResult
from app.tools.registry import ToolRegistry, classify_domain
from app.usb.hardware import (hardware_request, discover_hardware, presence_reply,
                              register_hardware_tool, unsupported_hardware_claims)
from app.usb.models import UsbDeviceObservation, apply_fingerprint
from app.usb.registry import UsbDeviceRegistry
from app.usb.service import UsbDeviceService
from app.world_state import WorldStateEngine

BUG = 'Ô mano, conectei um ESP32 aqui no computador. Consegue ver qual apareceu e acender o LED dele?'
FAKE = ('Conexão estabelecida com sucesso. O dispositivo ESP32 foi detectado na rede local. '
        'O LED do módulo está aceso e piscando em padrão "heartbeat", confirmando que a '
        'comunicação serial está ativa e os pinos digitais estão operantes.')


class DiscoveryFixture:
    # Deliberately represents native-adapter output for unit tests only.
    source = 'windows_setupapi'
    simulated = False
    last_error = None

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = 0

    def enumerate(self):
        self.calls += 1
        return self.rows


def board(name='ESP32-S3', **metadata):
    return apply_fingerprint(UsbDeviceObservation(
        name=name, product=name, category='Serial', com_port='COM7',
        device_instance_id=r'USB\VID_ABCD&PID_0001\CONTROLLED_TEST', metadata=metadata,
    ))


async def service(tmp_path, rows=()):
    discovery = DiscoveryFixture(rows)
    registry = UsbDeviceRegistry(tmp_path / 'usb')
    await registry.initialize()
    return UsbDeviceService(EventBus(), registry=registry, discovery=discovery), discovery


@pytest.mark.parametrize('text', [BUG, 'o ESP32 está conectado', 'acende o LED dele',
                                   'a placa que conectei, verifica se o GPIO funciona',
                                   'Liga o LED dele', 'Coloca o GPIO 48 em nível alto',
                                   'o dispositivo está conectado', 'o LED está aceso'])
def test_hardware_claims_require_observation_not_conversation(text):
    request = hardware_request(text)
    assert request is not None and request.source == 'user_claim'
    assert ToolRegistry().should_route_to_agent(text)
    assert classify_domain(text) != 'CONVERSATION'
    assert UsbDeviceService.can_handle_chat(text)


@pytest.mark.parametrize('text', ['Como funciona um ESP32?', 'Me explica como acender um LED no Arduino',
                                   'Abre o Home Assistant', 'bom dia'])
def test_education_and_unrelated_domains_are_not_physical_requests(text):
    assert hardware_request(text) is None


@pytest.mark.asyncio
async def test_exact_bug_no_device_forces_fresh_discovery(tmp_path):
    usb, discovery = await service(tmp_path)
    reply = await usb.handle_chat(BUG)
    result = usb.last_hardware_observation
    assert discovery.calls == 1
    assert result['device_found'] is False
    assert result['source'] == 'usb_discovery' and result['request_source'] == 'user_claim'
    assert result['observed_at'] and result['status'] == 'BLOCKED'
    assert result['error_code'] == 'DEVICE_NOT_FOUND'
    assert result['effect_verified'] is False and not usb._connected
    assert usb._last_hardware_device_id is None
    assert 'Não encontrei nenhum ESP32 conectado agora' in reply
    assert unsupported_hardware_claims(reply, []) == []


@pytest.mark.asyncio
async def test_revalidate_after_disconnection_and_never_reuse_old_com(tmp_path):
    usb, discovery = await service(tmp_path, [board()])
    found = await discover_hardware(usb, hardware_request(BUG))
    assert found['device_found'] is True and len(found['hardware_facts']) == 1
    assert found['effect_verified'] is False
    assert 'Não acionei o LED' in presence_reply(found)
    discovery.rows = []
    absent = await discover_hardware(usb, hardware_request('acende o LED dele'))
    assert absent['device_found'] is False and absent['devices'] == []
    assert discovery.calls == 2 and 'COM7' not in presence_reply(absent)


@pytest.mark.asyncio
async def test_failed_refresh_is_unknown_not_absent_or_cached_present(tmp_path):
    usb, discovery = await service(tmp_path, [board()])
    await discover_hardware(usb, hardware_request(BUG))
    discovery.last_error = 'CONTROLLED_ACCESS_DENIED'
    result = await discover_hardware(usb, hardware_request(BUG))
    assert result['device_found'] is None and result['success'] is False
    assert 'Não consegui verificar' in presence_reply(result)


@pytest.mark.asyncio
async def test_friendly_name_and_generic_serial_bridge_do_not_identify_chip(tmp_path):
    usb, _ = await service(tmp_path, [board('USB Serial Adapter')])
    await usb.refresh()
    await usb.update_device(next(iter(usb._connected)), {'friendly_name': 'ESP32', 'registered': True})
    result = await discover_hardware(usb, hardware_request(BUG))
    assert result['device_found'] is False and result['unidentified_serial_count'] == 1
    assert 'não identifica sozinha' in presence_reply(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_simulated, row_simulated', [(True, False), (False, True)])
async def test_simulated_results_never_become_physical_success(tmp_path, provider_simulated, row_simulated):
    usb, discovery = await service(tmp_path, [board(simulated=row_simulated)])
    discovery.simulated = provider_simulated
    result = await discover_hardware(usb, hardware_request(BUG))
    assert result['device_found'] is None and not result['effect_verified']
    assert presence_reply(result).startswith('SIMULATED:')


@pytest.mark.asyncio
async def test_usb_is_not_network_discovery(tmp_path):
    usb, discovery = await service(tmp_path, [board()])
    result = await discover_hardware(usb, hardware_request('O ESP32 está conectado na rede'))
    assert result['device_found'] is None and result['success'] is False
    assert discovery.calls == 0 and 'probe de rede' in presence_reply(result)


@pytest.mark.asyncio
async def test_agent_entrypoint_forces_discovery_without_asking_model(tmp_path):
    class NoModel(LLMProvider):
        name = 'must-not-be-called'
        async def health(self): return True
        async def chat(self, messages): raise AssertionError('Unobserved hardware reached LLM')
        async def complete(self, messages, tools=None): raise AssertionError('Unobserved hardware reached planner')

    usb, discovery = await service(tmp_path)
    registry = ToolRegistry()
    register_hardware_tool(registry, usb)
    response = await ToolAgentLoop(NoModel(), registry).run([LLMMessage(role='user', content=BUG)], turn_id='turn_hw')
    assert discovery.calls == 1 and 'Não encontrei nenhum ESP32' in response


def observation(tool='system_shell', **data):
    ledger = GroundingLedger(turn_id='turn_hw')
    ledger.record(tool_call_id='call_hw', tool_name=tool, result_data={'success': True, **data})
    return ledger


@pytest.mark.parametrize('data', [
    {'stdout': 'Build PASS', 'exit_code': 0}, {'stdout': 'Upload success'},
    {'open': True}, {'ready': True, 'effect_verified': True}, {'stdout': FAKE},
])
def test_build_upload_open_port_and_echo_are_not_effect_evidence(data):
    ledger = observation(**data)
    claims = unsupported_hardware_claims(FAKE, ledger.observations, 'turn_hw')
    assert set(claims) >= {'device_present', 'led_on', 'led_blinking', 'heartbeat', 'serial_active', 'gpio_state'}
    assert unsupported_hardware_claims('Firmware compilado.', ledger.observations) == []


def test_negation_in_another_clause_does_not_hide_fake_effect():
    assert 'led_on' in unsupported_hardware_claims('Não houve erros, o LED está aceso.', [])
    assert 'led_on' in unsupported_hardware_claims('Não encontrei a placa, mas o LED está aceso.', [])
    assert unsupported_hardware_claims('Não confirmei se o LED está aceso.', []) == []


@pytest.mark.parametrize('age,source,simulated,turn,tool', [
    (60, 'firmware_ack', False, 'turn_hw', 'firmware_ack'),
    (0, 'user_claim', False, 'turn_hw', 'firmware_ack'),
    (0, 'firmware_ack', True, 'turn_hw', 'firmware_ack'),
    (0, 'firmware_ack', False, 'turn_old', 'firmware_ack'),
    (0, 'firmware_ack', False, 'turn_hw', 'system_shell'),
])
def test_effect_facts_require_trusted_adapter_freshness_turn_and_no_simulation(age,source,simulated,turn,tool):
    fact = dict(kind='led_on', value=True, device_id='usb_real', source=source, simulated=simulated,
                turn_id=turn, observed_at=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat())
    ledger = observation(tool, hardware_facts=[fact])
    assert unsupported_hardware_claims('O LED está aceso.', ledger.observations, 'turn_hw') == ['led_on']


@pytest.mark.asyncio
async def test_presenter_cannot_fill_missing_effects_even_if_model_repeats_fake_success():
    class BadPresenter(LLMProvider):
        name = 'bad-presenter'
        async def health(self): return True
        async def chat(self, messages): return FAKE
        async def complete(self, messages, tools=None): return LLMResponse(content=FAKE)

    result = ToolResult(tool='system_shell', risk=RiskLevel.READ_ONLY, ok=True,
                        data={'success': True, 'stdout': 'Build PASS', 'exit_code': 0}, elapsed_ms=1)
    reply = await ToolAgentLoop(BadPresenter(), ToolRegistry())._grounded_final([], FAKE, [result])
    assert reply != FAKE and unsupported_hardware_claims(reply, []) == []


@pytest.mark.asyncio
async def test_world_state_expires_usb_and_rejects_user_claim_refresh(tmp_path):
    now = [1000.0]
    world = WorldStateEngine(EventBus(), persistence_path=tmp_path / 'world.json', clock=lambda: now[0])
    assert world.ingest_usb_snapshot([board().model_dump()], source='windows_setupapi')
    assert world.get_snapshot()['connected_usb']['freshness'] == 'FRESH'
    now[0] += 46
    assert world.get_snapshot()['connected_usb']['freshness'] == 'STALE'
    assert not world.ingest_usb_snapshot([board().model_dump()], source='user_claim')
    now[0] += 46
    assert world.get_snapshot()['connected_usb']['freshness'] == 'EXPIRED'
    assert world.get_snapshot()['connected_usb']['value'] is None
    assert world.ingest_usb_snapshot([], source='windows_setupapi')
    assert world.get_snapshot()['connected_usb']['value'] == []


@pytest.mark.asyncio
async def test_realtime_entrypoint_never_streams_the_invented_hardware_response(tmp_path):
    from app.avatar import AvatarController
    from app.character import StateMachine
    from app.events import EventType
    from app.memory import MemoryRepository
    from app.memory.models import MemoryCreate, MemoryCategory
    from app.perception import PCAwareness
    from app.realtime.orchestrator import RealtimeOrchestrator
    from app.realtime.settings import V4SettingsManager
    from app.realtime.telemetry import RealtimeTelemetry
    from app.speech.queue import SpeechQueue
    from app.speech.voice_processor import VoiceProcessor
    from test_realtime_v4 import RecordingTTS

    class NoModel(LLMProvider):
        name = 'must-not-stream-hardware'
        async def health(self): return True
        async def chat(self, messages): raise AssertionError('Hardware escaped into conversation')
        async def stream(self, messages):
            raise AssertionError('Ungrounded token reached chat or speech')
            yield FAKE

    usb, discovery = await service(tmp_path)
    bus = usb.event_bus
    memory = MemoryRepository(tmp_path / 'memory.db', bus)
    await memory.initialize()
    # Even a previously hallucinated assistant message is not observed state.
    await memory.add(MemoryCreate(category=MemoryCategory.SHORT_TERM, role='assistant', content=FAKE))
    settings = V4SettingsManager(tmp_path / 'settings.json')
    speech = SpeechQueue()
    orchestrator = RealtimeOrchestrator(
        NoModel(), memory, StateMachine(memory, bus), bus, RecordingTTS(tmp_path), speech,
        settings_manager=settings, telemetry=RealtimeTelemetry(),
        perception=PCAwareness(bus, settings.value.realtime, settings.value.privacy),
        avatar=AvatarController(bus), voice_processor=VoiceProcessor(settings.value.voice_processor),
    )
    orchestrator.usb_devices = usb
    orchestrator.tools = ToolRegistry()
    register_hardware_tool(orchestrator.tools, usb)
    result = await orchestrator.converse(BUG, synthesize=False)
    assert discovery.calls == 1 and 'Não encontrei nenhum ESP32' in result.response
    tokens = ''.join(event.payload.get('delta', '') for event in bus.history() if event.type == EventType.LLM_TOKEN_RECEIVED)
    assert 'heartbeat' not in tokens and 'LED do módulo está aceso' not in tokens
    assert usb.last_hardware_observation['turn_id'] == result.turn_id

    # The adapter being unavailable is not permission to ask the model to guess.
    orchestrator.tools = ToolRegistry()
    result = await orchestrator.converse('o ESP32 está conectado', synthesize=False)
    assert 'Não consegui verificar' in result.response


@pytest.mark.asyncio
async def test_goal_controller_records_discovery_and_device_not_found(tmp_path):
    from app.agent import AgentController
    from app.core.config import Settings

    class NoModel(LLMProvider):
        name = 'not-used'
        async def health(self): return True
        async def chat(self, messages): raise AssertionError('Hardware planner must observe first')

    usb, discovery = await service(tmp_path)
    registry = ToolRegistry()
    register_hardware_tool(registry, usb)
    settings = Settings.from_sources(database_path=tmp_path / 'agents.db', agent_enabled=True)
    controller = AgentController(settings, usb.event_bus, NoModel(), registry)
    await controller.initialize()
    reply = await controller.run([LLMMessage(role='user', content=BUG)], BUG, turn_id='turn_goal_hw')
    run = (await controller.recent(1))[0]
    assert 'Não encontrei nenhum ESP32' in reply and discovery.calls == 1
    assert run.error == 'DEVICE_NOT_FOUND' and run.status != 'COMPLETED'
    assert run.tool_calls == 1
