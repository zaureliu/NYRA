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

from app.agent import AgentController
from app.brain import BrainManager
from app.character.context import ContextBuilder
from app.character.state import EmotionalState
from app.core.config import Settings
from app.events import EventBus, EventType
from app.memory import MemoryRepository
from app.tools import RemoteShellService, SystemShellService, create_tool_registry


PROMPTS = [
    "Nyra, verifica o Proxmox.",
    "Nyra, vê se o OpenWrt está saudável.",
    "Nyra, verifica por que o backend da NYRA não está respondendo e tenta recuperar, mas não faça mudança sem policy ou aprovação.",
]


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nyra-agent-conversation-") as directory:
        # Conversational smoke is deliberately read-only; ACT/VERIFY mutation is
        # covered with controlled fakes in the unit suite.
        settings = Settings.from_sources(
            database_path=Path(directory) / "conversation.db",
            agent_read_only=True,
        )
        bus = EventBus(history_size=1000)
        memory = MemoryRepository(settings.database_path, bus)
        await memory.initialize()
        shell = SystemShellService(settings, bus)
        await shell.initialize()
        remote = RemoteShellService(settings, bus, shell.approvals)
        await remote.initialize()
        tools = create_tool_registry(shell, remote)
        llm = BrainManager(settings.ollama_url, settings.llm_model, settings.llm_timeout_seconds)
        agent = AgentController(settings, bus, llm, tools)
        await agent.initialize()
        context = ContextBuilder(memory)
        selected = PROMPTS if len(sys.argv) == 1 else [PROMPTS[int(index)] for index in sys.argv[1:]]
        results = []
        for prompt in selected:
            before = len(bus.history())
            runtime = {
                "system_shell": shell.status(),
                "trusted_remote_shell": remote.status(),
                "agent_policy": agent.status(),
            }
            messages = await context.build(prompt, EmotionalState.FOCUSED, json.dumps(runtime, ensure_ascii=False))
            response = await agent.run(messages, prompt)
            events = bus.history()[before:]
            run_events = [event for event in events if event.type in {
                EventType.AGENT_RUN_STARTED, EventType.AGENT_RUN_STATE_CHANGED,
                EventType.AGENT_RUN_STEP, EventType.AGENT_RUN_FINISHED,
            }]
            results.append({
                "prompt": prompt,
                "response": response,
                "agent": [{"type": event.type.value, **event.payload} for event in run_events],
                "local_commands": [event.payload.get("command") for event in events if event.type == EventType.SHELL_EXECUTION_STARTED],
                "remote_commands": [
                    {"host": event.payload.get("host"), "command": event.payload.get("command")}
                    for event in events if event.type == EventType.REMOTE_SHELL_EXECUTION_STARTED
                ],
            })
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
