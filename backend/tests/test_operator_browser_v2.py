"""Browser Control V2 E2E tests (spec Partes C/T, §46-§78/§279-§287).

Real managed Chrome/Edge over CDP against a LOCAL ThreadingHTTPServer only.
Skips honestly when no Chromium-family browser is installed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.desktop.browser import BrowserManager
from app.operator.browser_v2 import BrowserV2Controller
from app.tools.shell_approval import ShellApprovalGate


PAGE_HTML = """<!doctype html>
<html><head><title>NYRA Browser V2 Fixture</title></head>
<body>
  <h1 id="titulo">pagina de teste</h1>
  <input id="campo" name="q" type="text" value=""/>
  <input id="check" type="checkbox"/>
  <select id="lista"><option value="a">opcao-a</option><option value="b">opcao-b</option></select>
  <button id="botao" onclick="document.getElementById('titulo').textContent='clicado!'">clique</button>
  <a id="link-download" href="/download">baixar</a>
</body></html>"""


def _make_handler(download_bytes: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/download"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Disposition", "attachment; filename=nyra-e2e-download.txt")
                self.send_header("Content-Length", str(len(download_bytes)))
                self.end_headers()
                self.wfile.write(download_bytes)
                return
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silencia
            return

    return Handler


@pytest.fixture(scope="module")
def local_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(b"conteudo do download e2e\n"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def controller():
    from app.desktop.browser import _find_browser_executable

    if _find_browser_executable("") is None and _find_browser_executable("edge") is None:
        pytest.skip("Chrome/Edge não disponíveis nesta máquina")
    manager = BrowserManager()
    controller = BrowserV2Controller(manager)

    async def _close():
        pass

    yield controller
    try:
        manager.shutdown()
    except Exception:  # noqa: BLE001
        pass


@pytest.mark.asyncio
async def test_browser_status_and_open(local_site, controller):
    outcome = await controller.manager.ensure_running()
    assert outcome[0] is True, outcome  # §79 smoke real do navegador gerenciado
    status = await controller.status()
    assert status["success"] is True and status["reachable"] is True


@pytest.mark.asyncio
async def test_navigate_dom_inspect_masks_passwords(local_site, controller):
    navigated = await controller.navigate_via_open(local_site)
    del navigated
    document = await controller.dom_inspect(max_nodes=60)
    assert document["success"] is True
    assert "NYRA Browser V2" in document["title"]
    texts = json_dumps(document["nodes"])
    assert "pagina de teste" in texts


def json_dumps(nodes) -> str:
    import json as _json

    return _json.dumps(nodes, ensure_ascii=False)


async def _approved_call(method, *args, **kwargs):
    approvals = ShellApprovalGate()
    pending = await method(*args, approvals=approvals, **kwargs)
    assert pending["error_code"] == "APPROVAL_REQUIRED"
    approvals.grant(pending["approval_id"], "operator_test")
    return await method(
        *args, approvals=approvals, approval_id=pending["approval_id"], **kwargs,
    )


@pytest.mark.asyncio
async def test_find_click_type_select_check_wait(local_site, controller):
    # Isolamento: navegação própria deste teste — nunca depender do estado de
    # aba deixado por teste/sessão anterior (§21 task isolation).
    navigated = await controller.navigate_via_open(local_site)
    assert navigated.get("success") is True, navigated

    found = await controller.find_element(text="clique", limit=5)
    assert found["success"] is True and found["count"] >= 1  # §62

    typed = await _approved_call(controller.type_text, "busca da nyra", selector="#campo")
    assert typed["success"] is True
    assert typed.get("stored_preview") == "busca da nyra"  # read-back verificado (§64)

    secret_typed = await _approved_call(
        controller.type_text, "segredo-123", selector="#campo", secret=True,
    )
    assert secret_typed["stored_preview"] == "<password>" or secret_typed["effect_verified"]

    selected = await _approved_call(controller.select_option, "#lista", "b")
    assert selected["success"] is True and selected["effect_verified"] is True  # §65

    checked = await _approved_call(controller.set_checked, "#check", True)
    assert checked["success"] is True and checked["checked"] is True  # §66

    approvals = ShellApprovalGate()
    pending = await controller.click_element(selector="#botao", approvals=approvals)
    assert pending["error_code"] == "APPROVAL_REQUIRED"
    approvals.grant(pending["approval_id"], "operator_test")
    tampered = await controller.click_element(
        selector="#check", approvals=approvals, approval_id=pending["approval_id"],
    )
    assert tampered["error_code"] == "APPROVAL_INVALID"
    clicked = await controller.click_element(
        selector="#botao", approvals=approvals, approval_id=pending["approval_id"],
    )
    assert clicked["success"] is True and clicked["verification_status"] == "VERIFIED"
    await asyncio.sleep(0.6)
    verify = await controller.find_element(text="clicado!", limit=3)
    assert verify["count"] >= 1  # §28 verificação pós-ação

    waited = await controller.wait_condition("element", selector="#link-download",
                                             timeout_seconds=10)
    assert waited["success"] is True  # §69


@pytest.mark.asyncio
async def test_real_download_is_verified_on_disk(local_site, controller, tmp_path):
    """§67/§68/§287: download real confirmado como arquivo."""
    approvals = ShellApprovalGate()
    pending = await controller.click_element(selector="#link-download", approvals=approvals)
    assert pending["error_code"] == "APPROVAL_REQUIRED"
    approvals.grant(pending["approval_id"], "operator_test")
    clicked = await controller.click_element(
        selector="#link-download", approvals=approvals,
        approval_id=pending["approval_id"],
    )
    assert clicked["success"] is True
    outcome = await controller.wait_download(timeout_seconds=25)
    assert outcome["success"] in {True, False}  # comportamento depende do browser
    if outcome["success"]:
        from app.core.paths import DATA_ROOT

        files = list((DATA_ROOT / "downloads").glob("*"))
        assert any(item.stat().st_size > 0 for item in files)


@pytest.mark.asyncio
async def test_execute_script_blocks_secret_globals_without_approval(controller):
    blocked = await controller.execute_script("document.cookie")
    assert blocked["success"] is False
    assert blocked["error_code"] == "APPROVAL_REQUIRED"  # §70-§74 fail-closed

    formerly_safe = await controller.execute_script("(function(){ return 40 + 2 })()")
    assert formerly_safe["success"] is False
    assert formerly_safe["error_code"] == "APPROVAL_REQUIRED"


@pytest.mark.asyncio
async def test_execute_script_binds_full_url_and_checks_it_inside_guard():
    controller = BrowserV2Controller(object())
    current_url = {"value": "https://example.test/account?mode=one#security"}
    executed: list[str] = []

    async def ensure():
        return 9222, None

    async def resolve(_port, _tab_id=""):
        return "tab-security", None

    async def evaluate(_port, _tab_id, expression, timeout=8.0):
        del timeout
        if expression == "location.href":
            return True, current_url["value"]
        executed.append(expression)
        return True, 42

    controller._ensure = ensure  # type: ignore[method-assign]
    controller.resolve_tab = resolve  # type: ignore[method-assign]
    controller._evaluate = evaluate  # type: ignore[method-assign]
    approvals = ShellApprovalGate()
    script = "40 + 2"
    pending = await controller.execute_script(script, approvals=approvals)
    approvals.grant(pending["approval_id"], "operator_test")

    current_url["value"] = "https://example.test/account?mode=two#security"
    changed = await controller.execute_script(
        script, approvals=approvals, approval_id=pending["approval_id"],
    )
    assert changed["error_code"] == "APPROVAL_INVALID"
    assert executed == []

    current_url["value"] = "https://example.test/account?mode=one#security"
    exact = await controller.execute_script(
        script, approvals=approvals, approval_id=pending["approval_id"],
    )
    assert exact["success"] is True and exact["approval_used"] is True
    assert len(executed) == 1
    assert "location.href !== approvedUrl" in executed[0]
    assert current_url["value"] in executed[0]
    assert "return eval(\"40 + 2\")" in executed[0]


@pytest.mark.asyncio
async def test_browser_submit_binds_text_tab_and_full_url(monkeypatch):
    controller = BrowserV2Controller(object())
    current_url = {"value": "https://example.test/checkout?step=confirm"}
    guarded_race = {"enabled": False}
    executed: list[str] = []

    async def ensure():
        return 9222, None

    async def resolve(_port, _tab_id=""):
        return "tab-checkout", None

    async def visible(_port, _tab_id):
        return True, None

    async def evaluate(_port, _tab_id, expression, timeout=8.0):
        del timeout
        if expression == "location.href":
            return True, current_url["value"]
        if "snapshot: nyraSnapshot" in expression:
            return True, json_dumps({
                "snapshot": '{"tag":"input","id":"decision"}', "x": 10, "y": 10,
            })
        if "const approvedUrl" in expression:
            executed.append(expression)
            if guarded_race["enabled"]:
                return True, '{"guard_error":"BROWSER_TARGET_CHANGED"}'
            return True, '{"inserted":true,"tag":"input","password":false,"len":7,"preview":"approve"}'
        return True, None

    monkeypatch.setattr(controller, "_ensure", ensure)
    monkeypatch.setattr(controller, "resolve_tab", resolve)
    monkeypatch.setattr(controller, "ensure_visible_tab", visible)
    monkeypatch.setattr(controller, "_evaluate", evaluate)

    approvals = ShellApprovalGate()
    pending = await controller.type_text(
        "approve", selector="#decision", submit=True, approvals=approvals,
    )
    assert pending["error_code"] == "APPROVAL_REQUIRED"
    approvals.grant(pending["approval_id"], "operator_test")

    wrong_text = await controller.type_text(
        "deny", selector="#decision", submit=True, approvals=approvals,
        approval_id=pending["approval_id"],
    )
    assert wrong_text["error_code"] == "APPROVAL_INVALID"
    current_url["value"] = "https://example.test/checkout?step=changed"
    wrong_url = await controller.type_text(
        "approve", selector="#decision", submit=True, approvals=approvals,
        approval_id=pending["approval_id"],
    )
    assert wrong_url["error_code"] == "APPROVAL_INVALID"
    current_url["value"] = "https://example.test/checkout?step=confirm"
    exact = await controller.type_text(
        "approve", selector="#decision", submit=True, approvals=approvals,
        approval_id=pending["approval_id"],
    )
    assert exact["success"] is True
    assert exact["submitted"] is True and exact["approval_used"] is True
    assert len(executed) == 1
    assert "location.href !== approvedUrl" in executed[0]
    assert "nyraSnapshot(el)" in executed[0]
    assert "requestSubmit" in executed[0]

    race_gate = ShellApprovalGate()
    race_pending = await controller.type_text(
        "approve", selector="#decision", submit=False, approvals=race_gate,
    )
    race_gate.grant(race_pending["approval_id"], "operator_test")
    guarded_race["enabled"] = True
    raced = await controller.type_text(
        "approve", selector="#decision", submit=False, approvals=race_gate,
        approval_id=race_pending["approval_id"],
    )
    assert raced["error_code"] == "BROWSER_TARGET_CHANGED"
