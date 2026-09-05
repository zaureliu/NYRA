"""Hardware presence gate: user claims never become physical observations."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def plain(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(c))


BOARD = re.compile(r'\b(?:esp32(?:[- ]s[23]|[- ]c[236])?|esp8266|arduino|rp2040|pico)\b')
PHYSICAL = re.compile(r'\b(?:led|gpio|heartbeat|serial|pinos? digitais?|sensor)\b')


class HardwareRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: str = Field(default='placa', pattern=r'^(?:placa|ESP32(?:[- ]S[23]|[- ]C[236])?|ESP8266|ARDUINO|RP2040|PICO)$')
    transport: Literal['usb_serial', 'network'] = 'usb_serial'
    action: Literal['inspect', 'physical_effect'] = 'inspect'
    source: Literal['user_claim'] = 'user_claim'


def hardware_request(text: str) -> HardwareRequest | None:
    value = plain(text)
    board = BOARD.search(value)
    effect = PHYSICAL.search(value)
    # Instructional questions remain instructional; they do not assert live state.
    if re.search(r'\b(?:como|explique|explica|tutorial|exemplo|codigo)\b', value) and not re.search(
        r'\b(?:conectei|conectado|liguei|agora|aqui|apareceu|confirma|verifica)\b', value
    ):
        return None
    live = re.search(r'\b(?:conect\w*|desconect\w*|liguei|apareceu|agora|aqui|detect\w*|'
                     r'acend\w*|aces[oa]|liga|ligue|ligar|pisca\w*|ativa\w*|desliga\w*|verifica\w*|confere|'
                     r'coloca\w*|comuta\w*|configura\w*|'
                     r'identifica\w*|funciona\w*|respond\w*|compila\w*|grav\w*|flash\w*)\b', value)
    referent = re.search(r'\b(?:placa|dispositivo|modulo|dele|dela|ele|ela|esse|essa)\b', value)
    if not live or not (board or effect or (referent and re.search(r'\b(?:usb|placa|dispositivo|modulo)\b', value))):
        return None
    return HardwareRequest(
        target=board.group().upper() if board else 'placa',
        transport='network' if re.search(r'\b(?:rede|wifi|wi-fi|ip|lan)\b', value)
        and not re.search(r'\b(?:usb|computador|pc|serial)\b', value) else 'usb_serial',
        action='physical_effect' if effect else 'inspect',
    )


UNKNOWN_RESPONSE = ('Não consegui verificar a presença do dispositivo agora. '
                    'Não tenho evidência para confirmar conexão, comunicação serial ou o estado do LED.')
UNVERIFIED_RESPONSE = ('Não tenho evidência física ou serial suficiente para confirmar '
                       'o estado do dispositivo, do LED ou dos pinos.')


def presence_reply(data: dict) -> str:
    """Deterministic presenter; no free-form tool message or requested effect is evidence."""
    if data.get('simulated') is True:
        return 'SIMULATED: estes dados são de simulação; não confirmam nenhum dispositivo físico ou efeito real.'
    if data.get('transport') == 'network':
        return ('Não tenho uma descoberta ou probe de rede que confirme esse dispositivo. '
                'USB/serial não comprova presença na rede nem o estado do LED.')
    if data.get('success') is not True:
        return UNKNOWN_RESPONSE
    devices = data.get('devices') or []
    if data.get('device_found') is False:
        target = data.get('target', 'placa')
        label = f'nenhum {target}' if target != 'placa' else 'a placa referenciada'
        suffix = (' Há uma interface serial, mas ela não identifica sozinha o chip ou a placa.'
                  if data.get('unidentified_serial_count') else '')
        return (f'Não encontrei {label} conectado agora na USB/serial.' + suffix
                + ' Quando ele aparecer, consigo identificar e continuar.')
    if len(devices) != 1:
        return 'Encontrei mais de uma placa compatível na USB. Preciso identificar qual delas você quer usar; não acionei nenhum LED.'
    item = devices[0]
    port = f" na {item['com_port']}" if item.get('com_port') else ''
    reply = (f"A descoberta USB identificou {item['name']}{port}. "
             'Isso não confirma comunicação serial ativa nem o estado dos pinos.')
    if data.get('action') == 'physical_effect':
        reply += (' Não acionei o LED: ainda não há um protocolo de controle e verificação física disponível para essa placa.')
    return reply


async def discover_hardware(service, request: HardwareRequest) -> dict:
    from app.core.turn import get_current_turn_id

    base = dict(request.model_dump(), request_source='user_claim', source='usb_discovery',
                turn_id=get_current_turn_id(), device_found=None, devices=[], success=False,
                simulated=False, effect_verified=False, status='BLOCKED',
                error_code='HARDWARE_DISCOVERY_UNAVAILABLE')
    if request.transport == 'network':
        return {**base, 'source': 'none', 'error_code': 'NETWORK_DEVICE_UNVERIFIED'}
    try:
        snapshot = await asyncio.wait_for(service.refresh(reason='hardware_grounding'), 10)
    except Exception:
        return base
    if snapshot.get('discovery_success') is not True:
        return base
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot['observed_at'])).total_seconds()
        if not 0 <= age <= 15:
            return {**base, 'error_code': 'STALE_DEVICE_OBSERVATION'}
    except (KeyError, ValueError, TypeError):
        return base
    # Unknown/test providers are never silently promoted to real hardware.
    simulated = (getattr(service.discovery, 'source', None) != 'windows_setupapi'
                 or getattr(service.discovery, 'simulated', True) is not False)
    rows = snapshot.get('observed_devices') or []
    simulated = simulated or any(item.get('metadata', {}).get('simulated') is True for item in rows)
    if simulated:
        return {**base, 'simulated': True, 'error_code': 'SIMULATED_HARDWARE'}
    observed_at = snapshot['observed_at']
    target = plain(request.target)
    reference_id = getattr(service, '_last_hardware_device_id', None) if target == 'placa' else None
    matches = []
    for item in rows:
        # Friendly names are operator labels, NOT chip identification.
        identity = plain(f"{item.get('name', '')} {item.get('product', '')}")
        identified = bool(BOARD.search(identity)) if target == 'placa' else bool(
            re.search(r'\b' + re.escape(target) + r'\b', identity))
        if reference_id:
            identified = item.get('device_id') == reference_id
        if identified and item.get('status') == 'CONNECTED' and item.get('device_instance_id'):
            matches.append({key: item.get(key) for key in
                            ('device_id', 'name', 'com_port', 'vid', 'pid', 'device_instance_id')})
    if len(matches) == 1:
        service._last_hardware_device_id = matches[0]['device_id']
    facts = [dict(kind='device_present', device_id=item['device_id'], value=True,
                  source='usb_discovery', transport='usb_serial', observed_at=observed_at,
                  turn_id=base['turn_id'], simulated=False) for item in matches]
    blocked = not matches or len(matches) != 1 or request.action == 'physical_effect'
    return {**base, 'success': True, 'observed_at': observed_at, 'device_found': bool(matches),
            'status': 'BLOCKED' if blocked else 'OBSERVED',
            'devices': matches, 'hardware_facts': facts,
            'unidentified_serial_count': sum(bool(item.get('com_port')) for item in rows if not any(
                match['device_id'] == item.get('device_id') for match in matches)),
            'error_code': ('HARDWARE_EFFECT_UNVERIFIED' if matches else 'DEVICE_NOT_FOUND') if blocked else None}


def register_hardware_tool(registry, service) -> None:
    from app.tools.models import RiskLevel
    from app.tools.registry import ToolDefinition

    async def discover(**kwargs):
        result = await discover_hardware(service, HardwareRequest(**kwargs))
        service.last_hardware_observation = result
        return result

    registry.register(ToolDefinition(
        'hardware_discover', 'Revalida presença USB/serial real. Não abre serial, não grava firmware e não aciona GPIO.',
        RiskLevel.READ_ONLY, HardwareRequest, discover,
    ))


# Positive physical assertions need narrowly typed facts from actual adapters.
# Shell stdout/exit 0, user text, generic ready/open and build/upload are excluded.
_CLAIMS = {
    'device_present': r'\b(?:esp32[\w-]*|esp8266|arduino|placa|dispositivo|modulo)\b.{0,75}\b(?:detectad[oa]|conectad[oa]|identificad[oa]|encontrad[oa])\b',
    'led_on': r'\bled\b.{0,55}\b(?:aceso|ligado|on)\b|\b(?:acendi|liguei)\b.{0,30}\bled\b',
    'led_blinking': r'\bled\b.{0,55}\b(?:piscando|pisca|blink\w*)\b',
    'heartbeat': r'\bheartbeat\b.{0,35}\b(?:ativo|funcionando|confirmado)\b|\b(?:padrao|em)\b.{0,25}\bheartbeat\b',
    'serial_active': r'\b(?:comunicacao|conexao)\b.{0,20}\bserial\b.{0,40}\b(?:ativa|estabelecida|funcionando|confirmada)\b',
    'gpio_state': r'\b(?:gpio\s*\d*|pinos? digitais?)\b.{0,55}\b(?:alto|baixo|operantes?|funcionando|ativos?)\b',
    'device_response': r'\b(?:dispositivo|placa|sensor|esp32[\w-]*)\b.{0,40}\b(?:respondeu|funcionando|funciona)\b',
}
_FACT_TOOLS = {
    'device_present': {'hardware_discover': {'usb_discovery'}, 'network_device_probe': {'network_probe'}},
    'led_on': {'gpio_readback': {'gpio_readback'}, 'firmware_ack': {'firmware_ack'}, 'hardware_vision': {'camera'}},
    'led_blinking': {'firmware_ack': {'firmware_telemetry'}, 'hardware_vision': {'camera'}},
    'heartbeat': {'firmware_ack': {'firmware_telemetry'}},
    'serial_active': {'serial_probe': {'serial_probe'}, 'firmware_ack': {'firmware_ack'}},
    'gpio_state': {'gpio_readback': {'gpio_readback'}, 'firmware_ack': {'firmware_ack'}},
    'device_response': {'serial_probe': {'serial_probe'}, 'firmware_ack': {'firmware_ack'}},
}


def unsupported_hardware_claims(draft: str, observations, turn_id: str | None = None) -> list[str]:
    claims = set()
    for clause in re.split(r'[.!?\n;]|\bmas\b', plain(draft)):
        def asserted(match):
            prefix = clause[:match.start()].rsplit(',', 1)[-1]
            local = prefix + match.group()
            return not re.search(r'\b(?:nao|nenhum|sem evidencia|se|pode|poderia|quando|para|precis\w*)\b', local)
        for kind, pattern in _CLAIMS.items():
            if any(asserted(match) for match in re.finditer(pattern, clause)):
                claims.add(kind)
        if any(asserted(match) for match in re.finditer(r'\b(?:encontrei|detectei|identifiquei)\b.{0,50}\b(?:esp32[\w-]*|placa|dispositivo)\b', clause)):
            claims.add('device_present')
    unsupported = []
    network_claim = bool(re.search(r'\b(?:rede|wifi|wi-fi|lan)\b', plain(draft)))
    for kind in claims:
        supported = False
        for observation in observations:
            allowed = _FACT_TOOLS[kind].get(observation.tool_name, set())
            if not observation.ok or observation.success is not True:
                continue
            for fact in observation.hardware_facts:
                try:
                    observed = datetime.fromisoformat(str(fact.get('observed_at')))
                    age = (datetime.now(timezone.utc) - observed).total_seconds()
                except (ValueError, TypeError):
                    continue
                if (fact.get('kind') == kind and fact.get('source') in allowed
                        and fact.get('simulated') is False and fact.get('value') is True
                        and fact.get('device_id') and 0 <= age <= 15
                        and (not turn_id or fact.get('turn_id') == turn_id)
                        and not (kind == 'device_present' and network_claim and fact.get('transport') != 'network')):
                    supported = True
        if not supported:
            unsupported.append(kind)
    return sorted(unsupported)
