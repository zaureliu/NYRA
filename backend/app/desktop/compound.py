"""Deterministic compound app plans and their sequential desktop executor.

Plans in this module originate only from the raw operator utterance.  They do
not accept LLM-authored JSON or arbitrary tool names.  A plan is consumed once
per turn and every step carries observable verification evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.desktop.discovery import normalize
from app.desktop.models import operation_result


@dataclass(frozen=True)
class CompoundStepSpec:
    capability: str
    target: str = ""
    arguments: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CompoundPlanSpec:
    target: str
    steps: tuple[CompoundStepSpec, ...]
    contextual: bool = False
    explicit_new: bool = False
    final_action: str = ""
    confidence: float = 1.0


_NEXT_ACTION = re.compile(
    r"\s+e\s+(?=(?:escrev|digit|envi|mand|pesquis|busc|maximiz|minimiz|"
    r"restaur|traz|coloc|foc|abre|abra)\w*\b)",
    re.IGNORECASE,
)
_LEADING_CONVERSATION = re.compile(
    r"^(?:kazumi|ei\s+kazumi|oi\s+kazumi)\s*[,!:.]?\s*", re.IGNORECASE,
)
_TRAILING_CONTEXT = re.compile(
    r"\s+(?:nele|nela|nisso|ali|no\s+canal\s+aberto|na\s+janela\s+aberta|"
    r"no\s+arquivo\s+aberto)\s*$",
    re.IGNORECASE,
)
_QUOTED_TEXT = re.compile(r"[\"'“‘](.*?)[\"'”’]", re.DOTALL)


def _argument_after_verb(value: str, verb_pattern: str) -> str:
    match = re.match(rf"^(?:{verb_pattern})\w*\s+(?P<argument>.+)$", value, re.IGNORECASE)
    if not match:
        return ""
    raw = match.group("argument").strip()
    quoted = _QUOTED_TEXT.search(raw)
    if quoted:
        return " ".join(quoted.group(1).split())
    raw = _TRAILING_CONTEXT.sub("", raw).strip()
    raw = re.sub(r"^(?:por\s+)", "", raw, flags=re.IGNORECASE)
    return " ".join(raw.strip(" \"'“”‘’").split())


def _followup_step(value: str) -> CompoundStepSpec | None:
    clean = value.strip(" ,.!?")
    lowered = clean.casefold()
    if re.match(r"^(?:escrev|digit)", lowered):
        text = _argument_after_verb(clean, r"escrev|digit")
        return CompoundStepSpec("type_text", arguments={"text": text}) if text else None
    if re.match(r"^(?:envi|mand)", lowered):
        text = _argument_after_verb(clean, r"envi|mand")
        return CompoundStepSpec("send_text", arguments={"text": text}) if text else None
    if re.match(r"^(?:pesquis|busc)", lowered):
        query = _argument_after_verb(clean, r"pesquis|busc")
        return CompoundStepSpec("search", arguments={"query": query}) if query else None
    if re.match(r"^maximiz", lowered):
        return CompoundStepSpec("maximize")
    if re.match(r"^minimiz", lowered):
        return CompoundStepSpec("minimize")
    if re.match(r"^(?:restaur|desminimiz)", lowered):
        return CompoundStepSpec("restore")
    if re.match(r"^(?:traz|coloc|foc)", lowered):
        return CompoundStepSpec("focus")
    if re.match(r"^(?:abre|abra)\b", lowered):
        from app.desktop.intents import parse_universal_intent, UniversalAction

        parsed = parse_universal_intent(clean)
        if parsed and parsed.action == UniversalAction.OPEN_FOLDER:
            return CompoundStepSpec("open_folder", target=parsed.target)
        # Inside an already opened Explorer, "abre Downloads" is a folder
        # navigation request even when the short parser cannot classify it.
        target = re.sub(r"^(?:abre|abra)\s+(?:a\s+|o\s+)?", "", clean,
                        count=1, flags=re.IGNORECASE).strip()
        return CompoundStepSpec("open_folder", target=target) if target else None
    return None


def parse_compound_intent(text: str) -> CompoundPlanSpec | None:
    """Parse generic OPEN+ACTION or contextual ACTION commands.

    Application names are deliberately opaque here.  Canonical resolution is
    performed once by the registry during execution.
    """

    value = " ".join((text or "").strip().split())
    if not value or len(value) > 2200:
        return None
    value = _LEADING_CONVERSATION.sub("", value, count=1)

    boundary = _NEXT_ACTION.search(value)
    if boundary:
        first = value[:boundary.start()].strip(" ,")
        tail = value[boundary.end():].strip()
        from app.desktop.intents import UniversalAction, parse_universal_intent

        opened = parse_universal_intent(first)
        followup = _followup_step(tail)
        if opened is None or opened.action != UniversalAction.OPEN_APP or followup is None:
            return None
        steps = (
            CompoundStepSpec(
                "open_or_focus", target=opened.target,
                arguments={"force_new": "true" if opened.explicit_new else "false"},
            ),
            CompoundStepSpec("wait_for_ready", target=opened.target),
            followup,
        )
        return CompoundPlanSpec(
            target=opened.target,
            steps=steps,
            explicit_new=opened.explicit_new,
            final_action=followup.capability,
        )

    contextual_value = re.sub(r"^agora\s+", "", value, count=1, flags=re.IGNORECASE)
    followup = _followup_step(contextual_value)
    if followup is None or followup.capability not in {"type_text", "send_text"}:
        return None
    explicitly_contextual = bool(
        re.search(r"\b(?:nele|nela|nisso|ali|arquivo\s+aberto|canal\s+aberto)\b",
                  contextual_value, re.IGNORECASE)
        or contextual_value != value
    )
    if not explicitly_contextual:
        return None
    return CompoundPlanSpec(
        target="",
        steps=(CompoundStepSpec("wait_for_ready"), followup),
        contextual=True,
        final_action=followup.capability,
        confidence=0.9,
    )


@dataclass
class ActionContext:
    target_query: str
    display_name: str
    canonical_id: str = ""
    hwnd: int | None = None
    process_names: tuple[str, ...] = ()
    app_was_open: bool = False


def _element_criteria(element: dict[str, Any]) -> dict[str, str]:
    if element.get("automation_id"):
        return {"automation_id": str(element["automation_id"])}
    if element.get("name"):
        return {
            "name": str(element["name"]),
            "control_type": str(element.get("control_type") or "Edit"),
        }
    return {"control_type": str(element.get("control_type") or "Edit")}


def _rank_editable(element: dict[str, Any], *, search: bool) -> tuple[int, int, int]:
    name = normalize(str(element.get("name") or ""))
    search_words = ("address", "search", "pesquisa", "endereco", "omnibox", "url")
    message_words = ("message", "mensagem", "composer", "chat", "digite", "escreva")
    document_words = ("document", "documento", "editor", "text", "texto")
    preferred = search_words if search else message_words
    semantic = 4 if any(word in name for word in preferred) else (
        2 if any(word in name for word in document_words) else 0
    )
    rect = element.get("rect") or {}
    bottom = int(rect.get("y") or 0)
    enabled = 1 if element.get("enabled", True) else 0
    return (semantic, enabled, bottom if not search else -bottom)


class CompoundActionExecutor:
    """Single-owner PLAN -> ACT -> VERIFY executor for local app sequences."""

    def __init__(self, controller) -> None:
        self.controller = controller
        self._consumed: dict[str, dict[str, Any]] = {}

    async def execute(self, intent, *, turn_id: str | None = None) -> dict[str, Any]:
        fingerprint = hashlib.sha256(
            (f"{turn_id}|{intent.intent_id}|" + "|".join(
                f"{step.capability}:{step.target}:{sorted(step.arguments.items())}"
                for step in intent.plan
            )).encode("utf-8")
        ).hexdigest()
        if turn_id and fingerprint in self._consumed:
            return self._consumed[fingerprint]

        started = time.perf_counter()
        context = ActionContext(
            intent.target,
            intent.target,
            hwnd=(getattr(intent.resolved, "hwnd", None) if intent.resolved else None),
            process_names=(tuple(intent.resolved.process_names) if intent.resolved else ()),
        )
        steps: list[dict[str, Any]] = []
        opened = False
        failure_message = ""

        for item in intent.plan:
            capability = item.capability
            if capability == "open_or_focus":
                result = await self.controller.launch_dynamic(
                    item.target or context.target_query,
                    origin="compound_fastpath",
                    force_new=item.arguments.get("force_new") == "true",
                )
                candidate = result.get("candidate") or {}
                context.display_name = str(candidate.get("display_name") or result.get("app") or context.target_query)
                context.canonical_id = str(candidate.get("id") or "")
                context.process_names = tuple(candidate.get("process_names") or ())
                context.app_was_open = bool(result.get("already_open"))
                context.hwnd = self._hwnd_from(result)
                ok = bool(result.get("success")) and result.get("effect_verified") is True
                steps.append({"step": capability, "ok": ok, "result": result})
                if not ok:
                    failure_message = f"Não consegui abrir {context.display_name}."
                    break
                opened = True
                continue

            if capability == "wait_for_ready":
                ready = await self._wait_for_ready(context)
                steps.append({"step": capability, "ok": ready, "hwnd": context.hwnd})
                if not ready:
                    prefix = f"Abri {context.display_name}, mas" if opened else ""
                    failure_message = (
                        f"{prefix} não consegui confirmar que a janela está pronta."
                        if prefix else f"Não consegui localizar a janela do {context.display_name}."
                    )
                    break
                continue

            if capability in {"type_text", "send_text"}:
                text = item.arguments.get("text", "")
                action_result = await self._type_or_send(context, text, send=capability == "send_text")
                steps.append({"step": capability, "ok": action_result["ok"],
                              "result": action_result["evidence"]})
                if not action_result["ok"]:
                    prefix = f"Abri {context.display_name}, mas" if opened else ""
                    failure_message = (
                        f"{prefix} não consegui localizar ou confirmar o campo de texto."
                        if prefix else f"Não consegui inserir o texto no {context.display_name}."
                    )
                    break
                continue

            if capability == "search":
                query = item.arguments.get("query", "")
                action_result = await self._search(context, query)
                steps.append({"step": capability, "ok": action_result["ok"],
                              "result": action_result["evidence"]})
                if not action_result["ok"]:
                    failure_message = f"Abri {context.display_name}, mas não consegui confirmar a pesquisa."
                    break
                continue

            if capability in {"focus", "minimize", "maximize", "restore"}:
                action_result = await self._window_action(context, capability)
                steps.append({"step": capability, "ok": action_result})
                if not action_result:
                    failure_message = f"Abri {context.display_name}, mas não consegui {self._verb(capability)} a janela."
                    break
                continue

            if capability == "open_folder":
                handled, reply = await self.controller._universal_open_folder(item.target)
                result = self.controller.last_operation_result or {}
                ok = bool(handled and result.get("effect_verified") is True)
                steps.append({"step": capability, "ok": ok, "result": result})
                if not ok:
                    failure_message = reply
                    break
                continue

            steps.append({"step": capability, "ok": False, "error": "UNSUPPORTED_STEP"})
            failure_message = "Não consegui concluir a sequência solicitada."
            break

        success = bool(steps) and all(step.get("ok") for step in steps)
        message = self._success_message(intent, context) if success else failure_message
        payload = operation_result(
            success=success,
            app=context.display_name,
            action="compound_app_action",
            message=message,
            execution_success=success,
            effect_verified=success,
            verification_status="VERIFIED" if success else "VERIFICATION_FAILED",
            duration_ms=(time.perf_counter() - started) * 1000,
            steps=steps,
            canonical_id=context.canonical_id,
            hwnd=context.hwnd,
            app_was_open=context.app_was_open,
            remote_shell_calls=0,
            agent_run_calls=0,
        )
        payload["user_facing_response"] = message
        self.controller.last_operation_result = payload
        if success:
            self.controller._note_controlled(
                context.display_name,
                kind="app",
                process_names=context.process_names,
                title_tokens=(context.display_name,),
                hwnd=context.hwnd,
                canonical_id=context.canonical_id,
            )
        if turn_id:
            if len(self._consumed) > 200:
                for stale in list(self._consumed)[:100]:
                    self._consumed.pop(stale, None)
            self._consumed[fingerprint] = payload
        return payload

    @staticmethod
    def _hwnd_from(result: dict[str, Any]) -> int | None:
        windows = result.get("windows") or []
        if windows and isinstance(windows[0], dict):
            try:
                return int(windows[0].get("hwnd") or 0) or None
            except (TypeError, ValueError):
                return None
        return None

    async def _wait_for_ready(self, context: ActionContext, timeout: float = 12.0) -> bool:
        from app.desktop import window_manager as wm

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if context.hwnd:
                state = await asyncio.to_thread(wm.window_state, context.hwnd)
                if state.get("alive") and state.get("visible"):
                    if state.get("iconic"):
                        await asyncio.to_thread(wm.restore_window, context.hwnd)
                    return True
            windows = self.controller._resolve_window_targets(
                context.display_name or context.target_query,
                hints={"process_names": list(context.process_names),
                       "title_tokens": [context.display_name]},
            )
            if windows:
                context.hwnd = windows[0].hwnd
                if await asyncio.to_thread(wm.focus_window, context.hwnd):
                    return True
            await asyncio.sleep(0.2)
        return False

    async def _editable(self, hwnd: int, *, search: bool = False) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        from app.desktop import uia

        evidence = await self.controller._uia_call(
            uia.find_in_window, hwnd, control_type="Edit", limit=40,
        )
        elements = [item for item in (evidence.get("elements") or []) if item.get("enabled", True)]
        if not elements:
            return None, evidence
        elements.sort(key=lambda item: _rank_editable(item, search=search), reverse=True)
        return elements[0], evidence

    async def _type_or_send(self, context: ActionContext, text: str, *, send: bool) -> dict[str, Any]:
        from app.desktop import uia, window_manager as wm

        if not context.hwnd or not text or len(text) > 2000:
            return {"ok": False, "evidence": {"error": "INVALID_TARGET_OR_TEXT"}}
        if not await asyncio.to_thread(wm.focus_window, context.hwnd):
            return {"ok": False, "evidence": {"error": "FOCUS_NOT_CONFIRMED"}}
        element, found = await self._editable(context.hwnd)
        if element is None:
            from app.desktop.visual_fallback import type_on_visual_surface

            visual = await type_on_visual_surface(
                context.hwnd, text, send=send,
            )
            return {
                "ok": bool(visual.get("success") and visual.get("effect_verified") is True),
                "evidence": {"uia": found, "visual_fallback": visual},
            }
        criteria = _element_criteria(element)
        before = element.get("value")
        evidence: dict[str, Any] = {"target": element, "before": before}

        if before is not None:
            intended = f"{before}{text}"
            typed = await self.controller._uia_call(
                uia.set_text, context.hwnd, intended, **criteria,
            )
        else:
            clicked = await self.controller._uia_call(
                uia.click_element, context.hwnd, **criteria,
            )
            if not clicked.get("success"):
                return {"ok": False, "evidence": {**evidence, "click": clicked}}
            typed = await self.controller._uia_call(
                uia.send_keys_to_foreground, text, context.hwnd,
                interpret_sequences=False,
            )
        evidence["typed"] = typed
        if not typed.get("success") or typed.get("effect_verified") is False:
            return {"ok": False, "evidence": evidence}

        readback = await self.controller._uia_call(
            uia.get_text, context.hwnd, **criteria,
        )
        current = readback.get("value")
        evidence["readback"] = readback
        typed_verified = bool(
            typed.get("effect_verified") is True
            or (isinstance(current, str) and text in current and current != before)
        )
        if not typed_verified:
            return {"ok": False, "evidence": evidence}
        if not send:
            return {"ok": True, "evidence": evidence}

        submitted = await self.controller._uia_call(
            uia.send_keys_to_foreground, "{enter}", context.hwnd,
        )
        evidence["submitted"] = submitted
        if not submitted.get("success"):
            return {"ok": False, "evidence": evidence}
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            after = await self.controller._uia_call(
                uia.get_text, context.hwnd, **criteria,
            )
            after_value = after.get("value")
            evidence["after_submit"] = after
            if isinstance(after_value, str) and text not in after_value:
                return {"ok": True, "evidence": evidence}
            await asyncio.sleep(0.15)
        return {"ok": False, "evidence": evidence}

    async def _search(self, context: ActionContext, query: str) -> dict[str, Any]:
        from app.desktop import uia, window_manager as wm

        if not context.hwnd or not query or len(query) > 500:
            return {"ok": False, "evidence": {"error": "INVALID_TARGET_OR_QUERY"}}
        if not await asyncio.to_thread(wm.focus_window, context.hwnd):
            return {"ok": False, "evidence": {"error": "FOCUS_NOT_CONFIRMED"}}
        select = await self.controller._uia_call(
            uia.send_keys_to_foreground, "{ctrl+l}", context.hwnd,
        )
        element, found = await self._editable(context.hwnd, search=True)
        evidence: dict[str, Any] = {"select_search": select, "discovery": found}
        if element is None:
            return {"ok": False, "evidence": evidence}
        criteria = _element_criteria(element)
        typed = await self.controller._uia_call(
            uia.set_text, context.hwnd, query, **criteria,
        )
        evidence["typed"] = typed
        if not typed.get("success") or typed.get("effect_verified") is not True:
            return {"ok": False, "evidence": evidence}
        submitted = await self.controller._uia_call(
            uia.send_keys_to_foreground, "{enter}", context.hwnd,
        )
        evidence["submitted"] = submitted
        if not submitted.get("success"):
            return {"ok": False, "evidence": evidence}
        # Submission is verified by observing that the address/search value
        # moved away from the exact draft query, or that the owned window stays
        # alive after navigation if the provider masks the value.
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            readback = await self.controller._uia_call(
                uia.get_text, context.hwnd, **criteria,
            )
            value = readback.get("value")
            evidence["after_submit"] = readback
            if isinstance(value, str) and value and normalize(value) != normalize(query):
                return {"ok": True, "evidence": evidence}
            await asyncio.sleep(0.25)
        return {"ok": False, "evidence": evidence}

    async def _window_action(self, context: ActionContext, action: str) -> bool:
        from app.desktop import window_manager as wm

        if not context.hwnd:
            return False
        fn = {
            "focus": wm.focus_window,
            "minimize": wm.minimize_window,
            "maximize": wm.maximize_window,
            "restore": wm.restore_window,
        }[action]
        return bool(await asyncio.to_thread(fn, context.hwnd))

    @staticmethod
    def _verb(action: str) -> str:
        return {"focus": "trazer", "minimize": "minimizar",
                "maximize": "maximizar", "restore": "restaurar"}.get(action, action)

    @staticmethod
    def _success_message(intent, context: ActionContext) -> str:
        last = intent.plan[-1]
        if last.capability == "type_text":
            return f"Pronto. Escrevi {last.arguments.get('text', '')} no {context.display_name}."
        if last.capability == "send_text":
            return f"Pronto. Enviei {last.arguments.get('text', '')} no {context.display_name}."
        if last.capability == "search":
            return f"Pesquisei por {last.arguments.get('query', '')} no {context.display_name}."
        if last.capability == "maximize":
            return f"{context.display_name} aberto e maximizado."
        if last.capability == "minimize":
            return f"{context.display_name} aberto e minimizado."
        if last.capability == "restore":
            return f"{context.display_name} aberto e restaurado."
        if last.capability == "focus":
            return f"{context.display_name} em primeiro plano."
        if last.capability == "open_folder":
            return f"Pronto. Abri {last.target}."
        return "Pronto."
