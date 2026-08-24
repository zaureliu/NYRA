from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.mock import MockLLMProvider
from app.llm.ollama import OllamaProvider


def create_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockLLMProvider()
    return OllamaProvider(
        base_url=settings.ollama_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        context_size=settings.ollama_context_size,
        keep_alive=settings.ollama_keep_alive,
    )
