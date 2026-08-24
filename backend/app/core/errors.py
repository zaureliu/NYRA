"""Safe error envelope for frontend consumption (spec §18, §216-§217).

Backend may log full stack traces; the frontend only ever receives:

    {"error_code": str, "safe_message": str, "stage": str, "recoverable": bool}

Unhandled exceptions get converted by the FastAPI handler registered in
app.main. HTTPException payloads keep their existing shapes for backwards
compatibility.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("nyra.errors")

_STAGE_HINTS = {
    "ollama": "llm",
    "llm": "llm",
    "brain": "llm",
    "tts": "tts",
    "speech": "tts",
    "stt": "stt",
    "transcribe": "stt",
    "shell": "shell",
    "remote_shell": "remote_shell",
    "ssh": "remote_shell",
    "homelab": "homelab",
    "proxmox": "homelab",
    "sentinel": "sentinel",
    "workflow": "workflow",
    "job": "jobs",
    "task": "tasks",
    "desktop": "desktop",
    "browser": "browser",
    "voice": "voice",
}


class SafeError(Exception):
    """Error carrying a frontend-safe payload."""

    def __init__(self, error_code: str, safe_message: str, *,
                 stage: str = "backend", recoverable: bool = True,
                 status_code: int = 500) -> None:
        super().__init__(safe_message)
        self.error_code = error_code
        self.safe_message = safe_message
        self.stage = stage
        self.recoverable = recoverable
        self.status_code = status_code

    def as_dict(self) -> dict:
        return {
            "error_code": self.error_code,
            "safe_message": self.safe_message,
            "stage": self.stage,
            "recoverable": self.recoverable,
        }


def infer_stage(error: BaseException) -> str:
    text = f"{type(error).__module__}.{type(error).__name__}".casefold()
    message = str(error).casefold()
    for token, stage in _STAGE_HINTS.items():
        if token in text or token in message[:160]:
            return stage
    return "backend"


def safe_envelope(error: BaseException, *, stage: str | None = None,
                  recoverable: bool = True) -> dict:
    return {
        "error_code": "INTERNAL_ERROR",
        "safe_message": "Falha interna temporária. Tente novamente; se persistir, verifique o Health Report.",
        "stage": stage or infer_stage(error),
        "recoverable": recoverable,
    }


async def unhandled_exception_handler(request: Request, error: Exception) -> JSONResponse:
    logger.exception(
        "unhandled_error path=%s method=%s error_type=%s",
        request.url.path, request.method, type(error).__name__,
    )
    return JSONResponse(status_code=500, content=safe_envelope(error))
