"""Release Readiness / About / Support Bundle / World State (prompt11
Partes AP/AQ/AR/BE/BF §206-§236).

Tudo aqui é somente leitura e não-secreto:

* ``about_payload``       — versão unificada do produto + componentes.
* ``release_health``      — GREEN/YELLOW/RED com critérios verificáveis.
* ``support_bundle``      — versões + estados de subsistemas/integrações +
  erros seguros recentes + release health (para suporte/documentação).
* ``world_state_snapshot``— observações categorizadas para a página World State.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from app.core.paths import DATA_ROOT, LOG_ROOT, PROJECT_ROOT, RUNTIME_ROOT

logger = logging.getLogger("nyra.release_info")

DAILY_CHECK_HISTORY = DATA_ROOT / "daily-check-history.jsonl"
RELEASE_GATE_REPORT = RUNTIME_ROOT / "reports" / "release-health.json"
GATE_PROGRESS = RUNTIME_ROOT / "reports" / "release-gate-progress.json"

# Versão oficial unificada do produto.
APP_VERSION = "0.4.0"
APP_NAME = "NYRA"

# Artefatos mais antigos que isso não representam o build atual (closure §20):
# viram STALE e nunca produzem RED por si sós.
ARTIFACT_STALE_SECONDS = 12 * 3600
REVALIDATION_TIMEOUT_SECONDS = 30 * 60

_revalidation: dict[str, Any] = {"state": "IDLE"}
_git_head_cache: dict[str, Any] = {"value": None, "at": 0.0}


def git_head() -> str | None:
    """HEAD curto do repositório (leitura only, cacheado por 60s)."""
    now = time.time()
    if _git_head_cache["value"] is not None and now - _git_head_cache["at"] < 60:
        return str(_git_head_cache["value"])
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0 and completed.stdout.strip():
            _git_head_cache.update(value=completed.stdout.strip()[:12], at=now)
            return str(_git_head_cache["value"])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def about_payload(services: Any) -> dict[str, Any]:
    settings = services.settings
    return {
        "generated_at": time.time(),
        "name": APP_NAME,
        "version": APP_VERSION,
        "model": settings.llm_model,
        "components": {
            "backend": f"FastAPI ({APP_VERSION})",
            "frontend": f"React/Vite ({APP_VERSION})",
            "desktop": f"Tauri 2 ({APP_VERSION})",
            "llm_provider": services.llm.name,
            "stt_provider": getattr(services.stt, "name", ""),
            "tts_provider": getattr(services.tts, "name", ""),
        },
        "license_note": "Uso pessoal/local. Integrações de voz possuem "
                        "licenças próprias — ver docs/voice-licensing.md.",
    }


def _last_daily_check() -> dict[str, Any] | None:
    try:
        if not DAILY_CHECK_HISTORY.is_file():
            return None
        lines = DAILY_CHECK_HISTORY.read_text(encoding="utf-8").strip().splitlines()
        for line in reversed(lines):
            try:
                document = json.loads(line)
                if isinstance(document, dict):
                    return document
            except ValueError:
                continue
    except OSError:
        return None
    return None


def _release_gate_report() -> dict[str, Any] | None:
    try:
        if RELEASE_GATE_REPORT.is_file():
            return json.loads(RELEASE_GATE_REPORT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None


def _artifact_age_seconds(path: Path) -> float | None:
    """Idade (s) do artefato pelo generated_at interno; None = sem timestamp confiável."""
    try:
        if not path.is_file():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stamp = document.get("generated_at") if isinstance(document, dict) else None
    if isinstance(stamp, str):
        try:
            from datetime import datetime
            stamp = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            return None
    if not isinstance(stamp, (int, float)) or stamp <= 0:
        return None
    return max(0.0, time.time() - float(stamp))


def release_health(services: Any) -> dict[str, Any]:
    criteria: list[dict[str, Any]] = []
    daily = _last_daily_check()
    gate = _release_gate_report()
    head = git_head()

    daily_age = _artifact_age_seconds(DAILY_CHECK_HISTORY)
    gate_age = _artifact_age_seconds(RELEASE_GATE_REPORT)

    def classify(state: str, age: float | None) -> str:
        # Closure §20: artefato que não corresponde ao build atual é STALE,
        # nunca RED atual. Sem timestamp confiável → STALE (§20.3).
        if state == "PENDING":
            return state
        if age is None or age > ARTIFACT_STALE_SECONDS:
            return "STALE"
        return state

    if daily is not None:
        overall = str(daily.get("overall", "")).upper()
        raw_state = "PASS" if overall == "PASS" else ("DEGRADED" if overall == "DEGRADED" else "FAIL")
        criteria.append({
            "id": "daily_use_suite",
            "state": classify(raw_state, daily_age),
            "detail": f"daily-check {overall} em {daily.get('timestamp', '?')}",
            "source": "data/daily-check-history.jsonl",
            "artifact_age_seconds": round(daily_age) if daily_age is not None else None,
        })
        categories = daily.get("categories") or {}
        failed = [name for name, value in categories.items()
                  if isinstance(value, dict) and str(value.get("result", "")).upper() == "FAIL"]
        criteria.append({
            "id": "no_daily_failures",
            "state": classify("PASS" if not failed else "FAIL", daily_age),
            "detail": "todas as categorias OK" if not failed else f"FALHAS: {', '.join(failed[:8])}",
            "source": "data/daily-check-history.jsonl",
            "artifact_age_seconds": round(daily_age) if daily_age is not None else None,
        })
    else:
        criteria.append({
            "id": "daily_use_suite",
            "state": "PENDING",
            "detail": "daily-use nunca executado (POST /api/daily_check/run).",
            "source": "data/daily-check-history.jsonl",
        })

    if gate is not None:
        gate_state = str(gate.get("verdict") or gate.get("state", "")).upper()
        raw_state = "PASS" if gate_state == "GREEN" else ("WARN" if gate_state == "YELLOW" else "FAIL")
        criteria.append({
            "id": "release_gate",
            "state": classify(raw_state, gate_age),
            "detail": str(gate.get("summary", ""))[:200],
            "source": ".tmp/release-health.json",
            "artifact_age_seconds": round(gate_age) if gate_age is not None else None,
        })
    else:
        criteria.append({
            "id": "release_gate",
            "state": "PENDING",
            "detail": "scripts/release_gate.py ainda não produziu .tmp/release-health.json.",
            "source": ".tmp/release-health.json",
        })

    # Encoding audit: zero mojibake nos fontes (critério contínuo, barato).
    try:
        from app.core.encoding_audit import PROJECT_ROOT as _AUDIT_ROOT
        from app.core.encoding_audit import iter_targets, scan_file

        offenders = 0
        for target in (_AUDIT_ROOT / "frontend" / "src",
                       _AUDIT_ROOT / "backend" / "app"):
            for path in iter_targets(target):
                if path.name in {"encoding_audit.py", "exotic_scan.py"}:
                    continue
                if scan_file(path):
                    offenders += 1
        criteria.append({
            "id": "encoding_audit",
            "state": "PASS" if not offenders else "FAIL",
            "detail": "zero mojibake" if not offenders else f"{offenders} arquivos com problemas",
        })
    except Exception as error:  # noqa: BLE001
        criteria.append({"id": "encoding_audit", "state": "PENDING",
                         "detail": type(error).__name__})

    blocking_states = {"FAIL"}
    warning_states = {"WARN", "DEGRADED", "PENDING", "STALE"}
    if any(c["state"] in blocking_states for c in criteria):
        overall = "RED"
    elif any(c["state"] in warning_states for c in criteria):
        overall = "YELLOW"
    else:
        overall = "GREEN"
    progress = revalidation_progress_from_disk() if _revalidation.get("state") == "RUNNING" else None
    return {
        "generated_at": time.time(),
        "git_head": head,
        "freshness": "STALE" if any(c["state"] == "STALE" for c in criteria) else "FRESH",
        "revalidation": {**_revalidation, "progress": progress},
        "state": overall,
        "criteria": criteria,
        "definition": {
            "GREEN": "gates obrigatórios ATUAIS passando",
            "YELLOW": "pendências não bloqueantes ou validação STALE (use Revalidar)",
            "RED": "falha crítica de validação ATUAL",
            "STALE": "artefato antigo: não representa o build atual",
        },
    }


async def start_release_revalidation() -> dict[str, Any]:
    """Closure §21: dispara o release gate em background (nunca bloqueia a UI).

    Executa a bateria rápida (--skip-daily-e2e --skip-stress) com timeout duro;
    progresso por etapa vem de .tmp/release-gate-progress.json.
    """
    if _revalidation.get("state") == "RUNNING":
        return {**_revalidation, "already_running": True}
    python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        _revalidation.clear()
        _revalidation.update(state="FAILED", error="RUNTIME_RESOLVER_MISSING")
        return dict(_revalidation)
    log_path = LOG_ROOT / "release-gate-revalidate.log"
    _revalidation.clear()
    _revalidation.update(
        state="RUNNING", started_at=time.time(), started_iso=time.strftime("%Y-%m-%dT%H:%M:%S"),
        pid=None, step_index=0, total_steps=5, current_step="preparando",
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "ab")  # noqa: SIM115
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = await asyncio.create_subprocess_exec(
            str(python), str(PROJECT_ROOT / "scripts" / "release_gate.py"),
            "--skip-daily-e2e", "--skip-stress",
            cwd=str(PROJECT_ROOT), stdout=handle, stderr=handle,
            creationflags=creationflags,
        )
        handle.close()
        _revalidation["pid"] = process.pid
    except OSError as error:
        _revalidation.clear()
        _revalidation.update(state="FAILED", error=f"SPAWN_FAILED:{type(error).__name__}")
        return dict(_revalidation)

    async def _await_completion() -> None:
        try:
            exit_code = await asyncio.wait_for(process.wait(), REVALIDATION_TIMEOUT_SECONDS)
            _revalidation.update(state="DONE" if exit_code == 0 else "DONE_WITH_FAILURES",
                                 finished_at=time.time(), exit_code=int(exit_code))
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            _revalidation.update(state="TIMEOUT", finished_at=time.time())
        logger.info("release_revalidation_finished state=%s", _revalidation.get("state"))

    asyncio.create_task(_await_completion())
    return dict(_revalidation)


def revalidation_progress_from_disk() -> dict[str, Any]:
    """Progresso publicado pelo release_gate.py (etapa atual N/M)."""
    try:
        if GATE_PROGRESS.is_file():
            document = json.loads(GATE_PROGRESS.read_text(encoding="utf-8"))
            if isinstance(document, dict) and document.get("started_at", 0) >= float(_revalidation.get("started_at") or 0):
                _revalidation.update(
                    step_index=int(document.get("step_index", 0)),
                    total_steps=int(document.get("total_steps", 0)),
                    current_step=str(document.get("current_step", ""))[:60],
                )
    except (OSError, ValueError):
        pass
    return {"step_index": _revalidation.get("step_index"), "total_steps": _revalidation.get("total_steps"),
            "current_step": _revalidation.get("current_step")}


async def support_bundle(services: Any) -> dict[str, Any]:
    """Bundle de suporte (§237): seguro por construção — sem segredos."""
    from app.core.capabilities import get_capabilities
    from app.integrations.center import integrations_status

    about = about_payload(services)
    capabilities = await get_capabilities(services)
    integrations = await integrations_status(services)
    recent_errors = []
    try:
        history = services.event_bus.history()
        for event in history[-120:]:
            event_type = str(getattr(event.type, "value", event.type))
            if "ERROR" in event_type or "FAILED" in event_type or "RECOVERY" in event_type:
                payload = getattr(event, "payload", {}) or {}
                recent_errors.append({
                    "type": event_type,
                    "timestamp": getattr(event, "timestamp", None),
                    "operation": str(payload.get("operation", ""))[:60],
                    "error_code": str(payload.get("error_code", ""))[:60],
                })
        recent_errors = recent_errors[-20:]
    except Exception as error:  # noqa: BLE001
        logger.info("bundle_event_history_unavailable error=%s", type(error).__name__)

    watchdog_status: dict[str, Any]
    try:
        heartbeat = services.settings.watchdog_heartbeat_path
        if heartbeat.exists():
            document = json.loads(heartbeat.read_text(encoding="utf-8"))
            age = round(max(0.0, time.time() - float(document.get("timestamp", 0))), 1)
            watchdog_status = {"heartbeat_age_seconds": age, "stale": age > 30}
        else:
            watchdog_status = {"running": False}
    except Exception:  # noqa: BLE001
        watchdog_status = {"running": False}

    return {
        **about,
        "release_health": release_health(services),
        "capabilities_summary": capabilities["summary"],
        "capabilities_pending_restart": capabilities["summary"].get("restart_required", 0),
        "integrations": {
            key: {f: card.get(f) for f in
                  ("enabled", "configured", "connected", "state", "health", "last_error")}
            for key, card in integrations["integrations"].items()
        },
        "recent_safe_errors": recent_errors,
        "watchdog": watchdog_status,
        "note": "Este bundle não contém segredos, áudio, memórias nem topologia além "
                "dos hosts já cadastrados no homelab.",
    }


async def world_state_snapshot(services: Any) -> dict[str, Any]:
    """Página World State (Parte S §103-§106): observações categorizadas."""
    observations: list[dict[str, Any]] = []

    def observe(category: str, name: str, state: str, source: str,
                verification: str, detail: str = "", fresh_seconds: float = 0) -> None:
        observations.append({
            "category": category,
            "name": name,
            "state": state,
            "source": source,
            "observed_at": time.time(),
            "freshness": "FRESH" if fresh_seconds < 300 else "STALE",
            "verification": verification,
            "detail": detail[:160],
        })

    # Local computer
    perception_enabled = bool(getattr(getattr(services, "perception", None),
                                      "snapshot", None) and services.perception.snapshot.enabled)
    observe("Local Computer", "Percepção PC",
            "READY" if perception_enabled else "DISABLED",
            "realtime.perception", "self_reported")
    # Applications / Services
    try:
        snapshots = await services.runtime_supervisor.inspect_all_public()
        running = sum(
            1 for s in snapshots
            if str(getattr(getattr(s, "state", None), "value", getattr(s, "state", ""))).endswith("RUNNING")
        )
        total = len(snapshots)
        observe("Services", "Serviços gerenciados",
                "READY" if running == total and total else ("READY" if not total else "DEGRADED"),
                "runtime.supervisor", "health_checks",
                detail=f"{running}/{total} RUNNING")
    except Exception as error:  # noqa: BLE001
        observe("Services", "Serviços gerenciados", "OFFLINE",
                "runtime.supervisor", "failed", detail=type(error).__name__)
    # Network
    try:
        network_status = services.network_watch.status()
        enabled = bool(network_status.get("enabled"))
        snapshot = network_status.get("snapshot") or {}
        snapshot_ts = str(snapshot.get("timestamp") or "")
        fresh_seconds = 0
        if snapshot_ts:
            try:
                from datetime import datetime as _dt

                parsed = _dt.fromisoformat(snapshot_ts.replace("Z", "+00:00"))
                fresh_seconds = max(0.0, time.time() - parsed.timestamp())
            except ValueError:
                fresh_seconds = 0
        observe("Network", "Network Watch",
                "READY" if enabled else "DISABLED",
                "network_watch.monitor", "probes",
                detail=str(network_status.get("target_summary", ""))[:120],
                fresh_seconds=fresh_seconds)
    except Exception as error:  # noqa: BLE001
        observe("Network", "Network Watch", "OFFLINE",
                "network_watch.monitor", "failed", detail=type(error).__name__)
    # Homelab
    try:
        overview = await services.homelab.overview(force=False)
        summary = getattr(overview, "summary", {}) or {}
        reachable = int(summary.get("reachable", summary.get("online", 0)) or 0)
        generated_at = float(getattr(overview, "generated_at", 0.0) or 0.0)
        observe("Homelab", "Hosts registrados",
                "READY" if reachable else "OFFLINE",
                "homelab.controller", "icmp/tcp probes",
                detail=f"{reachable} alcançáveis",
                fresh_seconds=max(0.0, time.time() - generated_at) if generated_at else 0)
    except Exception as error:  # noqa: BLE001
        observe("Homelab", "Hosts registrados", "OFFLINE",
                "homelab.controller", "failed", detail=type(error).__name__)
    # Integrations
    try:
        sentinel_status = services.sentinel.status()
        state = str(sentinel_status.get("state"))
        observe("Integrations", "UTAMO Sentinel",
                "READY" if state == "CONNECTED" else ("DISABLED" if not sentinel_status.get("enabled") else "DEGRADED"),
                "sentinel.connector", "socket.io bridge",
                detail=f"bridge v{sentinel_status.get('bridge_version')}")
    except Exception as error:  # noqa: BLE001
        observe("Integrations", "UTAMO Sentinel", "OFFLINE",
                "sentinel.connector", "failed", detail=type(error).__name__)
    # Tasks & Jobs
    operator_v2 = getattr(services, "operator_v2", None)
    tasks_running = jobs_running = 0
    if operator_v2 is not None:
        try:
            tasks_running = len(operator_v2.tasks.list_tasks(state="RUNNING")) if hasattr(operator_v2.tasks, "list_tasks") else 0
        except Exception:  # noqa: BLE001
            tasks_running = 0
        try:
            jobs_running = sum(
                1 for j in operator_v2.jobs.list_jobs().get("jobs", [])
                if str(j.get("state", "")).upper() == "RUNNING"
            ) if hasattr(operator_v2.jobs, "list_jobs") else 0
        except Exception:  # noqa: BLE001
            jobs_running = 0
    observe("Tasks", "Tasks ativas", "READY" if operator_v2 else "DISABLED",
            "operator.tasks", "registry", detail=f"{tasks_running} ativas")
    observe("Jobs", "Jobs persistentes", "READY" if operator_v2 else "DISABLED",
            "operator.jobs", "process monitor", detail=f"{jobs_running} RUNNING")

    categories = ["Local Computer", "Applications", "Services", "Network",
                  "Homelab", "Integrations", "Tasks", "Jobs"]
    grouped = {c: [] for c in categories}
    for item in observations:
        grouped.setdefault(item["category"], []).append(item)
    return {
        "generated_at": time.time(),
        "categories": [
            {"category": c, "observations": items}
            for c, items in grouped.items() if items
        ],
        "total_observations": len(observations),
    }
