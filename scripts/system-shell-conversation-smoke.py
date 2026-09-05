from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.brain import BrainManager
from app.character.context import ContextBuilder
from app.character.state import EmotionalState
from app.core.config import Settings
from app.core.logging import configure_logging
from app.events import EventBus, EventType
from app.memory import MemoryRepository
from app.tools import create_tool_registry
from app.tools.agent import ToolAgentLoop
from app.tools.system_shell import SystemShellService


PROMPTS = [
    "Kazumi, pinga o gateway.",
    "Kazumi, vê se o Proxmox está online.",
    "Kazumi, qual processo está usando a porta 5173?",
    "Kazumi, mostra minhas interfaces de rede.",
    "Kazumi, verifica o status do Git desse projeto.",
    "Kazumi, pinga o Proxmox.",
]


async def main() -> None:
    settings = Settings.from_sources()
    configure_logging(settings.log_level)
    bus = EventBus(history_size=500)
    shell = SystemShellService(settings, bus)
    await shell.initialize()
    registry = create_tool_registry(shell)
    llm = BrainManager(settings.ollama_url, settings.llm_model, settings.llm_timeout_seconds)
    results = []
    with tempfile.TemporaryDirectory(prefix="kazumi-shell-conversation-") as directory:
        memory = MemoryRepository(Path(directory) / "conversation.db")
        await memory.initialize()
        context = ContextBuilder(memory)
        selected_prompts = PROMPTS if len(sys.argv) == 1 else [PROMPTS[int(index)] for index in sys.argv[1:]]
        for prompt in selected_prompts:
            before = len(bus.history())
            messages = await context.build(
                prompt,
                EmotionalState.FOCUSED,
                "SYSTEM_SHELL_STATUS=" + json.dumps(shell.status(), ensure_ascii=False),
            )
            response = await ToolAgentLoop(llm, registry, settings.shell_max_calls_per_turn).run(messages)
            events = bus.history()[before:]
            results.append({
                "prompt": prompt,
                "response": response,
                "executions": [event.payload for event in events if event.type == EventType.SHELL_EXECUTION_FINISHED],
                "commands": [event.payload.get("command") for event in events if event.type == EventType.SHELL_EXECUTION_STARTED],
                "approval_required": [event.payload for event in events if event.type == EventType.SHELL_APPROVAL_REQUIRED],
            })
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
