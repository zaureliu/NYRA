"""Typed intent -> capability plan; raw model prose cannot become execution."""
import re

from app.usb.hardware import plain, hardware_request
from .models import GoalIntent


def understand(text: str, *, active_project=False) -> GoalIntent | None:
    value = plain(text)
    board = re.search(r'\b(?:esp32(?:[- ]?(?:s[23]|c[236]))?|esp8266|arduino|rp2040|stm32|nrf52|lilygo|m5stack|cardputer)\b', value)
    referent = re.search(r'\b(?:essa|esse|dessa|desse|aquela|aquele|nesse|neste)\s+(?:placa|dispositivo|projeto|display|sensor|led|botao)|\b(?:placa|firmware|chip|gpio|onboard|ram|psram)\b|\bqual\s+(?:display|sensor|led)\b', value)
    # A temporal adverb is not a request to edit the active project. Commands
    # such as "agora adiciona..." still match their actual action verb.
    continuation = active_project and re.search(r'\b(?:continua|adiciona|compila|altera|muda|coloca|codigo anterior|nesse projeto|quando eu)\b', value)
    project_request = re.search(r'\b(?:crie|cria|criar)\b.*\bprojeto\b', value)
    if not (hardware_request(text) or board or referent or continuation or project_request):
        return None
    if re.search(r'\b(?:como funciona|o que e um|me explica)\b', value) and not re.search(r'\b(?:pesquisa|procura|conect|essa|aqui)\w*\b', value):
        return None
    effect = 'modify' if continuation else 'info'
    for pattern, action in [
        (r'\b(?:pesquis|documenta|datasheet|pinout|gpio controla)\w*', 'research'),
        (r'\b(?:cria|crie)\b.*\bprojeto\b', 'project'),
        (r'\b(?:continua|retoma)\w*', 'resume'),
        (r'\bcompil\w*|\bbuild\b', 'build'),
        (r'\b(?:acend|liga|ligue|ativ)\w*.*\bled\b|\bled\b.*\b(?:acend|ligar|ativ)\w*', 'led_on'),
        (r'\b(?:desliga|apaga)\w*.*\bled\b', 'led_off'),
        (r'\bpisca\w*', 'led_blink'),
        (r'\bbotao\b', 'button'),
        (r'\b(?:display|tela)\b', 'display'),
        (r'\bsensor\b', 'sensor'),
        (r'\b(?:servidor|interface)\s+web\b', 'web_server'),
    ]:
        if re.search(pattern, value):
            effect = action
    interval = re.search(r'\b(\d+|dois|duas|um|uma|tres|cinco)\s*(segundos?|ms|milissegundos?)\b', value)
    milliseconds = 2000
    if interval:
        amount = int(interval[1]) if interval[1].isdigit() else {'dois': 2, 'duas': 2, 'um': 1, 'uma': 1, 'tres': 3, 'cinco': 5}[interval[1]]
        milliseconds = amount * (1000 if interval[2].startswith('seg') else 1)
    if 'meio segundo' in value:
        milliseconds = 500
    if re.match(r'\s*(?:qual\b|quanto\b|o que\b|me fala\b|me diz\b)', value):
        effect = 'info'
    physical_claim = re.search(r'\b(?:conectei|conectado|pluguei|na usb|no pc)\b', value)
    project_only = bool(project_request or continuation or re.search(r'\b(?:projeto|codigo|firmware)\b', value)) and not physical_claim
    target = board.group().upper() if board else ''
    return GoalIntent(effect=effect, target=target, interval_ms=max(100, min(60000, milliseconds)), text=text[:1000], project_only=project_only)


def plan(intent: GoalIntent, *, runtime_capable=False):
    if intent.project_only:
        return ['project.resolve', 'project.inspect', 'research', 'code.plan', 'code.edit', 'build', 'verify.source']
    if intent.effect == 'resume':
        return ['project.resume', 'discover', 'identify']
    prefix = ['discover', 'identify']
    if intent.effect == 'info':
        return prefix
    if intent.effect == 'research':
        return prefix + ['web.research']
    if runtime_capable and intent.effect in ('led_on', 'led_off', 'led_blink'):
        return prefix + ['serial.control', 'verify']
    return prefix + ['serial.capabilities', 'web.research', 'toolchain.select', 'project.create_or_resume',
                     'code.generate', 'build', 'backup', 'flash', 'reconnect', 'verify']
