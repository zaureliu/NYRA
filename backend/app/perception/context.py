from __future__ import annotations

import json
import re

from app.perception.models import PerceptionSnapshot


class ContextSelector:
    """Selects only perception fields relevant to the current request."""

    def select(self, text: str, snapshot: PerceptionSnapshot) -> str:
        if not snapshot.enabled:
            return ""
        value: dict = {"source": "Local PC Awareness"}
        normalized = text.casefold()
        if re.search(r"\b(o que estou|aplicativo|programa|janela|vs code|browser|terminal)\b", normalized):
            value["foreground_app"] = {
                "classification": snapshot.foreground_app.classification,
                "process": snapshot.foreground_app.process,
            }
        if re.search(r"\b(cpu|ram|mem[oó]ria|disco|carga|computador|pc|sistema)\b", normalized):
            value["system"] = snapshot.system.model_dump(mode="json", exclude={"audio_output_active"})
        if re.search(r"\b(ocioso|idle|ausente|voltei|atividade)\b", normalized):
            value["user_activity"] = snapshot.user_activity
            value["idle_seconds"] = snapshot.idle_seconds
        if len(value) == 1:
            return ""
        return json.dumps(value, ensure_ascii=False)
