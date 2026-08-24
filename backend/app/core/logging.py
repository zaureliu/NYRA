from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from pythonjsonlogger.json import JsonFormatter

from app.core.paths import LOG_ROOT, ensure_runtime_directories


LOGGERS = {
    "nyra": "application.log",
    "nyra.conversation": "conversation.log",
    "nyra.tools": "tools.log",
    "nyra.homelab": "homelab.log",
    "nyra.errors": "errors.log",
    "nyra.voice": "voice.log",
    "nyra.microphone": "microphone.log",
    "nyra.desktop": "desktop.log",
    "nyra.listening": "listening.log",
    "nyra.network_watch": "network-watch.log",
    "nyra.shell": "shell.log",
    "nyra.remote_shell": "remote-shell.log",
    "nyra.agent": "agent-runs.log",
    "nyra.ollama_warm": "ollama-warm.log",
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
        if logger_name == "nyra":
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            logger.addHandler(console)
