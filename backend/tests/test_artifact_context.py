"""Regressão targeted do contexto de arquivos/logs/artefatos."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

from app.computer import (
    ArtifactContextService,
    ComputerAutonomyService,
    ComputerStateService,
    EffectVerificationService,
    IntentUnderstandingService,
    RecentArtifactMemory,
    SkillMemoryService,
    UsageLearningService,
)
from app.computer.artifacts import (
    extract_artifact_paths,
    parse_artifact_request,
)
from app.core.turn import reset_current_turn_id, set_current_turn_id
from app.events import EventBus
from app.tools.models import RiskLevel
from app.tools.registry import ToolDefinition, ToolRegistry


class FakeDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.last_operation_result: dict | None = None

    async def handle_universal(self, intent, *, turn_id=None):
        self.calls.append((intent.action.value, intent.target))
        self.last_operation_result = {
            "success": True,
            "execution_success": True,
            "effect_verified": True,
            "action": intent.action.value,
            "app": intent.target,
            "windows": [{
                "hwnd": 7, "pid": 77, "title": intent.target,
                "process_name": "fake.exe",
            }],
        }
        if intent.action.value == "OPEN_FILE":
            return True, f"Arquivo {Path(intent.target).name} aberto."
        if intent.action.value == "OPEN_FOLDER":
            return True, f"Pasta {Path(intent.target).name} aberta."
        return True, f"Aberto: {intent.target}."

    async def open_url(self, url: str):
        self.calls.append(("OPEN_URL", url))
        self.last_operation_result = {
            "success": True, "execution_success": True,
            "effect_verified": True,
        }
        return self.last_operation_result


class FakeRemoteHosts:
    def public_remote_hosts(self):
        return [{"id": "proxmox", "enabled": True}]

    def find_remote_in_text(self, text: str):
        if "proxmox" in text.casefold():
            return SimpleNamespace(id="proxmox")
        return None


class FakeRemoteShell:
    def __init__(self) -> None:
        self.hosts = FakeRemoteHosts()
        self.calls: list[dict] = []
        self.missing = False

    async def execute(self, **payload):
        self.calls.append(payload)
        if self.missing:
            return {
                "success": False,
                "stderr": "tail: cannot open: No such file or directory",
                "error_code": "SSH_COMMAND_FAILED",
            }
        return {
            "success": True,
            "host": payload["host"],
            "stdout": "linha-98\nlinha-99\nlinha-100\n",
            "stderr": "",
            "effect_verified": None,
        }


def build_pipeline(tmp_path):
    desktop = FakeDesktop()
    remote = FakeRemoteShell()
    state = ComputerStateService(base_dir=tmp_path / "state")
    memory = RecentArtifactMemory()
    artifacts = ArtifactContextService(
        memory=memory, desktop=desktop, remote_shell=remote, state=state,
    )
    pipeline = ComputerAutonomyService(
        state=state,
        intent_service=IntentUnderstandingService(state),
        perception=SimpleNamespace(metrics={}, event_bus=EventBus()),
        verifier=EffectVerificationService(),
        usage=UsageLearningService(base_dir=tmp_path / "usage"),
        skills=SkillMemoryService(base_dir=tmp_path / "skills"),
        desktop=desktop,
        remote_shell=remote,
        artifacts=artifacts,
    )
    return pipeline, desktop, remote


def test_parser_normalizes_context_and_literal_paths():
    now = parse_artifact_request("abra esse log, agora.")
    assert now is not None
    assert now.action == "OPEN_ARTIFACT"
    assert now.reference == "abra esse log"
    assert now.wanted_kind == "log"

    generated = parse_artifact_request("abra o log que você gerou")
    pronoun = parse_artifact_request("abre ele")
    tail = parse_artifact_request("mostra as últimas linhas dele")
    remote = parse_artifact_request("abre /var/log/syslog")
    windows = parse_artifact_request(r'abra "C:\Temp\teste.log"')
    assert generated is not None and generated.wanted_kind == "log"
    assert pronoun is not None and pronoun.wanted_kind is None
    assert tail is not None and tail.action == "TAIL_ARTIFACT"
    assert remote is not None and remote.explicit_path == "/var/log/syslog"
    assert windows is not None and windows.explicit_path == r"C:\Temp\teste.log"
    assert extract_artifact_paths(r"Salvei em C:\Temp\foo.log agora.") == [
        r"C:\Temp\foo.log",
    ]


def test_apps_and_known_folders_are_not_claimed_as_artifacts():
    assert parse_artifact_request("abre o discord") is None
    assert parse_artifact_request("abre o spotify") is None
    assert parse_artifact_request("abre o code") is None
    assert parse_artifact_request("abre downloads") is None


async def test_remote_log_reference_chain_bypasses_app_and_agent(tmp_path):
    pipeline, desktop, remote = build_pipeline(tmp_path)
    pipeline.artifacts.register(
        "/var/log/test.log",
        kind="log",
        host_scope="remote",
        host_id="proxmox",
        conversation_id="release",
        source_turn_id="turn_generated",
        exists_state="created",
        source_type="tool_result",
        source_tool="log_generator",
    )

    path = await pipeline.handle_user_request(
        "qual é o caminho exato?",
        conversation_id="release",
        turn_id="turn_path",
    )
    assert path.handled
    assert path.reply == "proxmox:/var/log/test.log"
    assert remote.calls == []

    for phrase in (
        "abra esse log, agora.",
        "abra o log que você gerou",
        "abre ele",
        "mostra as últimas linhas dele",
    ):
        result = await pipeline.handle_user_request(
            phrase,
            conversation_id="release",
            turn_id=f"turn_{len(remote.calls)}",
        )
        assert result.handled is True
        assert result.target == "/var/log/test.log"
        assert result.verified is True
        assert "aplicativo" not in result.reply.casefold()
        assert result.metrics["app_resolver_called"] == 0
        assert result.metrics["agent_run_calls"] == 0
        assert remote.calls[-1]["host"] == "proxmox"
        assert remote.calls[-1]["command"] == (
            "tail -n 100 -- /var/log/test.log"
        )

    assert desktop.calls == []
    assert len(remote.calls) == 4
    assert pipeline.artifacts.metrics["app_resolver_called_for_artifact"] == 0
    assert pipeline.artifacts.metrics["agent_run_calls"] == 0


async def test_missing_remote_artifact_does_not_fall_into_app_resolver(tmp_path):
    pipeline, desktop, remote = build_pipeline(tmp_path)
    pipeline.artifacts.register(
        "/var/log/removed.log",
        kind="log", host_scope="remote", host_id="proxmox",
        conversation_id="missing", exists_state="verified",
        source_type="tool_result",
    )
    remote.missing = True
    result = await pipeline.handle_user_request(
        "abre ele", conversation_id="missing", turn_id="turn_missing",
    )
    assert result.handled and result.verified is False
    assert "não existe mais" in result.reply
    assert "aplicativo" not in result.reply.casefold()
    assert desktop.calls == []


async def test_local_file_open_and_tail_are_grounded(tmp_path):
    pipeline, desktop, remote = build_pipeline(tmp_path)
    local_log = tmp_path / "foo.log"
    local_log.write_text(
        "\n".join(f"linha-{index}" for index in range(1, 151)),
        encoding="utf-8",
    )
    pipeline.artifacts.register(
        str(local_log), kind="log", conversation_id="local",
        source_turn_id="turn_create", exists_state="verified",
        source_type="operator_created",
    )
    opened = await pipeline.handle_user_request(
        "abre esse arquivo", conversation_id="local", turn_id="turn_open",
    )
    assert opened.handled and opened.verified is True
    assert desktop.calls == [("OPEN_FILE", str(local_log))]
    assert remote.calls == []

    shown = await pipeline.handle_user_request(
        "mostra as últimas linhas dele",
        conversation_id="local",
        turn_id="turn_tail",
    )
    assert shown.handled and shown.verified is True
    assert "linha-150" in shown.reply
    assert "linha-1\n" not in shown.reply
    assert desktop.calls == [("OPEN_FILE", str(local_log))]


async def test_local_missing_artifact_returns_missing_not_app(tmp_path):
    pipeline, desktop, _remote = build_pipeline(tmp_path)
    missing = tmp_path / "gone.log"
    pipeline.artifacts.register(
        str(missing), kind="log", conversation_id="local-missing",
        exists_state="created", source_type="operator_created",
    )
    result = await pipeline.handle_user_request(
        "abra esse log",
        conversation_id="local-missing",
        turn_id="turn_gone",
    )
    assert result.handled and result.verified is False
    assert "não existe mais" in result.reply
    assert desktop.calls == []


async def test_explicit_posix_path_uses_single_registered_host(tmp_path):
    pipeline, desktop, remote = build_pipeline(tmp_path)
    result = await pipeline.handle_user_request(
        "abre /var/log/syslog",
        conversation_id="literal",
        turn_id="turn_literal",
    )
    assert result.handled and result.verified is True
    assert result.target == "/var/log/syslog"
    assert len(remote.calls) == 1
    assert remote.calls[0]["host"] == "proxmox"
    assert desktop.calls == []


async def test_discord_and_downloads_keep_normal_routing(tmp_path):
    pipeline, desktop, remote = build_pipeline(tmp_path)
    discord = await pipeline.handle_user_request(
        "abre o discord", conversation_id="apps", turn_id="turn_discord",
    )
    downloads = await pipeline.handle_user_request(
        "abre downloads", conversation_id="apps", turn_id="turn_downloads",
    )
    assert discord.handled and discord.intent_action == "OPEN_APP"
    assert downloads.handled and downloads.intent_action == "OPEN_FOLDER"
    assert desktop.calls == [
        ("OPEN_APP", "discord"),
        ("OPEN_FOLDER", "downloads"),
    ]
    assert remote.calls == []


async def test_parent_reference_does_not_steal_file_focus(tmp_path):
    pipeline, desktop, _remote = build_pipeline(tmp_path)
    report = tmp_path / "reports" / "report.md"
    report.parent.mkdir()
    report.write_text("relatório", encoding="utf-8")
    pipeline.artifacts.register(
        str(report), kind="report", conversation_id="chain",
        exists_state="verified", source_type="operator_created",
    )
    parent = await pipeline.handle_user_request(
        "mostra a pasta dele",
        conversation_id="chain",
        turn_id="turn_parent",
    )
    assert parent.handled
    assert desktop.calls[-1] == ("OPEN_FOLDER", str(report.parent))
    resolved = pipeline.artifacts.memory.resolve(
        parse_artifact_request("abre ele"),
        conversation_id="chain",
        turn_id="turn_after_parent",
    )
    assert resolved is not None and resolved.path == str(report)


def test_recent_memory_persists_metadata_only_and_is_bounded(tmp_path):
    context_path = tmp_path / "recent-artifacts.json"
    memory = RecentArtifactMemory(
        max_items=20, persistence_path=context_path,
    )
    for index in range(30):
        memory.register(
            str(tmp_path / f"file-{index}.log"),
            conversation_id="bounded", exists_state="created",
            source_type="tool_result",
        )
    assert len(memory.items) == 20
    assert memory.persist() is True
    raw = context_path.read_text(encoding="utf-8")
    assert "conteúdo-do-log" not in raw
    assert '"path"' in raw and '"source_turn_id"' in raw
    restored = RecentArtifactMemory(
        max_items=20, persistence_path=context_path,
    )
    assert len(restored.items) == 20
    assert restored.items[-1].display_name == "file-29.log"


class NoInput(BaseModel):
    pass


async def test_structured_tool_result_registers_real_artifact(tmp_path):
    output = tmp_path / "diagnostic.log"
    output.write_text("grounded", encoding="utf-8")

    async def generate_log():
        return {
            "success": True,
            "path": str(output),
            "message": f"salvo em {output}",
        }

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        "diagnostic_log_generator",
        "Gera log de diagnóstico.",
        RiskLevel.LOW_RISK,
        NoInput,
        generate_log,
    ))
    memory = RecentArtifactMemory()
    context = ArtifactContextService(memory=memory)
    context.note_turn("tool-conversation", "turn_tool")
    registry.add_result_observer(context.observe_tool_result)
    token = set_current_turn_id("turn_tool")
    try:
        result = await registry.execute("diagnostic_log_generator", {})
    finally:
        reset_current_turn_id(token)
    assert result.ok
    assert len(memory.items) == 1
    artifact = memory.items[0]
    assert artifact.path == str(output)
    assert artifact.conversation_id == "tool-conversation"
    assert artifact.source_turn_id == "turn_tool"
    assert artifact.source_tool == "diagnostic_log_generator"
    assert artifact.exists_state == "verified"


def test_assistant_only_remote_claim_is_not_verified():
    context = ArtifactContextService(memory=RecentArtifactMemory())
    context.observe_assistant_response(
        "Gerei /var/log/kazumi/current.log.",
        conversation_id="claims",
        turn_id="turn_claim",
        grounded=False,
    )
    artifact = context.memory.items[0]
    assert artifact.path == "/var/log/kazumi/current.log"
    assert artifact.exists_state == "planned"
    assert artifact.source_type == "assistant_mention"


def test_remote_tool_host_is_kept_when_path_appears_only_in_response():
    memory = RecentArtifactMemory()
    context = ArtifactContextService(memory=memory)
    context.note_turn("host-context", "turn_host")
    context.observe_tool_result(
        "remote_shell",
        {"host": "proxmox", "command": "true"},
        {
            "ok": True,
            "data": {
                "success": True,
                "host": "proxmox",
                "stdout": "ok",
            },
        },
        "turn_host",
    )
    context.observe_assistant_response(
        "O arquivo está em /var/log/kazumi/current.log.",
        conversation_id="host-context",
        turn_id="turn_host",
        grounded=True,
    )
    artifact = memory.items[0]
    assert artifact.host_scope == "remote"
    assert artifact.host_id == "proxmox"
