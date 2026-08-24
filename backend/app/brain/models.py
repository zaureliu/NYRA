from pydantic import BaseModel, Field


class BrainSelectionRequest(BaseModel):
    model: str = Field(pattern=r"^qwen3(?:\.5)?:[a-zA-Z0-9._-]+$")
    confirmed: bool = False


class BrainBenchmarkRequest(BaseModel):
    models: list[str] = Field(default_factory=lambda: ["qwen3:8b", "qwen3.5:9b"], min_length=1, max_length=4)
    context_size: int = Field(default=8192, ge=2048, le=16384)

