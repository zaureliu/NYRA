from __future__ import annotations

import re

from num2words import num2words


IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
CIDR = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b")
MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
VERSION = re.compile(r"\b(?:v(?:ers(?:ão|ao)?)?\s*)?\d+\.\d+(?:\.\d+)?\b", re.I)
PERCENT = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*%")
RATE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(Kbps|Mbps|Gbps|B/s|KB/s|MB/s|GB/s|KiB|MiB|GiB|TiB|KB|MB|GB|TB|GHz|MHz|ms|°C)\b", re.I)


def words(value: str) -> str:
    try:
        return str(num2words(float(value.replace(",", ".")) if "." in value or "," in value else int(value), lang="pt_BR"))
    except (ValueError, TypeError, NotImplementedError):
        return value


def normalize_numbers(text: str) -> tuple[str, list[dict]]:
    applied: list[dict] = []
    protected: dict[str, str] = {}

    def hold(match: re.Match) -> str:
        key = f"§ADDR{len(protected)}§"
        value = match.group(0)
        if "/" in value:
            ip, prefix = value.split("/", 1)
            spoken = f"{', '.join(words(part) for part in ip.split('.'))}, prefixo {words(prefix)}"
        elif ":" in value:
            spoken = " ".join(words(str(int(part, 16))) for part in value.split(":"))
        else:
            spoken = ", ".join(words(part) for part in value.split('.'))
        protected[key] = spoken
        applied.append({"term": value, "strategy": "technical_address", "spoken_form": spoken})
        return key

    text = CIDR.sub(hold, text)
    text = MAC.sub(hold, text)
    text = IPV4.sub(hold, text)

    def clock(match: re.Match) -> str:
        hour = int(match.group(1))
        minute = int(match.group(2))
        hour_unit = "hora" if hour == 1 else "horas"
        if minute == 0:
            spoken = f"{words(str(hour))} {hour_unit}"
        else:
            minute_unit = "minuto" if minute == 1 else "minutos"
            spoken = f"{words(str(hour))} {hour_unit} e {words(str(minute))} {minute_unit}"
        applied.append({"term": match.group(0), "strategy": "clock_time", "spoken_form": spoken})
        return spoken

    text = TIME.sub(clock, text)

    def rate(match: re.Match) -> str:
        number, unit = match.groups()
        unit_map = {"mbps": "megabits por segundo", "gbps": "gigabits por segundo", "kbps": "quilobits por segundo", "mb/s": "megabytes por segundo", "gb/s": "gigabytes por segundo", "kb/s": "quilobytes por segundo", "b/s": "bytes por segundo", "kib": "quibibytes", "mib": "mebibytes", "gib": "gibibytes", "tib": "tebibytes", "kb": "quilobytes", "mb": "megabytes", "gb": "gigabytes", "tb": "terabytes", "ghz": "gigahertz", "mhz": "megahertz", "ms": "milissegundos", "°c": "graus Celsius"}
        spoken_unit = unit_map.get(unit.casefold(), unit)
        if number.replace(',', '.').strip() in {'1', '1.0', '1,0'}:
            spoken_unit = spoken_unit.replace('gigabits', 'gigabit').replace('megabits', 'megabit').replace('quilobits', 'quilobit').replace('gigabytes', 'gigabyte').replace('megabytes', 'megabyte').replace('quilobytes', 'quilobyte').replace('quibibytes', 'quibibyte').replace('mebibytes', 'mebibyte').replace('gibibytes', 'gibibyte').replace('tebibytes', 'tebibyte').replace('terabytes', 'terabyte').replace('milissegundos', 'milissegundo')
        spoken = f"{words(number)} {spoken_unit}"
        applied.append({"term": match.group(0), "strategy": "technical_number", "spoken_form": spoken})
        return spoken

    text = RATE.sub(rate, text)
    text = PERCENT.sub(lambda m: f"{words(m.group(1))} por cento", text)
    text = VERSION.sub(lambda m: f"versão {m.group(0).replace('v', '').replace('V', '').replace('.', ' ponto ')}", text)
    for key, value in protected.items():
        text = text.replace(key, value)
    return text, applied
