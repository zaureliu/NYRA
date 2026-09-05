"""Grounded event normalization and natural-message policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.events import Event, EventType
from app.proactive_presence.models import ProactiveCandidate, ProactivePriority


SUPPORTED_EVENTS = {
    EventType.NETWORK_STATUS_UPDATED,
    EventType.NETWORK_GATEWAY_DOWN,
    EventType.NETWORK_GATEWAY_RECOVERED,
    EventType.NETWORK_INTERNET_DOWN,
    EventType.NETWORK_INTERNET_RECOVERED,
    EventType.NETWORK_DNS_FAILURE,
    EventType.NETWORK_DNS_RECOVERED,
    EventType.NETWORK_HIGH_LATENCY,
    EventType.NETWORK_LATENCY_RECOVERED,
    EventType.NETWORK_PACKET_LOSS,
    EventType.NETWORK_PACKET_LOSS_RECOVERED,
    EventType.NETWORK_HIGH_JITTER,
    EventType.NETWORK_JITTER_RECOVERED,
    EventType.NETWORK_LINK_DOWN,
    EventType.NETWORK_LINK_UP,
    EventType.NETWORK_RECOVERED,
    EventType.HOMELAB_HOST_ONLINE,
    EventType.HOMELAB_HOST_OFFLINE,
    EventType.HOMELAB_HOST_DEGRADED,
    EventType.PROXMOX_VM_CHANGED,
    EventType.PROXMOX_TASK_COMPLETED,
    EventType.PROXMOX_TASK_FAILED,
    EventType.HOME_ASSISTANT_ACTION_VERIFIED,
    EventType.SENTINEL_EVENT,
    EventType.TASK_STATE_CHANGED,
    EventType.TASK_FINISHED,
    EventType.AGENT_RUN_FINISHED,
    EventType.JOB_FINISHED,
    EventType.WORKFLOW_FINISHED,
    EventType.MONITOR_JOB_CHANGED,
    EventType.MONITOR_JOB_COMPLETED,
    EventType.MONITOR_JOB_FAILED,
    EventType.OPEN_LOOP_CREATED,
    EventType.OPEN_LOOP_STATE_CHANGED,
    EventType.OPEN_LOOP_RESOLVED,
    EventType.USB_DEVICE_CONNECTED,
    EventType.USB_DEVICE_KNOWN_CONNECTED,
    EventType.USB_DEVICE_UNKNOWN,
    EventType.USB_DEVICE_IDENTITY_CHANGED,
    EventType.USB_DEVICE_COM_CHANGED,
    EventType.USB_DEVICE_DISCONNECTED,
    EventType.USB_MONITOR_FAILURE,
    EventType.ARTIFACT_CONTEXT_UPDATED,
    EventType.SELFDEV_VALIDATION_PASS,
    EventType.SELFDEV_VALIDATION_FAIL,
    EventType.SELFDEV_POST_VALIDATION_PASS,
    EventType.SELFDEV_PROMOTION_APPLIED,
    EventType.SELFDEV_ROLLBACK,
    EventType.RUNTIME_FAILED,
    EventType.RUNTIME_CRASH_LOOP,
    EventType.RUNTIME_RECOVERED,
    EventType.COMPUTER_EFFECT_VERIFIED,
    EventType.COMPUTER_OPERATOR_FAILURE,
    EventType.COMPUTER_VERIFICATION_FAILURE,
}


def candidate_from_event(event: Event, linked_loop: Any = None) -> ProactiveCandidate | None:
    event_type = event.type
    name = event_type.value
    payload = event.payload if isinstance(event.payload, dict) else {}
    source = _source(event_type)
    loop_id = str(getattr(linked_loop, "id", "") or "") or None
    goal_id = str(payload.get("goal_id") or getattr(linked_loop, "goal", "") or "") or None

    if event_type == EventType.NETWORK_STATUS_UPDATED:
        return _candidate(event, source, "network-status", "network_status", "Estado de rede atualizado.",
                          ProactivePriority.LOW, .1, .1, baseline="IGNORE")
    if event_type in _NETWORK_DOWN:
        entity = _network_entity(event_type, payload)
        incident = f"network:{entity.casefold()}"
        message = _message(payload, _network_down_message(event_type, entity))
        return _candidate(event, source, entity, "network_outage", message,
                          ProactivePriority.HIGH, .9, .9, opens_incident=incident)
    if event_type in _NETWORK_RECOVERY:
        entity = _network_entity(event_type, payload)
        incident = f"network:{entity.casefold()}"
        return _candidate(event, source, entity, "network_recovered",
                          _message(payload, _network_recovery_message(event_type, entity)),
                          ProactivePriority.NORMAL, .65, .45, recovery_of=incident)
    if event_type in _NETWORK_QUALITY:
        entity = _network_entity(event_type, payload)
        severity = str(payload.get("severity") or "warning").casefold()
        priority = ProactivePriority.HIGH if severity == "critical" else ProactivePriority.NORMAL
        return _candidate(event, source, entity, "network_quality",
                          _message(payload, "A qualidade da conexão piorou de forma sustentada."),
                          priority, .7, .55)

    if event_type in {EventType.HOMELAB_HOST_OFFLINE, EventType.HOMELAB_HOST_DEGRADED}:
        entity = _label(payload, "display_name", "host_id", "host", default="um host do homelab")
        state = "ficou offline" if event_type == EventType.HOMELAB_HOST_OFFLINE else "ficou degradado"
        return _candidate(event, source, entity, "homelab_unavailable", f"{entity} {state}.",
                          ProactivePriority.HIGH, .86, .78,
                          opens_incident=f"homelab:{entity.casefold()}")
    if event_type == EventType.HOMELAB_HOST_ONLINE:
        entity = _label(payload, "display_name", "host_id", "host", default="O host")
        return _candidate(event, source, entity, "homelab_recovered", f"{entity} voltou a ficar online.",
                          ProactivePriority.NORMAL, .65, .45,
                          recovery_of=f"homelab:{entity.casefold()}")
    if event_type == EventType.PROXMOX_TASK_FAILED:
        entity = _label(payload, "guest", "vmid", default="a operação no Proxmox")
        return _candidate(event, source, entity, "proxmox_task", f"A operação no Proxmox para {entity} falhou.",
                          ProactivePriority.HIGH, .88, .78)
    if event_type == EventType.PROXMOX_TASK_COMPLETED:
        entity = _label(payload, "guest", "vmid", default="a máquina virtual")
        return _candidate(event, source, entity, "proxmox_task", f"A operação no Proxmox para {entity} terminou.",
                          ProactivePriority.NORMAL, .65, .45)
    if event_type == EventType.PROXMOX_VM_CHANGED:
        entity = _label(payload, "guest", "vmid", default="A máquina virtual")
        verified = payload.get("effect_verified") is True
        priority = ProactivePriority.NORMAL if verified else ProactivePriority.HIGH
        state = str(payload.get("new_state") or "mudou de estado")
        return _candidate(event, source, entity, "proxmox_vm_state", f"{entity} agora está {state}.",
                          priority, .72, .55)
    if event_type == EventType.HOME_ASSISTANT_ACTION_VERIFIED:
        entity = _label(payload, "entity_id", default="Home Assistant")
        return _candidate(event, source, entity, "home_assistant_verified",
                          f"A alteração em {entity} foi confirmada.", ProactivePriority.LOW, .35, .2,
                          baseline="LOG_ONLY")

    if event_type == EventType.SENTINEL_EVENT:
        raw = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        entity_data = raw.get("entity") if isinstance(raw.get("entity"), dict) else {}
        entity = str(entity_data.get("name") or raw.get("title") or "Sentinel")[:240]
        severity = str(raw.get("severity") or "info").casefold()
        message = str(raw.get("summary") or "O Sentinel detectou uma mudança relevante.")[:500]
        if payload.get("replay") is True:
            baseline = "LOG_ONLY"
        else:
            baseline = "EVALUATE" if severity in {"warning", "critical", "recovery"} else "LOG_ONLY"
        priority = {
            "critical": ProactivePriority.CRITICAL,
            "warning": ProactivePriority.HIGH,
            "recovery": ProactivePriority.NORMAL,
        }.get(severity, ProactivePriority.LOW)
        incident = f"sentinel:{entity.casefold()}"
        return _candidate(
            event, source, entity, "sentinel_alert", message, priority,
            .98 if priority == ProactivePriority.CRITICAL else .75,
            .98 if priority == ProactivePriority.CRITICAL else .65,
            baseline=baseline,
            recovery_of=incident if severity == "recovery" else None,
            opens_incident=incident if severity in {"warning", "critical"} else None,
        )

    if event_type in {EventType.TASK_STATE_CHANGED, EventType.TASK_FINISHED}:
        state = str(payload.get("state") or "").upper()
        if state not in {"SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}:
            return None
        entity = str(payload.get("task_id") or "task")[:240]
        objective = _objective(payload, linked_loop, "a tarefa")
        if state in {"SUCCEEDED", "COMPLETED"}:
            return _candidate(event, source, entity, "task_terminal", f"Terminei {objective}.",
                              ProactivePriority.NORMAL, .72, .45, goal_id=goal_id, loop_id=loop_id)
        if state in {"FAILED", "BLOCKED"}:
            reason = str(payload.get("failure_reason") or payload.get("reason") or "uma etapa não pôde ser concluída")[:180]
            return _candidate(event, source, entity, "task_terminal", f"{_sentence(objective)} parou porque {reason}.",
                              ProactivePriority.HIGH, .88, .75, goal_id=goal_id, loop_id=loop_id)
        return _candidate(event, source, entity, "task_terminal", f"{_sentence(objective)} foi cancelada.",
                          ProactivePriority.LOW, .3, .2, baseline="LOG_ONLY", goal_id=goal_id, loop_id=loop_id)

    if event_type == EventType.AGENT_RUN_FINISHED:
        entity = str(payload.get("agent_run_id") or "agent-run")[:240]
        objective = _objective(payload, linked_loop, "a atividade")
        state = str(payload.get("state") or payload.get("status") or "").upper()
        failed = state in {"FAILED", "BLOCKED", "ERROR"}
        return _candidate(event, source, entity, "agent_run_terminal",
                          f"{_sentence(objective)} falhou." if failed else f"Terminei {objective}.",
                          ProactivePriority.HIGH if failed else ProactivePriority.NORMAL,
                          .85 if failed else .7, .7 if failed else .4,
                          goal_id=goal_id, loop_id=loop_id)
    if event_type in {EventType.JOB_FINISHED, EventType.WORKFLOW_FINISHED}:
        entity = _label(payload, "job_id", "run_id", "workflow_id", default="atividade")
        success = payload.get("success") is not False and str(payload.get("state") or "").upper() not in {"FAILED", "ERROR"}
        label = "O workflow" if event_type == EventType.WORKFLOW_FINISHED else "O job"
        return _candidate(event, source, entity, "operator_terminal",
                          f"{label} terminou." if success else f"{label} falhou.",
                          ProactivePriority.NORMAL if success else ProactivePriority.HIGH,
                          .65 if success else .85, .4 if success else .72)

    if event_type in {EventType.MONITOR_JOB_COMPLETED, EventType.MONITOR_JOB_FAILED, EventType.MONITOR_JOB_CHANGED}:
        entity = str(payload.get("monitor_id") or "monitor")[:240]
        objective = _objective(payload, linked_loop, "a condição acompanhada")
        if event_type == EventType.MONITOR_JOB_FAILED:
            message = f"Não consegui continuar acompanhando {objective}."
            return _candidate(event, source, entity, "monitor_terminal", message,
                              ProactivePriority.HIGH, .88, .78, goal_id=goal_id, loop_id=loop_id)
        if event_type == EventType.MONITOR_JOB_CHANGED:
            message = f"Houve uma mudança relevante em {objective}."
            return _candidate(event, source, entity, "monitor_change", message,
                              ProactivePriority.NORMAL, .62, .4, goal_id=goal_id, loop_id=loop_id)
        reason = str(payload.get("completion_reason") or "").upper()
        if reason == "CONDITION_MET":
            if linked_loop is not None and str(getattr(linked_loop, "state", "")).upper().endswith("ACTIVE"):
                title = str(getattr(linked_loop, "title", objective)).strip().rstrip(".")
                continuation = title if title.casefold().startswith("continuar ") else f"continuar {title}"
                message = f"A condição aguardada foi atendida. Posso {continuation}."
            else:
                message = f"A condição aguardada para {objective} foi atendida."
            return _candidate(event, source, entity, "monitor_terminal", message,
                              ProactivePriority.NORMAL, .78, .58, goal_id=goal_id, loop_id=loop_id,
                              voice_requested=bool(payload.get("voice")))
        return _candidate(event, source, entity, "monitor_terminal",
                          f"O acompanhamento de {objective} terminou sem atingir a condição.",
                          ProactivePriority.NORMAL, .62, .4, goal_id=goal_id, loop_id=loop_id)

    if event_type in {EventType.OPEN_LOOP_CREATED, EventType.OPEN_LOOP_RESOLVED}:
        entity = str(payload.get("loop_id") or loop_id or "open-loop")[:240]
        title = str(getattr(linked_loop, "title", "uma pendência") or "uma pendência")[:240]
        return _candidate(event, source, entity, "open_loop_lifecycle", f"Estado atualizado para {title}.",
                          ProactivePriority.LOW, .3, .15, baseline="LOG_ONLY",
                          goal_id=goal_id, loop_id=loop_id)
    if event_type == EventType.OPEN_LOOP_STATE_CHANGED:
        state = str(payload.get("state") or "").upper()
        if state != "ACTIVE" or linked_loop is None:
            return None
        waiting = getattr(linked_loop, "waiting_for", None)
        monitors = getattr(linked_loop, "related_monitor", [])
        baseline = "EVALUATE" if waiting or monitors else "LOG_ONLY"
        title = str(getattr(linked_loop, "title", "uma pendência") or "uma pendência")[:240]
        return _candidate(event, source, str(payload.get("loop_id") or loop_id), "open_loop_actionable",
                          f"Já posso retomar: {title}.", ProactivePriority.NORMAL, .78, .55,
                          baseline=baseline, goal_id=goal_id, loop_id=loop_id)

    if event_type in _USB_EVENTS:
        device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
        entity = str(device.get("device_id") or payload.get("device_id") or "usb")[:240]
        name_value = str(device.get("friendly_name") or device.get("name") or "dispositivo USB")[:180]
        if event_type in {EventType.USB_DEVICE_CONNECTED, EventType.USB_DEVICE_KNOWN_CONNECTED}:
            return _candidate(event, source, entity, "usb_connected", f"{name_value} foi conectado.",
                              ProactivePriority.LOW, .15, .1, baseline="IGNORE")
        if event_type == EventType.USB_DEVICE_UNKNOWN:
            return _candidate(event, source, entity, "usb_unknown", f"Detectei um dispositivo USB desconhecido: {name_value}.",
                              ProactivePriority.HIGH, .82, .72)
        if event_type == EventType.USB_DEVICE_IDENTITY_CHANGED:
            return _candidate(event, source, entity, "usb_identity", f"A identidade de {name_value} não corresponde ao dispositivo conhecido.",
                              ProactivePriority.HIGH, .9, .82)
        if event_type == EventType.USB_DEVICE_COM_CHANGED:
            current = str(payload.get("current_com") or "outra porta")[:40]
            return _candidate(event, source, entity, "usb_port", f"{name_value} passou para {current}.",
                              ProactivePriority.NORMAL, .58, .35)
        return _candidate(event, source, entity, "usb_disconnected", f"{name_value} foi desconectado.",
                          ProactivePriority.NORMAL, .55, .3)
    if event_type == EventType.USB_MONITOR_FAILURE:
        return _candidate(event, source, "usb-monitor", "usb_monitor_failure",
                          "O monitor de dispositivos USB entrou em modo degradado.",
                          ProactivePriority.HIGH, .8, .65)

    if event_type == EventType.ARTIFACT_CONTEXT_UPDATED:
        raw = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
        path = str(raw.get("path") or "")[:1200]
        name_value = Path(path).name[:180] if path else "O artefato"
        baseline = "EVALUATE" if linked_loop is not None else "LOG_ONLY"
        return _candidate(event, source, str(raw.get("artifact_id") or path or "artifact"),
                          "artifact_ready", f"O artefato {name_value} ficou pronto.",
                          ProactivePriority.NORMAL, .62, .35, baseline=baseline,
                          goal_id=goal_id, loop_id=loop_id)

    if event_type in _SELFDEV_EVENTS:
        issue = str(payload.get("issue_id") or "selfdev")[:240]
        if event_type in {EventType.SELFDEV_VALIDATION_FAIL, EventType.SELFDEV_ROLLBACK}:
            return _candidate(event, source, issue, "selfdev_validation",
                              "A validação do SelfDev encontrou um problema.",
                              ProactivePriority.HIGH, .82, .65)
        if event_type == EventType.SELFDEV_PROMOTION_APPLIED:
            return _candidate(event, source, issue, "selfdev_promotion",
                              "O SelfDev aplicou uma promoção validada.",
                              ProactivePriority.NORMAL, .7, .4)
        return _candidate(event, source, issue, "selfdev_validation",
                          "O SelfDev terminou a validação.",
                          ProactivePriority.NORMAL, .68, .4)

    if event_type in {EventType.RUNTIME_FAILED, EventType.RUNTIME_CRASH_LOOP, EventType.RUNTIME_RECOVERED}:
        entity = _label(payload, "service_id", "service", default="um serviço da NYRA")
        incident = f"runtime:{entity.casefold()}"
        if event_type == EventType.RUNTIME_CRASH_LOOP:
            return _candidate(event, source, entity, "runtime_failure",
                              f"{_sentence(entity)} entrou em um ciclo de falhas.",
                              ProactivePriority.CRITICAL, .98, .98, opens_incident=incident)
        if event_type == EventType.RUNTIME_FAILED:
            return _candidate(event, source, entity, "runtime_failure", f"{_sentence(entity)} falhou.",
                              ProactivePriority.HIGH, .88, .8, opens_incident=incident)
        return _candidate(event, source, entity, "runtime_recovered", f"{_sentence(entity)} voltou a responder.",
                          ProactivePriority.NORMAL, .65, .4, recovery_of=incident)

    if event_type in {EventType.COMPUTER_OPERATOR_FAILURE, EventType.COMPUTER_VERIFICATION_FAILURE}:
        operation = _label(payload, "operation", "tool", default="A operação")
        return _candidate(event, source, operation, "operator_failure",
                          f"{_sentence(operation)} não pôde ser confirmada.",
                          ProactivePriority.HIGH, .82, .68)
    if event_type == EventType.COMPUTER_EFFECT_VERIFIED:
        operation = _label(payload, "operation", "tool", default="A operação")
        return _candidate(event, source, operation, "operator_verified",
                          f"{_sentence(operation)} foi concluída e verificada.",
                          ProactivePriority.LOW, .35, .2, baseline="LOG_ONLY")
    return None


_NETWORK_DOWN = {
    EventType.NETWORK_GATEWAY_DOWN, EventType.NETWORK_INTERNET_DOWN,
    EventType.NETWORK_DNS_FAILURE, EventType.NETWORK_LINK_DOWN,
}
_NETWORK_RECOVERY = {
    EventType.NETWORK_GATEWAY_RECOVERED, EventType.NETWORK_INTERNET_RECOVERED,
    EventType.NETWORK_DNS_RECOVERED, EventType.NETWORK_LINK_UP,
    EventType.NETWORK_RECOVERED, EventType.NETWORK_LATENCY_RECOVERED,
    EventType.NETWORK_PACKET_LOSS_RECOVERED, EventType.NETWORK_JITTER_RECOVERED,
}
_NETWORK_QUALITY = {
    EventType.NETWORK_HIGH_LATENCY, EventType.NETWORK_PACKET_LOSS,
    EventType.NETWORK_HIGH_JITTER,
}
_USB_EVENTS = {
    EventType.USB_DEVICE_CONNECTED, EventType.USB_DEVICE_KNOWN_CONNECTED,
    EventType.USB_DEVICE_UNKNOWN, EventType.USB_DEVICE_IDENTITY_CHANGED,
    EventType.USB_DEVICE_COM_CHANGED, EventType.USB_DEVICE_DISCONNECTED,
}
_SELFDEV_EVENTS = {
    EventType.SELFDEV_VALIDATION_PASS, EventType.SELFDEV_VALIDATION_FAIL,
    EventType.SELFDEV_POST_VALIDATION_PASS, EventType.SELFDEV_PROMOTION_APPLIED,
    EventType.SELFDEV_ROLLBACK,
}


def _candidate(
    event: Event, source: str, entity: str, family: str, message: str,
    priority: ProactivePriority, impact: float, urgency: float, *,
    baseline: str = "EVALUATE", goal_id: str | None = None,
    loop_id: str | None = None, recovery_of: str | None = None,
    opens_incident: str | None = None, voice_requested: bool = False,
) -> ProactiveCandidate:
    return ProactiveCandidate(
        event_id=event.id, event_type=event.type.value, source=source,
        entity=(entity or "system")[:240], goal_id=goal_id,
        open_loop_id=loop_id, message=" ".join(message.split())[:500],
        priority=priority, impact=impact, urgency=urgency,
        confidence=float(event.payload.get("confidence", 1.0)) if isinstance(event.payload, dict) else 1.0,
        semantic_family=family, baseline=baseline,
        recovery_of=recovery_of, opens_incident=opens_incident,
        voice_requested=voice_requested, occurred_at=event.timestamp,
    )


def _source(event_type: EventType) -> str:
    name = event_type.value.casefold()
    if name.startswith("network_"):
        return "network_watch"
    if name.startswith("usb.") or name.startswith("usb_"):
        return "usb_monitor"
    if name.startswith("selfdev."):
        return "selfdev"
    if name.startswith("monitor_job"):
        return "monitor_job"
    if name.startswith("open_loop"):
        return "open_loops"
    if name.startswith("proxmox"):
        return "proxmox"
    if name.startswith("home_assistant"):
        return "home_assistant"
    if name.startswith("homelab"):
        return "homelab"
    if name.startswith("sentinel"):
        return "sentinel"
    if name.startswith("artifact"):
        return "artifact"
    if name.startswith("runtime"):
        return "runtime_supervisor"
    if name.startswith("computer"):
        return "operator"
    return "tasks"


def _network_entity(event_type: EventType, payload: dict[str, Any]) -> str:
    explicit = _label(payload, "target", "interface", "interface_name", default="")
    if explicit:
        return explicit
    if "GATEWAY" in event_type.value:
        return "gateway"
    if "DNS" in event_type.value:
        return "DNS"
    if "LINK" in event_type.value:
        return "interface de rede"
    return "internet"


def _network_down_message(event_type: EventType, entity: str) -> str:
    if "DNS" in event_type.value:
        return "A resolução DNS parou de responder."
    if "GATEWAY" in event_type.value:
        return "O gateway da rede ficou indisponível."
    if "LINK" in event_type.value:
        return f"A {entity} perdeu o link."
    return "A conexão com a internet caiu."


def _network_recovery_message(event_type: EventType, entity: str) -> str:
    if "DNS" in event_type.value:
        return "A resolução DNS normalizou."
    if "GATEWAY" in event_type.value:
        return "O gateway da rede voltou a responder."
    if "LINK" in event_type.value:
        return f"A {entity} recuperou o link."
    return "A conexão normalizou."


def _message(payload: dict[str, Any], fallback: str) -> str:
    value = str(payload.get("message") or fallback)
    return " ".join(value.split())[:500]


def _label(payload: dict[str, Any], *keys: str, default: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:240]
    return default


def _objective(payload: dict[str, Any], linked_loop: Any, default: str) -> str:
    value = str(payload.get("objective") or payload.get("goal") or getattr(linked_loop, "title", "") or default)
    return value.strip().rstrip(".")[:240]


def _sentence(value: str) -> str:
    cleaned = value.strip().rstrip(".")
    return cleaned[:1].upper() + cleaned[1:] if cleaned else "A atividade"
