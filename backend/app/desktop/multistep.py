"""Multi-step determinístico (nyra-full §26/§38): abrir → digitar → salvar.

Executor SEM LLM para o padrão canônico do Bloco de Notas. Cada passo é
PLAN→ACT→VERIFY; falha real interrompe com relatório honesto por passo.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("nyra.desktop.multistep")


async def notepad_write_and_save(controller, text: str, filename: str,
                                 save_dir: Path | None = None,
                                 *, close_after: bool = False) -> dict[str, Any]:
    """Executa abrir→digitar→salvar→verificar e, quando pedido, fechar→verificar."""
    started = time.perf_counter()
    steps: list[dict[str, Any]] = []

    def step(name: str, ok: bool, detail: Any = None) -> dict[str, Any]:
        entry = {"step": name, "ok": bool(ok)}
        if detail is not None:
            entry["detail"] = detail
        steps.append(entry)
        return entry

    from app.core.paths import DATA_ROOT

    directory = save_dir or (Path.home() / "Desktop")
    if not directory.is_dir():
        directory = DATA_ROOT
    target_file = directory / filename

    # 1) launch + verify
    launch = await controller.launch_dynamic("bloco de notas", origin="multistep")
    pid = None
    windows = launch.get("windows") or []
    if isinstance(launch.get("detail"), dict):
        pid = launch["detail"].get("pid") or (launch["detail"].get("windows") or [{}])[0].get("pid")
    if not windows:
        windows = launch.get("windows") or []
    hwnd = windows[0].get("hwnd") if windows else None
    step("launch_notepad", bool(launch.get("success")) and bool(launch.get("effect_verified")),
         {"pid": pid, "hwnd": hwnd})
    if not (launch.get("success") and launch.get("effect_verified")):
        return {
            "success": False, "action": "notepad_write_and_save",
            "message": f"Falha ao abrir o Bloco de Notas: {launch.get('message', '')}",
            "steps": steps, "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    # 2) focus
    from app.desktop import window_manager as wm

    focused = await asyncio.to_thread(wm.focus_window, hwnd)
    step("focus", focused)

    # 3) digitar com verificação de leitura (UIA ValuePattern)
    typed = await controller.ui_set_text(text, hwnd=hwnd, control_type="Edit")
    step("type_text", bool(typed.get("success")) and typed.get("effect_verified") is not False,
         {"effect_verified": typed.get("effect_verified"), "message": typed.get("message", "")})
    if not typed.get("success"):
        return {
            "success": False, "action": "notepad_write_and_save",
            "message": f"Não consegui inserir o texto no Bloco de Notas: {typed.get('message', '')}",
            "steps": steps, "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    # 4) Ctrl+S → diálogo Salvar como
    await controller.ui_send_keys("{ctrl+s}", hwnd=hwnd)
    dialog_hwnd = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.4)
        dialog_hwnd = _find_save_dialog(pid)
        if dialog_hwnd:
            break
    step("save_dialog", dialog_hwnd is not None, {"hwnd": dialog_hwnd})

    saved_via_dialog = False
    if dialog_hwnd:
        full_path = str(target_file)
        # O Edit do ComboBox do Save As recebe o foco por padrão; SendInput
        # é mais confiável que ValuePattern nesse controle legado.
        typed_path = await controller.ui_send_keys(f"{full_path}{{enter}}", hwnd=dialog_hwnd)
        saved_via_dialog = bool(typed_path.get("success"))
        step("type_path_and_confirm", saved_via_dialog,
             {"message": typed_path.get("message", "")})
        await asyncio.sleep(1.2)

    # 5) verificação final do arquivo em disco
    file_ok = False
    content_ok = False
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if target_file.is_file():
            file_ok = True
            try:
                content_ok = text[:200] in target_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                content_ok = False
            break
        await asyncio.sleep(0.5)
    step("verify_file", file_ok and content_ok,
         {"path": str(target_file), "content_match": content_ok})

    success = bool(file_ok and content_ok)
    close_verified: bool | None = None
    if success and close_after:
        closed = await controller.close(hwnd=hwnd)
        close_verified = closed.get("effect_verified")
        step("close_notepad", close_verified is True,
             {"hwnd": hwnd, "verification_status": closed.get("verification_status")})
        success = success and close_verified is True

    if success:
        message = (
            f"Tarefa concluída: Bloco de Notas aberto, texto inserido e arquivo salvo "
            f"em {target_file} com conteúdo verificado"
            + ("; janela fechada com verificação." if close_after else ".")
        )
    else:
        failed_steps = [s["step"] for s in steps if not s["ok"]]
        message = (
            "Tarefa não concluída por completo; passos sem confirmação: "
            + ", ".join(failed_steps)
            + ". Nenhum sucesso foi assumido."
        )
    logger.info("multistep_notepad result=%s steps=%s", success, [s["step"] for s in steps])
    return {
        "success": success, "action": "notepad_write_and_save",
        "effect_verified": success,
        "message": message, "steps": steps,
        "file": str(target_file), "content_match": content_ok,
        "close_requested": close_after, "close_verified": close_verified,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _find_save_dialog(owner_pid: int | None) -> int | None:
    """Procura o diálogo modal '#32770' do Save As pertencente ao notepad."""
    from app.desktop.windows import annotate_process_names, list_visible_windows

    for window in annotate_process_names(list_visible_windows()):
        title = (window.title or "").casefold()
        if window.window_class == "#32770" and (
            "salvar como" in title or "save as" in title
        ):
            if owner_pid is None or window.pid == owner_pid:
                return window.hwnd
    return None
