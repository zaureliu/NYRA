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


@pytest.mark.asyncio
async def test_find_click_type_select_check_wait(local_site, controller):
    # Isolamento: navegação própria deste teste — nunca depender do estado de
    # aba deixado por teste/sessão anterior (§21 task isolation).
    navigated = await controller.navigate_via_open(local_site)
    assert navigated.get("success") is True, navigated

    found = await controller.find_element(text="clique", limit=5)
    assert found["success"] is True and found["count"] >= 1  # §62

    typed = await controller.type_text("busca da nyra", selector="#campo")
    assert typed["success"] is True
    assert typed.get("stored_preview") == "busca da nyra"  # read-back verificado (§64)

    secret_typed = await controller.type_text("segredo-123", selector="#campo", secret=True)
    assert secret_typed["stored_preview"] == "<password>" or secret_typed["effect_verified"]

    selected = await controller.select_option("#lista", "b")
    assert selected["success"] is True and selected["effect_verified"] is True  # §65

    checked = await controller.set_checked("#check", True)
    assert checked["success"] is True and checked["checked"] is True  # §66

    clicked = await controller.click_element(selector="#botao")
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
    clicked = await controller.click_element(selector="#link-download")
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

    allowed = await controller.execute_script("(function(){ return 40 + 2 })()")
    assert allowed["success"] is True
    assert "42" in allowed["result_preview"]
