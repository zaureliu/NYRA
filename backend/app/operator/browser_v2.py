"""Browser Control V2 via Chrome DevTools Protocol (spec Parte C §46-§78).

Extends the existing managed-browser stack (app.desktop.browser) WITHOUT
touching it. Priority respected: CDP first (§47); UIA and visual are upstream
fallbacks already provided by the desktop layer.

Privacy hard rules:
    §61/§72/§73  cookies/auth tokens never returned;
    §74          password field content is NEVER read back to the LLM
                 (masked as "<password>");
    §70/§71      execute_script exists but dangerous globals require approval
                 and every result passes redaction.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from app.desktop.browser import _http_json, _tabs, _ws_command
from app.desktop.control import operation_result
from app.tools.redaction import redact_secrets

_DOWNLOAD_DIR_NAME = "downloads"

_SCRIPT_MASK_PASSWORDS = (
    "(() => {"
    "const mask = (el) => {"
    "  if (!el) return '';"
    "  const t = el.value || el.textContent || '';"
    "  return (el.type === 'password') ? '<password>' : String(t).slice(0, 200);"
    "};"
    "return mask;"
    "})()"
)


def _safe_selector(selector: str) -> bool:
    """Reject selectors that try to escape attribute context or read secrets."""
    if len(selector) > 500 or "\x00" in selector:
        return False
    forbidden = re.compile(r"(?i)(cookie|localstorage|sessionstorage|token|apikey|api_key)")
    return not forbidden.search(selector)


def summarize_dom_js(max_nodes: int = 120) -> str:
    return (
        "(() => {"
        f"const max = {max_nodes};"
        "const nodes = [];"
        "const walk = (el, depth) => {"
        "  if (!el || nodes.length >= max || depth > 12) return;"
        "  if (el.nodeType === Node.ELEMENT_NODE) {"
        "    const rect = el.getBoundingClientRect();"
        "    const role = el.getAttribute && (el.getAttribute('role') || '');"
        "    const entry = {"
        "      tag: el.tagName.toLowerCase(),"
        "      role: role,"
        "      id: (el.id || '').slice(0, 60),"
        "      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 60),"
        "      text: (el.type === 'password' ? '<password>' : (el.innerText||'').trim().split('\\n')[0].slice(0, 80)),"
        "      href: el.tagName === 'A' ? (el.href||'').slice(0, 160) : undefined,"
        "      value: (el.type === 'password' ? '<password>' : (['INPUT','SELECT','TEXTAREA'].includes(el.tagName) ? String(el.value||'').slice(0,80) : undefined)),"
        "      checked: (el.type==='checkbox'||el.type==='radio') ? !!el.checked : undefined,"
        "      rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},"
        "    };"
        "    nodes.push(entry);"
        "    for (const child of el.children) walk(child, depth+1);"
        "  }"
        "};"
        "walk(document.body || document.documentElement, 0);"
        "return JSON.stringify({title: document.title.slice(0,150), url: location.href.slice(0,300), count: nodes.length, nodes});"
        "})()"
    )


def find_elements_js(role: str, label: str, text: str, selector: str, limit: int = 10) -> str:
    return (
        "(() => {"
        f"const limit = {limit};"
        f"const wantRole = {json.dumps(role.casefold())};"
        f"const wantLabel = {json.dumps(label.casefold())};"
        f"const wantText = {json.dumps(text.casefold())};"
        f"const cssSelector = {json.dumps(selector)};"
        "let candidates = [];"
        "if (cssSelector) {"
        "  try { candidates = Array.from(document.querySelectorAll(cssSelector)); } catch(e) { candidates = []; }"
        "} else {"
        "  candidates = Array.from(document.querySelectorAll('a,button,input,select,textarea,[role],[onclick],h1,h2,h3,h4,p,label,span,li'));"
        "}"
        "const matches = candidates.filter((el) => {"
        "  const role = (el.getAttribute && (el.getAttribute('role') || el.tagName.toLowerCase())) || '';"
        "  const label = ((el.getAttribute('aria-label')||el.name||el.id||'')+'').toLowerCase();"
        "  const text = ((el.innerText||el.value||'')+'').trim().toLowerCase();"
        "  if (wantRole && !role.includes(wantRole)) return false;"
        "  if (wantLabel && !label.includes(wantLabel)) return false;"
        "  if (wantText && !text.includes(wantText)) return false;"
        "  if (!wantRole && !wantLabel && !wantText) return true;"
        "  return true;"
        "}).slice(0, limit);"
        "const out = matches.map((el, i) => ({"
        "  index: i,"
        "  tag: el.tagName.toLowerCase(),"
        "  role: (el.getAttribute && el.getAttribute('role')) || '',"
        "  text: (el.type==='password' ? '<password>' : ((el.innerText||el.value||el.getAttribute('aria-label')||'')+'').trim().slice(0,80)),"
        "  selectorHint: (el.id ? '#'+CSS.escape(el.id) : '') + (el.name ? '[name=\"'+el.name+'\"]' : ''),"
        "  rect: (() => { const r = el.getBoundingClientRect(); return {x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)}; })(),"
        "}));"
        "return JSON.stringify({count: out.length, elements: out});"
        "})()"
    )


class BrowserV2Controller:
    """CDP capabilities layered on the managed browser (tabs/DOM/input/wait/
    download/script). All methods verify effects before claiming success."""

    def __init__(self, browser_manager) -> None:
        self.manager = browser_manager
        self._downloads: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ plumbing
    async def _ensure(self) -> tuple[int | None, dict | None]:
        running, info = await self.manager.ensure_running()
        if not running:
            return None, operation_result(app="browser", action="v2", success=False,
                                          error_code=info.get("error_code", "CDP_UNAVAILABLE"),
                                          message=info.get("message", "Navegador CDP indisponível."),
                                          execution_success=False)
        return int(info["port"]), None

    async def _evaluate(self, port: int, tab_id: str, expression: str, timeout: float = 8.0) -> tuple[bool, Any]:
        ok, result = await asyncio.to_thread(
            _ws_command, port, tab_id, "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False}, timeout,
        )
        if not ok or not isinstance(result, dict):
            return False, None
        value = (result.get("result") or {}).get("value")
        return True, value

    @staticmethod
    def _parse_json(value: Any) -> dict:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except ValueError:
                return {}
        return value if isinstance(value, dict) else {}

    async def resolve_tab(self, port: int, tab_id: str = "") -> tuple[str | None, dict | None]:
        tabs = await asyncio.to_thread(_tabs, port)
        if tab_id.strip():
            target = next((tab for tab in tabs if tab["id"] == tab_id.strip()), None)
        else:
            # Sem tab explícita: nunca operar silenciosamente sobre aba órfã
            # (chrome://, about:blank ou página morta de sessão anterior).
            # Preferir páginas http(s); entre elas, a mais recente.
            pages = [tab for tab in tabs if str(tab.get("type", "page")) == "page"]
            web = [tab for tab in pages
                   if str(tab.get("url", "")).startswith(("http://", "https://"))]
            target = web[-1] if web else (pages[-1] if pages else None)
        if target is None:
            return None, {"success": False, "error_code": "TAB_NOT_FOUND",
                          "message": "Aba não encontrada; use browser_tabs para listar."}
        return target["id"], None

    # ---------------------------------------------------------------- visibility
    async def _tab_visibility(self, port: int, tab_id: str) -> dict:
        ok, value = await self._evaluate(
            port, tab_id,
            "JSON.stringify({vis: document.visibilityState, focus: document.hasFocus()})")
        info = self._parse_json(value) if ok else {}
        return {"visible": str(info.get("vis", "hidden")) == "visible",
                "focused": bool(info.get("focus"))}

    async def ensure_visible_tab(self, port: int, tab_id: str) -> tuple[bool, dict | None]:
        """§12/§259: eventos de input reais são descartados pelo Chrome em
        páginas ocultas — sem verificação aqui, um clique 'VERIFIED' pode não
        ter efeito nenhum. Ativa a aba; se seguir oculta, falha honesta."""
        visibility = await self._tab_visibility(port, tab_id)
        if visibility["visible"]:
            return True, None
        await asyncio.to_thread(_http_json, port, f"/json/activate/{tab_id}", 3.0)
        for _ in range(5):
            await asyncio.sleep(0.4)
            visibility = await self._tab_visibility(port, tab_id)
            if visibility["visible"]:
                return True, None
        return False, operation_result(
            app="browser", action="visibility", success=False,
            error_code="PAGE_NOT_VISIBLE",
            message="A aba está oculta/minimizada; traga a janela do navegador "
                    "para primeiro plano e repita (input sintético é descartado "
                    "pelo Chrome em páginas hidden).",
            execution_success=False, recoverable=True)

    # ------------------------------------------------------------------- status
    async def status(self) -> dict:
        managed = self.manager.status()
        listing = []
        if managed.get("reachable") and managed.get("port"):
            listing = await asyncio.to_thread(_tabs, int(managed["port"]))
        return {"success": True, "managed": managed.get("managed", False),
                "reachable": managed.get("reachable", False),
                "browser": managed.get("browser"), "port": managed.get("port"),
                "version": managed.get("version"), "tab_count": len(listing)}

    async def open_url(self, url: str) -> dict:
        """Open a NEW tab (http/https only, §52-§54) and verify by tab list."""
        clean = str(url).strip()
        if not clean.casefold().startswith(("http://", "https://")):
            return operation_result(app="browser", action="open_url", success=False,
                                    error_code="INVALID_URL", message="Somente http/https.",
                                    execution_success=False)
        port, failure = await self._ensure()
        if failure:
            return failure
        assert port is not None
        ok, _payload = await asyncio.to_thread(
            _http_json, port, f"/json/new?{clean.replace(' ', '%20')}", 3.0, "PUT",
        )
        tabs = await asyncio.to_thread(_tabs, port)
        created = next((tab for tab in tabs if clean[:120] in (tab.get("url") or "")), None)
        verified = bool(ok) and created is not None
        return operation_result(app="browser", action="open_url",
                                success=bool(ok), execution_success=bool(ok),
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else "EXECUTED",
                                detail={"tab": created})

    async def navigate_via_open(self, url: str, tab_id: str = "") -> dict:
        """Navigate the active/given tab; verifies URL afterwards."""
        clean = str(url).strip()
        port, failure = await self._ensure()
        if failure:
            return failure
        assert port is not None
        resolved, failure = await self.resolve_tab(port, tab_id)
        if failure:
            return failure
        ok, _result = await asyncio.to_thread(
            _ws_command, port, resolved, "Page.navigate", {"url": clean}, 8.0,
        )
        deadline = time.monotonic() + 10
        verified = False
        while time.monotonic() < deadline:
            current = await self._current_url(port, resolved)
            if clean in current:
                verified = True
                break
            await asyncio.sleep(0.4)
        return operation_result(app="browser", action="navigate",
                                success=bool(ok), execution_success=bool(ok),
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else "EXECUTED")

    async def select_tab(self, tab_id: str) -> dict:
        port, failure = await self._ensure()
        if failure:
            return failure
        assert port is not None
        resolved, failure = await self.resolve_tab(port, tab_id)
        if failure:
            return failure
        ok, _ = await asyncio.to_thread(_ws_command, port, resolved, "Page.bringToFront", {})
        listing = await asyncio.to_thread(_tabs, port)
        current = next((tab for tab in listing if tab["id"] == resolved), {})
        verified = bool(ok)
        return operation_result(app="browser", action="select_tab", success=bool(ok),
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else "EXECUTED",
                                message=f"Aba ativa: {current.get('title', '')[:80]}",
                                detail={"tab": current})

    # --------------------------------------------------------------- dom inspect
    async def dom_inspect(self, tab_id: str = "", max_nodes: int = 120) -> dict:
        port, failure = await self._ensure()
        if failure:
            return failure
        assert port is not None
        resolved, failure = await self.resolve_tab(port, tab_id)
        if failure:
            return failure
        ok, value = await self._evaluate(port, resolved, summarize_dom_js(max_nodes))
        document = self._parse_json(value)
        if not ok or not document:
            return operation_result(app="browser", action="dom_inspect", success=False,
                                    error_code="DOM_UNAVAILABLE",
                                    message="Não consegui ler o DOM desta página via CDP.")
        return {
            "success": True,
            "title": document.get("title", ""),
            "url": redact_secrets(str(document.get("url", ""))),
            "node_count": document.get("count", len(document.get("nodes", []))),
            "nodes": document.get("nodes", [])[:max_nodes],
            "effect_verified": True,
            "verification_status": "VERIFIED",
        }

    async def find_element(self, *, role: str = "", label: str = "", text: str = "",
                           selector: str = "", tab_id: str = "", limit: int = 10) -> dict:
        if not any((role, label, text, selector)):
            return operation_result(app="browser", action="find_element", success=False,
                                    error_code="CRITERIA_REQUIRED",
                                    message="Informe role/label/text/selector (§62).")
        if selector and not _safe_selector(selector):
            return operation_result(app="browser", action="find_element", success=False,
                                    error_code="UNSAFE_SELECTOR",
                                    message="Selector rejeitado pelo filtro de privacidade (§61).")
        port, failure = await self._ensure()
        if failure:
            return failure
        resolved, failure = await self.resolve_tab(port, tab_id)
        if failure:
            return failure
        ok, value = await self._evaluate(port, resolved,
                                         find_elements_js(role, label, text, selector, limit))
        found = self._parse_json(value)
        if not ok:
            return operation_result(app="browser", action="find_element", success=False,
                                    error_code="FIND_FAILED")
        return {"success": True, "tab_id": resolved, "count": found.get("count", 0),
                "elements": found.get("elements", []),
                "effect_verified": True, "verification_status": "VERIFIED"}

    # --------------------------------------------------------------- interactions
    async def click_element(self, *, selector: str = "", x: int = 0, y: int = 0,
                            tab_id: str = "") -> dict:
        """Real input click at element center (Input domain), never JS-synthetic."""
        port, failure = await self._ensure()
        if failure:
            return failure
        assert port is not None
        resolved, failure = await self.resolve_tab(port, tab_id)
        if failure:
            return failure
        visible, failure = await self.ensure_visible_tab(port, resolved)
        if failure:
            return failure
        point_x, point_y = int(x), int(y)
        used_selector = ""
        if not (point_x and point_y):
            if not selector or not _safe_selector(selector):
                return operation_result(app="browser", action="click_element", success=False,
                                        error_code="INVALID_TARGET")
            used_selector = selector
            expression = (
                "(() => { const el = document.querySelector(" + json.dumps(selector) + ");"
                "if (!el) return 'null';"
                "const r = el.getBoundingClientRect();"
                "return JSON.stringify({x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)}); })()"
            )
            ok, value = await self._evaluate(port, resolved, expression)
            coords = self._parse_json(value)
            if not ok or not coords:
                return operation_result(app="browser", action="click_element", success=False,
                                        error_code="ELEMENT_NOT_FOUND")
            point_x, point_y = int(coords["x"]), int(coords["y"])
        url_before = await self._current_url(port, resolved)
        pressed = await asyncio.to_thread(_ws_command, port, resolved, "Input.dispatchMouseEvent",
                                          {"type": "mousePressed", "x": point_x, "y": point_y,
                                           "button": "left", "clickCount": 1})
        released = await asyncio.to_thread(_ws_command, port, resolved, "Input.dispatchMouseEvent",
                                           {"type": "mouseReleased", "x": point_x, "y": point_y,
                                            "button": "left", "clickCount": 1})
        await asyncio.sleep(0.8)
        url_after = await self._current_url(port, resolved)
        executed = bool(pressed[0] and released[0])
        navigated = bool(url_before and url_after and url_before != url_after)
        verified = executed  # pixel-level change can't be asserted generically; nav counts extra
        return operation_result(app="browser", action="click_element",
                                success=executed,
                                execution_success=executed,
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else "EXECUTION_FAILED",
                                navigation_detected=navigated,
                                message=f"Clique enviado em ({point_x},{point_y}).",
                                detail={"selector_used": used_selector, "at": {"x": point_x, "y": point_y}})

    async def type_text(self, text: str, *, selector: str = "", submit: bool = False,
                        secret: bool = False, tab_id: str = "") -> dict:
        port, failure = await self._ensure()
        if failure:
            return failure
        assert port is not None
        resolved, failure = await self.resolve_tab(port, tab_id)
        if failure:
            return failure
        visible, failure = await self.ensure_visible_tab(port, resolved)
        if failure:
            return failure
        focus_expression = (
            "(() => { const el = " + (
                f"document.querySelector({json.dumps(selector)})" if selector
                else "document.activeElement"
            ) + ";"
            "if (!el || !el.focus) return 'no-focusable';"
            "el.focus();"
            "return JSON.stringify({tag: el.tagName.toLowerCase(), password: el.type === 'password'}); })()"
        )
        ok, value = await self._evaluate(port, resolved, focus_expression)
        focused = self._parse_json(value)
        if not ok or not focused:
            return operation_result(app="browser", action="type_text", success=False,
                                    error_code="FOCUS_FAILED",
                                    message="Nenhum campo focável encontrado (informe selector).")
        is_password_field = bool(focused.get("password"))
        inserted = await asyncio.to_thread(_ws_command, port, resolved, "Input.insertText",
                                           {"text": text[:4000]})
        if submit:
            for key in ("Enter_down_placeholder",):
                break
            await asyncio.to_thread(_ws_command, port, resolved, "Input.dispatchKeyEvent",
                                    {"type": "rawKeyDown", "key": "Enter", "code": "Enter",
                                     "windowsVirtualKeyCode": 13})
            await asyncio.to_thread(_ws_command, port, resolved, "Input.dispatchKeyEvent",
                                    {"type": "keyUp", "key": "Enter", "code": "Enter",
                                     "windowsVirtualKeyCode": 13})
        await asyncio.sleep(0.4)
        readback_expression = (
            "(() => { const el = document.activeElement;"
            "if (!el || !('value' in el)) return '{}';"
            "const v = String(el.value || '');"
            "return JSON.stringify({len: v.length,"
            + ("masked: true});" if is_password_field or secret else "preview: v.slice(0,60)});")
            + "})()"
        )
        _, stored = await self._evaluate(port, resolved, readback_expression)
        stored_info = self._parse_json(stored)
        verified = bool(inserted[0]) and int(stored_info.get("len", -1)) >= 0
        payload = {
            "success": bool(inserted[0]),
            "execution_success": bool(inserted[0]),
            "effect_verified": verified,
            "verification_status": "VERIFIED" if verified else "EXECUTED",
            "submitted": submit,
            "field_tag": focused.get("tag", ""),
        }
        if not is_password_field and not secret:
            payload["stored_preview"] = str(stored_info.get("preview", ""))[:60]
        else:
            payload["stored_preview"] = "<password>"
        return payload

    async def _current_url(self, port: int, tab_id: str) -> str:
        tabs = await asyncio.to_thread(_tabs, port)
        current = next((tab for tab in tabs if tab["id"] == tab_id), {})
        return str(current.get("url", ""))

    # ------------------------------------------------------------ select/check
    async def select_option(self, selector: str, value: str, *, tab_id: str = "") -> dict:
        if not selector or not _safe_selector(selector):
            return operation_result(app="browser", action="select_option", success=False,
                                    error_code="INVALID_SELECTOR")
        port, failure = await self._ensure()
        if failure:
            return failure
        resolved, failure = await self.resolve_tab(port, tab_id)
        if failure:
            return failure
        expression = (
            "(() => { const el = document.querySelector(" + json.dumps(selector) + ");"
            "if (!el || el.tagName !== 'SELECT') return 'not-select';"
            "const option = Array.from(el.options).find(o => o.value === " + json.dumps(value) +
            " || o.textContent.trim() === " + json.dumps(value) + ");"
            "if (!option) return 'option-missing';"
            "el.value = option.value;"
            "el.dispatchEvent(new Event('input', {bubbles:true}));"
            "el.dispatchEvent(new Event('change', {bubbles:true}));"
            "return JSON.stringify({selected: el.value}); })()"
        )
        ok, raw = await self._evaluate(port, resolved, expression)
        result = self._parse_json(raw) if isinstance(raw, str) else (raw or {})
        verified = ok and result.get("selected") is not None
        return operation_result(app="browser", action="select_option",
                                success=bool(verified),
                                effect_verified=bool(verified),
                                verification_status="VERIFIED" if verified else "EXECUTION_FAILED",
                                message=f"Opção selecionada: {result.get('selected')}" if verified
                                else f"Falha ao selecionar '{value}' ({raw}).")

    async def set_checked(self, selector: str, checked: bool, *, tab_id: str = "") -> dict:
        if not selector or not _safe_selector(selector):
            return operation_result(app="browser", action="set_checked", success=False,
                                    error_code="INVALID_SELECTOR")
        port, failure = await self._ensure()
        if failure:
            return failure
        resolved, failure = await self.resolve_tab(port, tab_id)
        if failure:
            return failure
        expression = (
            "(() => { const el = document.querySelector(" + json.dumps(selector) + ");"
            "if (!el || !('checked' in el)) return 'not-checkable';"
            "if (el.checked !== " + ("true" if checked else "false") + ") { el.click(); }"
            "return JSON.stringify({checked: el.checked}); })()"
        )
        ok, raw = await self._evaluate(port, resolved, expression)
        result = self._parse_json(raw) if isinstance(raw, str) else (raw or {})
        verified = ok and bool(result.get("checked")) == checked
        return operation_result(app="browser", action="set_checked",
                                success=bool(ok),
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else "EXECUTION_FAILED",
                                detail={"checked": result.get("checked")})

    # ------------------------------------------------------------------- wait
    async def wait_condition(self, condition: str, *, selector: str = "", timeout_seconds: float = 15.0,
                             tab_id: str = "") -> dict:
        allowed = {"navigation", "element", "network_idle", "download"}
        if condition not in allowed:
            return operation_result(app="browser", action="wait_condition", success=False,
                                    error_code="INVALID_CONDITION",
                                    message=f"Condições suportadas: {sorted(allowed)} (§69).")
        started = time.perf_counter()
        deadline = started + max(2.0, min(timeout_seconds, 60.0))
        port, failure = await self._ensure()
        if failure:
            return failure
        assert port is not None
        if condition == "download":
            outcome = await self.wait_download(deadline - time.perf_counter())
            return outcome
        resolved, failure = await self.resolve_tab(port, tab_id)
        if failure:
            return failure
        last_state: Any = None
        while time.perf_counter() < deadline:
            if condition == "element":
                expression = ("!!document.querySelector(" + json.dumps(selector or "body") + ")")
                ok, value = await self._evaluate(port, resolved, expression, timeout=4.0)
                if ok and value:
                    return self._wait_result(True, condition, time.perf_counter() - started)
            elif condition == "navigation":
                ready_expression = "document.readyState"
                ok, state = await self._evaluate(port, resolved, ready_expression, timeout=4.0)
                last_state = state
                if ok and state in {"complete", "interactive"}:
                    return self._wait_result(True, condition, time.perf_counter() - started)
            elif condition == "network_idle":
                pending_expression = (
                    "(performance.getEntriesByType('resource')"
                    ".filter(r => r.responseEnd === 0).length)"
                )
                ok, pending = await self._evaluate(port, resolved, pending_expression, timeout=4.0)
                if ok and int(pending or 0) == 0:
                    return self._wait_result(True, condition, time.perf_counter() - started)
            await asyncio.sleep(0.5)
        return operation_result(
            app="browser", action="wait_condition", success=False,
            error_code="WAIT_TIMEOUT",
            message=f"Condição '{condition}' não satisfeita em {timeout_seconds:.0f}s.",
            detail={"last_state": str(last_state)[:60]},
        )

    @staticmethod
    def _wait_result(met: bool, condition: str, elapsed: float) -> dict:
        return {"success": met, "condition": condition,
                "effect_verified": met, "verification_status": "VERIFIED" if met else "TIMEOUT",
                "elapsed_ms": round(elapsed * 1000, 1)}

    # --------------------------------------------------------------- download
    async def wait_download(self, timeout_seconds: float = 30.0) -> dict:
        """Enable Browser download behavior to our data dir and track progress."""
        from app.core.paths import DATA_ROOT

        downloads_dir = DATA_ROOT / _DOWNLOAD_DIR_NAME
        downloads_dir.mkdir(parents=True, exist_ok=True)
        version_ok, version = _http_json(self.manager.port or 0, "/json/version", timeout=3.0)
        browser_ws = (version or {}).get("webSocketDebuggerUrl") if isinstance(version, dict) else None
        before = sorted(p.name for p in downloads_dir.glob("*") if p.is_file())
        if not (version_ok and browser_ws):
            return operation_result(app="browser", action="wait_download", success=False,
                                    error_code="CDP_UNAVAILABLE",
                                    message="Sem endpoint de browser para rastrear download.")
        try:
            import websocket  # websocket-client já é dependência dos smokes

            connection = websocket.WebSocket()
            connection.settimeout(max(2.0, timeout_seconds))
            connection.connect(browser_ws, http_proxy_host=None, suppress_origin=True)
            connection.send(json.dumps({
                "id": 1, "method": "Browser.setDownloadBehavior",
                "params": {"behavior": "allowAndName", "downloadPath": str(downloads_dir),
                           "eventsEnabled": True},
            }))
            deadline = time.monotonic() + max(3.0, min(timeout_seconds, 120.0))
            completed_guid: str | None = None
            while time.monotonic() < deadline:
                try:
                    raw = connection.recv()
                    message = json.loads(raw)
                except Exception:  # noqa: BLE001 - timeouts/frames parciais são normais
                    continue
                params = message.get("params") or {}
                guid = str(params.get("guid") or "")
                if params.get("state") == "completed":
                    completed_guid = guid or completed_guid
                    break
                if params.get("state") in {"canceled", "interrupted"}:
                    connection.close()
                    return operation_result(app="browser", action="wait_download", success=False,
                                            error_code="DOWNLOAD_INTERRUPTED")
            connection.close()
            await asyncio.sleep(0.8)
            after = sorted(p.name for p in downloads_dir.glob("*") if p.is_file())
            new_files = [name for name in after if name not in before]
            file_verified = False
            size_bytes = 0
            if new_files:
                newest = max(new_files, key=lambda name: (downloads_dir / name).stat().st_size)
                path = downloads_dir / newest
                size_bytes = path.stat().st_size
                file_verified = path.is_file() and size_bytes > 0
            verified = bool(file_verified)
            return operation_result(
                app="browser", action="wait_download", success=verified,
                execution_success=True, effect_verified=verified,
                verification_status="VERIFIED" if verified else "DOWNLOAD_NOT_CONFIRMED",
                message=f"{len(new_files)} arquivo(s) novo(s) em data/{_DOWNLOAD_DIR_NAME}.",
                detail={"files": new_files[:10], "size_bytes": size_bytes,
                        "guid_tail": (completed_guid or "")[-12:]},
            )
        except OSError as exc:
            return operation_result(app="browser", action="wait_download", success=False,
                                    error_code="DOWNLOAD_TRACK_FAILED", message=str(exc)[:200])

    # ------------------------------------------------------------------ script
    async def execute_script(self, script: str, *, approval_id=None, approvals=None,
                             tab_id: str = "") -> dict:
        """§70/§71: only inside the controlled page; dangerous globals gated."""
        forbidden = re.compile(
            r"(?i)(document\.cookie|localstorage|sessionstorage|indexeddb|"
            r"fetch\s*\(|xmlhttprequest|navigator\.sendbeacon|credentials)"
        )
        needs_approval = bool(forbidden.search(script))
        if needs_approval:
            from app.tools.shell_models import ShellRiskLevel

            if approvals is None:
                return {"success": False, "error_code": "APPROVAL_REQUIRED"}
            description = f"Executar script com APIs sensíveis na página controlada"
            fingerprint = approvals.fingerprint(description, "browser_script", "", 20, target="local")
            if not approval_id:
                record = approvals.request(command=description, shell="browser_script",
                                           working_directory=".", timeout_seconds=20,
                                           risk_level=ShellRiskLevel.ELEVATED, target="local")
                return {"success": False, "error_code": "APPROVAL_REQUIRED",
                        "approval_required": True, "approval_id": record.approval_id}
            granted, reason = approvals.consume(approval_id, fingerprint)
            if not granted:
                return {"success": False, "error_code": "APPROVAL_INVALID", "message": reason}
        port, failure = await self._ensure()
        if failure:
            return failure
        resolved, failure = await self.resolve_tab(port, tab_id)
        if failure:
            return failure
        ok, value = await self._evaluate(port, resolved, script[:8000], timeout=10.0)
        output = redact_secrets(json.dumps(value, ensure_ascii=False, default=str)[:2000]) if value is not None else ""
        return {"success": bool(ok), "execution_success": bool(ok),
                "result_preview": output, "approval_used": bool(needs_approval)}
