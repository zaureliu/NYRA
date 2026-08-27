"""Functional TTS smoke test used by setup validation and troubleshooting."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.speech.tts import create_tts_provider  # noqa: E402


async def main() -> None:
    settings = Settings.from_sources()
    provider = await create_tts_provider(
        settings.tts_provider,
        settings.tts_language,
        settings.tts_model_path,
        settings.tts_voices_path,
        settings.tts_voice,
    )
    print(f"PROVIDER={provider.name}")
    print(f"HEALTH={await provider.health()}")
    output = await provider.synthesize(
        "Nyra está online. Os serviços estão estáveis, por enquanto.", "focused"
    )
    print(f"AUDIO={output}")
    print(f"BYTES={output.stat().st_size}")


if __name__ == "__main__":
    asyncio.run(main())
