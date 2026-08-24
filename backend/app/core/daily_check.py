"""Daily Check mode (spec Parte BC/BD §240-§247).

Comando interno acionável por API/UI — NUNCA automático por padrão (§241).

Executa somente verificações safe/read-only + fixtures controladas em temp
(§243). Cada categoria produz PASS / DEGRADED / FAIL / SKIPPED com prova;
histórico é persistido para comparação temporal (§246-§247).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.paths import DATA_ROOT

logger = logging.getLogger("nyra.daily_check")

HISTORY_PATH = DATA_ROOT / "daily-check-history.jsonl"
MAX_HISTORY_LINES = 400

PASS = "PASS"
DEGRADED = "DEGRADED"
FAIL = "FAIL"
SKIPPED = "SKIPPED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def run_daily_check(services) -> dict:
    """Run all categories with isolation; one failing category never blocks the rest."""
    generated = _utcnow()
    categories: dict[str, dict[str, Any]] = {}
    tasks = [
        _check_conversation(services, categories),
        _check_llm(services, categories),
        _check_voice(services, categories),
        _check_desktop(services, categories),
        _check_browser(services, categories),
        _check_filesystem_fixture(categories),
        _check_runtime(services, categories),
        _check_jobs(services, categories),
        _check_workflows(services, categories),
        _check_watchdog(services, categories),
        _check_homelab(services, categories),
        _check_integrations(services, categories),
    ]
    await asyncio.gather(*tasks)

    counts = {PASS: 0, DEGRADED: 0, FAIL: 0, SKIPPED: 0}
    for entry in categories.values():
        counts[entry["result"]] = counts.get(entry["result"], 0) + 1
    if counts[FAIL]:
        overall = "FAIL"
    elif counts[DEGRADED]:
        overall = "DEGRADED"
    else:
        overall = "PASS"

    report = {
        "generated_at": generated.isoformat(),
        "overall": overall,
        "counts": counts,
        "categories": categories,
        "version": 1,
    }
    _append_history(report)
    return report


def _append_history(report: dict) -> None:
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(report, ensure_ascii=False)
        existing = HISTORY_PATH.read_text(encoding="utf-8").splitlines() \
            if HISTORY_PATH.exists() else []
        existing.append(line)
        existing = existing[-MAX_HISTORY_LINES:]
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
        tmp.replace(HISTORY_PATH)
    except OSError as error:
        logger.warning("daily check history write failed: %s", error)


def load_history(limit: int = 30) -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-int(limit):]
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError):
        return []


# ------------------------------------------------------------------- categories


async def _check_conversation(services, out: dict) -> None:
    state = services.conversation.state.value
    registry = getattr(services.orchestrator, "turns", None)
    metrics = registry.snapshot().get("metrics", {}) if registry else {}
    active = int(metrics.get("active_turns") or 0)
    failed = int(metrics.get("failed_turns") or 0)
    result, detail = PASS, {"pipeline_state": state, "active_turns": active,
                            "failed_turns": failed}
    if state == "ERROR":
        result = FAIL
    elif active > 8 or failed > 50:
        result = DEGRADED
    out["Conversation"] = {"result": result, "details": detail}


async def _check_llm(services, out: dict) -> None:
    try:
        healthy, ready = await asyncio.gather(services.llm.health(), services.llm.ready())
    except Exception as error:  # noqa: BLE001
        out["LLM"] = {"result": FAIL, "details": {"error_type": type(error).__name__}}
        return
    warm_state = services.warm_manager.status()["state"] if services.warm_manager else None
    details = {"healthy": healthy, "ready": ready, "warm_state": warm_state,
               "model": services.settings.llm_model}
    if healthy and ready:
        result = PASS
    elif healthy:
        result = DEGRADED  # API viva mas modelo não residente ainda
    else:
        result = FAIL
    out["LLM"] = {"result": result, "details": details}


async def _check_voice(services, out: dict) -> None:
    try:
        stt_ok, tts_ok = await asyncio.gather(services.stt.health(), services.tts.health())
        microphone = bool(services.listening.status().get("microphone"))
    except Exception as error:  # noqa: BLE001
        out["Voice"] = {"result": FAIL, "details": {"error_type": type(error).__name__}}
        return
    details = {"stt": stt_ok, "tts": tts_ok, "microphone": microphone}
    if stt_ok and tts_ok:
        result = PASS
    elif stt_ok or tts_ok:
        result = DEGRADED
    else:
        result = FAIL
    if not microphone and result is PASS:
        # Microfone ausente não derruba voz de texto; degrada com informação (§150).
        result = DEGRADED
        details["note"] = "microfone ausente; texto/TTS seguem funcionando"
    out["Voice"] = {"result": result, "details": details}


async def _check_desktop(services, out: dict) -> None:
    controller = services.desktop
    if controller is None:
        out["Desktop"] = {"result": SKIPPED, "details": {"reason": "controller ausente"}}
        return
    try:
        apps = controller.list_apps()
        windows = controller.status_windows()
    except Exception as error:  # noqa: BLE001
        out["Desktop"] = {"result": FAIL, "details": {"error_type": type(error).__name__}}
        return
    count = len(apps.get("apps", []) if isinstance(apps, dict) else [])
    windows_count = len(windows.get("windows", []) if isinstance(windows, dict) else [])
    out["Desktop"] = {"result": PASS if count else DEGRADED,
                      "details": {"apps_catalogued": count, "windows_visible": windows_count}}


async def _check_browser(services, out: dict) -> None:
    del services
    # Browser control só é exercitado em E2E dedicado; aqui apenas presença.
    out["Browser"] = {"result": SKIPPED,
                      "details": {"reason": "verificação real coberta por daily_use_e2e"}}


async def _check_filesystem_fixture(out: dict) -> None:
    """Fixture controlada em temp: mkdir/write/read/rename/copy/delete (§125)."""
    steps_done: list[str] = []
    try:
        base = Path(tempfile.mkdtemp(prefix="nyra-daily-check-"))
        target = base / "fixture.txt"
        target.write_text("nyra-daily-check", encoding="utf-8")
        steps_done.append("write")
        content = target.read_text(encoding="utf-8")
        steps_done.append("read")
        renamed = base / "fixture-renamed.txt"
        target.rename(renamed)
        steps_done.append("rename")
        copied = base / "fixture-copy.txt"
        shutil.copy2(renamed, copied)
        steps_done.append("copy")
        ok = content == "nyra-daily-check" and renamed.exists() and copied.exists()
        shutil.rmtree(base, ignore_errors=True)
        steps_done.append("delete")
        out["Filesystem"] = {
            "result": PASS if ok and not base.exists() else FAIL,
            "details": {"steps_completed": steps_done,
                        "base_removed": not base.exists()},
        }
    except OSError as error:
        out["Filesystem"] = {"result": FAIL,
                             "details": {"steps_completed": steps_done,
                                         "error_type": type(error).__name__}}


async def _check_runtime(services, out: dict) -> None:
    try:
        snapshots = await services.runtime_supervisor.inspect_all_public()
    except Exception as error:  # noqa: BLE001
        out["Runtime"] = {"result": FAIL, "details": {"error_type": type(error).__name__}}
        return
    states = [{"id": getattr(item, "service_id", "?"), "state": str(getattr(item, "state", ""))}
              for item in snapshots]
    failed = [item for item in states if item["state"] in {"FAILED", "CRASH_LOOP"}]
    disabled = [item for item in states if item["state"] == "DISABLED"]
    effective = [item for item in states if item not in disabled]
    bad = len(failed)
    result = PASS if bad == 0 else (DEGRADED if bad < max(1, len(effective)) else FAIL)
    out["Runtime"] = {"result": result, "details": {"services": states, "failed": bad}}


async def _check_jobs(services, out: dict) -> None:
    jobs = getattr(getattr(services, "operator_v2", None), "jobs", None)
    if jobs is None:
        out["Jobs"] = {"result": SKIPPED, "details": {"reason": "persistent jobs desabilitado"}}
        return
    try:
        listing = await jobs.list(include_terminal=False)
    except Exception as error:  # noqa: BLE001
        out["Jobs"] = {"result": FAIL, "details": {"error_type": type(error).__name__}}
        return
    items = listing.get("jobs", []) if isinstance(listing, dict) else []
    stuck = [item for item in items if item.get("state") in {"STARTING"}]
    out["Jobs"] = {
        "result": PASS if not stuck else DEGRADED,
        "details": {"active_jobs": len(items), "starting_stuck": len(stuck)},
    }


async def _check_workflows(services, out: dict) -> None:
    engine = getattr(getattr(services, "operator_v2", None), "workflows", None)
    if engine is None:
        out["Workflows"] = {"result": SKIPPED,
                            "details": {"reason": "workflow engine desabilitada"}}
        return
    plan = engine.dry_run("wf_check_nyra_health")
    if not plan.get("success"):
        seeded = engine.seed_templates()
        plan = engine.dry_run("wf_check_nyra_health")
        if not plan.get("success"):
            out["Workflows"] = {
                "result": FAIL if not seeded.get("success") else SKIPPED,
                "details": {"reason": "template wf_check_nyra_health indisponível",
                            "seed": seeded},
            }
            return
    missing = plan.get("missing_parameters") or []
    cycle = plan.get("cycle") or []
    out["Workflows"] = {
        "result": PASS if not cycle else FAIL,
        "details": {"dry_run_ok": True, "missing_parameters": missing, "cycle": cycle,
                    "steps_planned": len(plan.get("plan", []))},
    }


async def _check_watchdog(_services, out: dict) -> None:
    heartbeat = DATA_ROOT / "watchdog-heartbeat.json"
    if not heartbeat.exists():
        out["Watchdog"] = {"result": SKIPPED,
                           "details": {"reason": "watchdog externo não iniciado"}}
        return
    try:
        document = json.loads(heartbeat.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        out["Watchdog"] = {"result": FAIL, "details": {"error_type": type(error).__name__}}
        return
    written = document.get("timestamp") or document.get("written_at")
    age = None
    if isinstance(written, (int, float)):
        age = max(0.0, _utcnow().timestamp() - float(written))
    stale = age is None or age > 60
    out["Watchdog"] = {
        "result": DEGRADED if stale else PASS,
        "details": {"heartbeat_age_seconds": round(age, 1) if age is not None else None,
                    "components": document.get("components")},
    }


async def _check_homelab(services, out: dict) -> None:
    homelab = services.homelab
    if homelab is None:
        out["Homelab"] = {"result": SKIPPED, "details": {"reason": "control plane ausente"}}
        return
    try:
        status = homelab.status()
    except Exception as error:  # noqa: BLE001
        out["Homelab"] = {"result": FAIL, "details": {"error_type": type(error).__name__}}
        return
    integrations = status.get("integrations", {}) if isinstance(status, dict) else {}
    normalized = {key: (value.get("state") if isinstance(value, dict) else str(value))
                  for key, value in integrations.items()}
    configured = any(str(value).upper() not in {"UNCONFIGURED", "DISABLED"}
                     for value in normalized.values())
    if not configured:
        out["Homelab"] = {"result": SKIPPED,
                          "details": {"reason": "nenhum host configurado",
                                      "integrations": normalized}}
        return
    offline_only = all(str(value).upper() in {"OFFLINE", "UNREACHABLE", "AUTHENTICATION_FAILED"}
                       for value in normalized.values())
    out["Homelab"] = {
        "result": DEGRADED if offline_only else PASS,
        "details": {"integrations": normalized,
                    "note": "auth failure é reportado honestamente, nunca como offline falso"},
    }


async def _check_integrations(services, out: dict) -> None:
    settings = services.settings
    components: dict[str, str] = {}

    # Resolução ÚNICA de credenciais (prompt11_1 §6): perfil ativo/Broker com
    # fallback legado — nunca imprime o valor.
    from app.integrations.home_assistant_profiles import (
        active_profile_id,
        resolve_profile_token,
    )

    ha_token = ""
    active = active_profile_id()
    if active:
        ha_token = resolve_profile_token(str(active))
    if not ha_token:
        ha_token = str(getattr(settings, "home_assistant_token", "") or "")
    ha_ready = bool(settings.home_assistant_url and ha_token)
    components["home_assistant"] = "configured" if ha_ready else "unconfigured"
    if ha_ready:
        try:
            from app.integrations.home_assistant import HomeAssistantClient

            client = HomeAssistantClient(settings.home_assistant_url, ha_token)
            config = await client.config()
            core_state = str((config or {}).get("state", "")).upper()
            components["home_assistant_core"] = core_state or "unknown"
        except Exception as error:  # noqa: BLE001
            components["home_assistant_error"] = type(error).__name__

    from app.integrations.proxmox.config import resolve_credentials

    pm_token_id, pm_token_secret = resolve_credentials(settings)
    proxmox_ready = bool(pm_token_id and pm_token_secret)
    components["proxmox"] = "configured" if proxmox_ready else "unconfigured"

    sentinel = services.sentinel
    sentinel_enabled = bool(getattr(getattr(sentinel, "settings", None),
                                    "sentinel_watch_enabled", False))
    components["sentinel"] = str(getattr(sentinel, "state", "disabled")).lower()

    degraded = any(value.startswith(("home_assistant_error",)) for value in components)
    unconfigured_all = (
        components["home_assistant"] == "unconfigured"
        and components["proxmox"] == "unconfigured"
        and components["sentinel"] in {"disabled", ""}
    )
    if unconfigured_all:
        result = SKIPPED
    elif degraded:
        result = DEGRADED
    else:
        result = PASS
    out["Integrations"] = {"result": result, "details": components}
