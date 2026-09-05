from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.computer.state import ComputerStateService
from app.events import Event, EventBus, EventType
from app.intelligence.context import ContextEngine
from app.world_state import WorldStateEngine


class Clock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def event(clock: Clock, kind: EventType, **payload) -> Event:
    return Event(
        type=kind,
        timestamp=datetime.fromtimestamp(clock(), timezone.utc),
        payload=payload,
    )


async def make_engine(tmp_path, clock: Clock | None = None) -> WorldStateEngine:
    engine = WorldStateEngine(
        EventBus(),
        persistence_path=tmp_path / "world-state.json",
        clock=clock or Clock(),
    )
    await engine.start()
    return engine


async def test_foreground_app_window_change_and_close(tmp_path):
    clock = Clock()
    engine = await make_engine(tmp_path, clock)

    engine.ingest_perception_snapshot({
        "foreground_window": {
            "hwnd": 10, "pid": 20, "title": "Discord", "process": "Discord.exe",
        },
        "windows": [
            {"hwnd": 10, "process": "Discord.exe"},
        ],
        "recent_files": [],
    }, user_activity_state="ACTIVE")
    first = engine.get_snapshot()
    assert first["current_app"]["value"]["canonical_id"] == "discord"
    assert first["current_app"]["value"]["display_name"] == "Discord"
    assert first["current_window"]["value"]["hwnd"] == 10
    assert first["current_process"]["value"] == {"pid": 20, "name": "Discord.exe"}
    assert first["current_app"]["source"] == "computer_perception:win32"
    assert first["current_app"]["verified"] is True
    assert first["user_activity_state"]["value"] == "ACTIVE"

    clock.value += 1
    await engine.update_from_event(event(
        clock, EventType.COMPUTER_WINDOW_FOREGROUND_CHANGED,
        hwnd=11, pid=21, title="Untitled - Notepad", process="notepad.exe",
    ))
    changed = engine.get_snapshot()
    assert changed["current_app"]["value"]["canonical_id"] == "windows_notepad"
    assert changed["current_window"]["value"]["title"] == "Untitled - Notepad"
    assert changed["current_focus"]["value"]["hwnd"] == 11

    await engine.update_from_event(event(
        clock, EventType.COMPUTER_WINDOW_CLOSED,
        hwnd=11, title="Untitled - Notepad", process="notepad.exe",
    ))
    closed = engine.get_snapshot()
    assert closed["current_app"] is None
    assert closed["current_window"] is None
    assert closed["current_focus"] is None
    await engine.stop()


async def test_application_close_removes_only_matching_current_app(tmp_path):
    clock = Clock()
    engine = await make_engine(tmp_path, clock)
    await engine.update_from_event(event(
        clock, EventType.COMPUTER_WINDOW_FOREGROUND_CHANGED,
        hwnd=7, title="Discord", process="Discord.exe",
    ))
    await engine.update_from_event(event(
        clock, EventType.COMPUTER_APPLICATION_CLOSED,
        hwnd=4, process="notepad.exe",
    ))
    assert engine.get_snapshot()["current_app"]["value"]["canonical_id"] == "discord"
    await engine.update_from_event(event(
        clock, EventType.COMPUTER_APPLICATION_CLOSED,
        hwnd=7, process="Discord.exe",
    ))
    assert engine.get_snapshot()["current_app"] is None
    await engine.stop()


async def test_desktop_target_minimize_restore_and_unverified_action(tmp_path):
    clock = Clock()
    engine = await make_engine(tmp_path, clock)
    await engine.update_from_event(event(
        clock, EventType.COMPUTER_STATE_UPDATED,
        action="MINIMIZE_APP", target="Discord", verified=True,
    ))
    assert engine.get_snapshot()["current_desktop_target"]["value"]["action"] == "MINIMIZE_APP"
    await engine.update_from_event(event(
        clock, EventType.COMPUTER_STATE_UPDATED,
        action="RESTORE_APP", target="Notepad", verified=False,
    ))
    assert engine.get_snapshot()["current_desktop_target"]["value"]["action"] == "MINIMIZE_APP"
    await engine.update_from_event(event(
        clock, EventType.COMPUTER_STATE_UPDATED,
        action="RESTORE_APP", target="Notepad", verified=True,
    ))
    target = engine.get_snapshot()["current_desktop_target"]["value"]
    assert target["action"] == "RESTORE_APP"
    assert target["app"]["canonical_id"] == "windows_notepad"
    await engine.stop()


async def test_verified_artifact_update_and_unverified_rejection(tmp_path):
    clock = Clock()
    engine = await make_engine(tmp_path, clock)
    artifact = {
        "artifact_id": "artifact_1", "path": r"E:\nyra\report.log",
        "display_name": "report.log", "kind": "log", "host_scope": "local",
        "exists_state": "verified",
    }
    await engine.update_from_event(event(
        clock, EventType.ARTIFACT_CONTEXT_UPDATED,
        artifact=artifact, verified=False,
    ))
    assert engine.get_snapshot()["recent_artifacts"] is None

    await engine.update_from_event(event(
        clock, EventType.ARTIFACT_CONTEXT_UPDATED,
        artifact=artifact, verified=True,
    ))
    snapshot = engine.get_snapshot()
    assert snapshot["current_file"]["value"]["path"].endswith("report.log")
    assert snapshot["recent_artifacts"]["value"][-1]["artifact_id"] == "artifact_1"
    assert snapshot["recent_artifacts"]["source"] == "artifact_context"
    await engine.stop()


async def test_usb_connect_disconnect_and_startup_reconciliation(tmp_path):
    clock = Clock()
    engine = await make_engine(tmp_path, clock)
    device = {
        "device_id": "usb_hash", "name": "USB Drive", "category": "Armazenamento",
        "drive_letter": "F:", "status": "CONNECTED", "known": True,
        "serial": "must-not-enter-world-state",
    }
    await engine.update_from_event(event(
        clock, EventType.USB_MONITOR_STARTED,
        state="ACTIVE", connected=1, connected_devices=[device],
    ))
    connected = engine.get_snapshot()["connected_usb"]
    assert connected["value"][0]["device_id"] == "usb_hash"
    assert "serial" not in connected["value"][0]
    await engine.update_from_event(event(
        clock, EventType.USB_DEVICE_DISCONNECTED, device=device,
    ))
    assert engine.get_snapshot()["connected_usb"]["value"] == []
    await engine.stop()


async def test_task_and_monitor_transitions_keep_internal_ids_out_of_context(tmp_path):
    clock = Clock()
    engine = await make_engine(tmp_path, clock)
    await engine.update_from_event(event(
        clock, EventType.TASK_STATE_CHANGED,
        task_id="task_secret", goal="Gerar release", state="RUNNING",
    ))
    await engine.update_from_event(event(
        clock, EventType.MONITOR_JOB_CREATED,
        monitor_id="mon_secret", objective="Aguardar VM online",
        probe_tool="proxmox_vm_status", status="ACTIVE",
    ))
    snapshot = engine.get_snapshot()
    assert snapshot["active_tasks"]["value"][0]["task_id"] == "task_secret"
    assert snapshot["active_monitors"]["value"][0]["monitor_id"] == "mon_secret"
    context = engine.get_relevant_state("qual tarefa e monitoramento estão ativos?")
    assert "task_id" not in context["active_tasks"]["value"][0]
    assert "monitor_id" not in context["active_monitors"]["value"][0]

    await engine.update_from_event(event(
        clock, EventType.TASK_FINISHED,
        task_id="task_secret", goal="Gerar release", state="SUCCEEDED",
    ))
    await engine.update_from_event(event(
        clock, EventType.MONITOR_JOB_COMPLETED,
        monitor_id="mon_secret", objective="Aguardar VM online", status="COMPLETED",
    ))
    assert engine.get_snapshot()["active_tasks"]["value"] == []
    assert engine.get_snapshot()["active_monitors"]["value"] == []
    await engine.stop()


async def test_network_and_integration_updates(tmp_path):
    clock = Clock()
    engine = await make_engine(tmp_path, clock)
    await engine.update_from_event(event(
        clock, EventType.NETWORK_STATUS_UPDATED,
        enabled=True, running=True, status="online",
        snapshot={"internet_reachable": True, "dns_ok": True, "internet_latency_ms": 8.4},
    ))
    await engine.update_from_event(event(
        clock, EventType.SENTINEL_STATUS_CHANGED,
        state="CONNECTED", status={"enabled": True, "state": "CONNECTED"},
    ))
    snapshot = engine.get_snapshot()
    assert snapshot["network_state"]["value"]["status"] == "online"
    assert snapshot["network_state"]["value"]["internet_reachable"] is True
    assert snapshot["integration_state"]["sentinel"]["value"]["state"] == "CONNECTED"
    await engine.stop()


async def test_browser_navigation_and_assistant_activity_state(tmp_path):
    clock = Clock()
    engine = await make_engine(tmp_path, clock)
    await engine.update_from_event(event(
        clock, EventType.COMPUTER_BROWSER_NAVIGATION,
        browser="chrome", tab_id="tab-1", title="NYRA",
        url="https://example.test/docs",
    ))
    await engine.update_from_event(event(clock, EventType.TTS_STARTED))
    snapshot = engine.get_snapshot()
    assert snapshot["current_browser"]["value"]["canonical_id"] == "google_chrome"
    assert snapshot["current_url"]["value"] == "https://example.test/docs"
    assert snapshot["current_tab"]["value"]["id"] == "tab-1"
    assert snapshot["assistant_state"]["value"] == "speaking"
    await engine.update_from_event(event(clock, EventType.TTS_FINISHED))
    assert engine.get_snapshot()["assistant_state"]["value"] == "idle"
    await engine.stop()


async def test_assistant_returns_idle_after_grounded_action_completion(tmp_path):
    clock = Clock()
    engine = await make_engine(tmp_path, clock)
    await engine.update_from_event(event(
        clock, EventType.SHELL_EXECUTION_STARTED, execution_id="exec-1",
    ))
    running = engine.get_snapshot()
    assert running["assistant_state"]["value"] == "acting"
    assert running["current_operation"]["value"]["state"] == "RUNNING"
    await engine.update_from_event(event(
        clock, EventType.SHELL_EXECUTION_FINISHED, execution_id="exec-1",
    ))
    completed = engine.get_snapshot()
    assert completed["assistant_state"]["value"] == "idle"
    assert completed["current_operation"]["value"]["state"] == "COMPLETED"
    await engine.stop()


async def test_ttl_hides_stale_state_instead_of_returning_it_as_current(tmp_path):
    clock = Clock()
    engine = await make_engine(tmp_path, clock)
    engine.ingest_perception_snapshot({
        "foreground_window": {"hwnd": 1, "title": "Discord", "process": "Discord.exe"},
        "windows": [], "recent_files": [],
    })
    assert engine.get_snapshot()["current_app"]["freshness"] == "FRESH"
    clock.value += 7
    stale = engine.get_snapshot()["current_app"]
    assert stale["freshness"] == "STALE"
    assert stale["value"] is None
    assert engine.get_current_focus() is None
    clock.value += 30
    assert engine.get_snapshot()["current_app"]["freshness"] == "EXPIRED"
    await engine.stop()


async def test_restart_persists_only_selected_references(tmp_path):
    clock = Clock()
    path = tmp_path / "world-state.json"
    first = WorldStateEngine(EventBus(), persistence_path=path, clock=clock)
    await first.start()
    first.ingest_perception_snapshot({
        "foreground_window": {"hwnd": 1, "title": "Discord", "process": "Discord.exe"},
        "windows": [], "recent_files": [{"path": r"E:\nyra\README.md", "mtime": 1, "size": 2}],
    })
    first.synchronize_authorities(
        tasks=[{"task_id": "task_1", "objective": "Release", "state": "RUNNING"}],
        monitors=[{"monitor_id": "mon_1", "objective": "VM", "status": "ACTIVE"}],
        artifacts=[{"artifact_id": "a1", "path": r"E:\nyra\report.txt", "exists_state": "verified"}],
    )
    await first.stop()

    second = WorldStateEngine(EventBus(), persistence_path=path, clock=clock)
    await second.start()
    restored = second.get_snapshot()
    assert restored["current_app"] is None
    assert restored["current_window"] is None
    assert restored["assistant_state"]["value"] == "idle"
    assert restored["active_tasks"]["value"][0]["task_id"] == "task_1"
    assert restored["active_monitors"]["value"][0]["monitor_id"] == "mon_1"
    assert restored["recent_artifacts"]["value"][0]["artifact_id"] == "a1"
    assert restored["recent_files"]["value"][0]["path"].endswith("README.md")
    await second.stop()


async def test_provenance_and_free_form_llm_cannot_update_world(tmp_path):
    engine = await make_engine(tmp_path)
    assert engine.update_verified(
        "current_app", "Discord", source="llm", verified=True,
    ) is False
    assert engine.update_verified(
        "current_app", "Discord", source="computer_perception", verified=False,
    ) is False
    await engine.update_from_event(Event(
        type=EventType.LLM_PROCESSING,
        payload={"current_app": "Discord", "verified": True},
    ))
    snapshot = engine.get_snapshot()
    assert snapshot["current_app"] is None
    assert snapshot["assistant_state"]["value"] == "thinking"
    assert engine.health()["rejected_updates"] == 2
    await engine.stop()


async def test_context_relevance_and_compact_world_block(tmp_path):
    engine = await make_engine(tmp_path)
    engine.ingest_perception_snapshot({
        "foreground_window": {"hwnd": 9, "title": "Discord", "process": "Discord.exe"},
        "windows": [{"hwnd": 9, "process": "Discord.exe"}], "recent_files": [],
    })
    assert engine.get_relevant_state("conte uma piada") == {}
    relevant = engine.get_relevant_state("em que app estou?")
    assert relevant["current_app"]["value"]["display_name"] == "Discord"
    summary = engine.context_summary("minimiza ele")
    assert summary.startswith("[WORLD STATE]")
    assert "current_app: Discord" in summary
    assert len(summary) < 1800

    class EmptyMemory:
        async def retrieve(self, *_args, **_kwargs):
            return []

    class EmptyKnowledge:
        async def retrieve(self, *_args, **_kwargs):
            return []

    context = ContextEngine(
        EmptyMemory(), EmptyKnowledge(), budget_characters=2000,
        world_state_provider=engine.context_summary,
    )
    assembly = await context.assemble("em que app estou?", include_runtime=False)
    world_block = next(block for block in assembly.blocks if block.source == "world_state")
    assert "current_app: Discord" in world_block.content
    assert world_block.provenance == {"source": "world_state", "grounded": True}
    await engine.stop()


async def test_operator_uses_world_focus_before_local_rediscovery(tmp_path):
    engine = await make_engine(tmp_path)
    engine.ingest_perception_snapshot({
        "foreground_window": {
            "hwnd": 42, "title": "Discord", "process": "Discord.exe",
        },
        "windows": [{"hwnd": 42, "process": "Discord.exe"}], "recent_files": [],
    })
    state = ComputerStateService(
        base_dir=tmp_path / "computer", idle_fn=lambda: 0,
        world_state=engine,
    )
    resolved = state.resolve_reference("ele")
    assert resolved is not None
    assert resolved.display_name == "Discord"
    assert resolved.hwnd == 42
    assert resolved.source_slot == "world_state.current_focus"
    await engine.stop()


async def test_snapshot_latency_is_measured(tmp_path):
    engine = await make_engine(tmp_path)
    for _ in range(20):
        engine.get_snapshot()
    health = engine.health()
    assert health["average_snapshot_latency_ms"] >= 0
    assert health["average_snapshot_latency_ms"] < 10
    await engine.stop()
