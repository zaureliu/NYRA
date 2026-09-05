"""Same-sample metrics. No recording or transcript is persisted here."""
import re
import unicodedata


PHRASES = [
    "Ô mano, conectei um ESP32 aqui no computador. Consegue ver qual apareceu e acender o LED dele?",
    "Abre o Proxmox e verifica se a máquina virtual do Home Assistant está ligada.",
    "Esse LILYGO usa um ESP32-S3 e um rádio SX1262 de 915 megahertz.",
    "Coloca o GPIO 48 em nível alto e depois volta para nível baixo.",
    "Abre o VS Code naquele projeto e compila usando PlatformIO.",
    "Meu OpenWrt está respondendo, mas o DNS parece estar instável.",
    "Pô, essa porra travou de novo, mano. Espera aí... agora foi.",
    "Três ponto três volts, porta oito mil, cento e quinze mil duzentos baud.",
]
TERMS = ["NYRA", "ESP32", "ESP32-S3", "Proxmox", "OpenWrt", "LILYGO", "PlatformIO", "GPIO", "Home Assistant", "SX1262"]


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return " ".join(re.sub(r"[^\w\s]", " ", "".join(c for c in text if not unicodedata.combining(c))).split())


def distance(reference, actual) -> int:
    previous = list(range(len(actual) + 1))
    for i, expected in enumerate(reference, 1):
        current = [i]
        for j, word in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (expected != word)))
        previous = current
    return previous[-1]


def score(reference: str, actual: str) -> dict:
    expected, recognized = normalized(reference), normalized(actual)
    expected_words, actual_words = expected.split(), recognized.split()
    terms = [term for term in TERMS if normalized(term) in expected]
    matched = [term for term in terms if normalized(term) in recognized]
    return {
        "wer": round(distance(expected_words, actual_words) / len(expected_words), 4) if expected_words else None,
        "cer": round(distance(expected, recognized) / len(expected), 4) if expected else None,
        "technical_terms": {"expected": terms, "matched": matched,
                            "accuracy": len(matched) / len(terms) if terms else None},
    }
