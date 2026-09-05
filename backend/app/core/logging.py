from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from pythonjsonlogger.json import JsonFormatter

from app.core.paths import LOG_ROOT, ensure_runtime_directories


LOGGERS = {
    "kazumi": "application.log",
    "kazumi.conversation": "conversation.log",
    "kazumi.tools": "tools.log",
    "kazumi.homelab": "homelab.log",
    "kazumi.errors": "errors.log",
    "kazumi.voice": "voice.log",
    "kazumi.microphone": "microphone.log",
    "kazumi.desktop": "desktop.log",
    "kazumi.listening": "listening.log",
    "kazumi.network_watch": "network-watch.log",
    "kazumi.shell": "shell.log",
    "kazumi.remote_shell": "remote-shell.log",
    "kazumi.agent": "agent-runs.log",
    "kazumi.ollama_warm": "ollama-warm.log",
}


def configure_logging(level: str = "INFO") -> None:
    ensure_runtime_directories()
    formatter = JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    for logger_name, filename in LOGGERS.items():
        logger = logging.getLogger(logger_name)
        logger.setLevel(numeric_level)
        logger.propagate = False
        if logger.handlers:
            continue
        handler = RotatingFileHandler(
            LOG_ROOT / filename,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        if logger_name == "kazumi":
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            logger.addHandler(console)
