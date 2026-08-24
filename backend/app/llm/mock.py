from app.llm.base import LLMMessage, LLMProvider


class MockLLMProvider(LLMProvider):
    def __init__(self, response: str = "Estou online e observando o homelab.") -> None:
        self.response = response

    @property
    def name(self) -> str:
        return "mock"

    async def health(self) -> bool:
        return True

    async def chat(self, messages: list[LLMMessage]) -> str:
        return self.response

