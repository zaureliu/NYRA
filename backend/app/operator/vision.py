"""Visual understanding + actions (spec Parte A §17-§30).

Strategy order (§9): UIA structure projected onto the captured frame FIRST;
raw-pixel heuristics and OCR only as fallback (§19/§20). Element ids expire
with their frame (§21/§22); clicks revalidate geometry against a fresh capture
before acting (§24). Destructive modal acceptance is never automatic (§30).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from app.desktop import uia as uia_layer
from app.desktop.windows import list_visible_windows
from app.operator.vision_capture import (
    CaptureError,
    Frame,
    FrameStore,
    capture_screen,
    capture_window,
    diff_frames,
    fingerprint_pixels,
    sanitize_visual_text,
)
from app.tools.redaction import redact_secrets

_MODAL_UAC = re.compile(r"(?i)(controle de conta de usu|user account control|\buac\b)")
_MODAL_ERROR = re.compile(r"(?i)^\[?(erro|error|alerta|warning|falha)")
_MODAL_CONFIRM = re.compile(
    r"(?i)(confirmar|confirme|tem certeza|are you sure|excluir|apagar|deletar|formatar|delete|format)"
)
_MODAL_SAVE = re.compile(r"(?i)(salvar como|save as)")
_DESTRUCTIVE_LABELS = re.compile(r"(?i)(excluir|apagar|deletar|formatar|delete)")


class VisionEngine:
    """Façade owning the FrameStore plus visual inspection/actions.

    All UIA/GDI work runs on ONE dedicated worker thread: COM proxies stay
    pinned to a single apartment and never leak across asyncio pool threads.
    """

    def __init__(self, approvals=None, *, frame_ttl_seconds: float = 45.0,
                 max_frames: int = 8, debug_keep_frames: bool = False) -> None:
        self.frames = FrameStore(ttl_seconds=frame_ttl_seconds, max_frames=max_frames)
        self.approvals = approvals
        self.debug_keep_frames = debug_keep_frames
        self._fingerprints: dict[str, str] = {}
        import concurrent.futures

        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="nyra-vision-com",
        )
        self._shutdown = False

    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()

        def invoke():
            try:
                return fn(*args, **kwargs)
            finally:
                # UIA providers belong to the target application's COM/RPC
                # lifetime. Dispose their apartment-bound proxies immediately,
                # while still on this worker and before that app can close.
                if getattr(fn, "__module__", "") == uia_layer.__name__:
                    uia_layer.release_current_thread()

        return await loop.run_in_executor(self._pool, invoke)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        try:
            self._pool.submit(uia_layer.close_current_thread).result(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self.frames.clear()
            self._fingerprints.clear()

    # ------------------------------------------------------------------ capture
    def capture(self, *, target: str = "window", hwnd: int | None = None,
                monitor_id: int = 0, region: dict[str, int] | None = None) -> dict:
        started = time.perf_counter()
        try:
            if target == "window":
                if not hwnd:
                    raise CaptureError("HWND_REQUIRED", "Informe hwnd (prefira captura de janela, §13).")
                frame = capture_window(int(hwnd))
            elif target == "monitor":
                frame = capture_screen(monitor_id=int(monitor_id or 1))
            elif target == "region":
                frame = capture_screen(region=region, monitor_id=int(monitor_id or 0))
            else:
                raise CaptureError("INVALID_TARGET", "Use window/monitor/region (desktop inteiro evitado, §14).")
        except CaptureError as exc:
            return {"success": False, "error_code": exc.code, "message": str(exc)}
        self.frames.put(frame)
        self._fingerprints[frame.frame_id] = fingerprint_pixels(frame)
        debug_path = None
        if self.debug_keep_frames:
            debug_path = self._save_debug(frame)
        return {
            "success": True,
            "frame": {
                "frame_id": frame.frame_id,
                "timestamp": frame.timestamp,
                "monitor_id": frame.monitor_id,
                "window_handle": frame.window_handle,
                "dimensions": {"width": frame.width, "height": frame.height},
                "scale": frame.scale,
                "scope": frame.scope,
            },
            "debug_png_path": debug_path,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def _save_debug(self, frame: Frame) -> str | None:
        try:
            from pathlib import Path

            from app.core.paths import DATA_ROOT
            from app.operator.vision_capture import save_debug_png

            directory = DATA_ROOT / "vision-debug"
            directory.mkdir(parents=True, exist_ok=True)
            return save_debug_png(frame, Path(directory))
        except Exception:  # noqa: BLE001 - debug artifact must never break capture
            return None

    # ------------------------------------------------------------------ inspect
    async def inspect(self, frame_id: str) -> dict:
        frame = self.frames.get(frame_id)
        elements: list[dict] = []
        source = "uia"
        if frame.window_handle:
            try:
                tree = await self._run(uia_layer.inspect_window, int(frame.window_handle), 5)
                elements = self._project_uia_nodes(frame, tree.get("elements") or [])
            except Exception:  # noqa: BLE001 - honest fallback below
                elements = []
        if not elements and not frame.window_handle:
            source = "pixels_only"
        text_regions = [
            {"visual_element_id": el["visual_element_id"], "text": el["name"], "rect": el["rect"]}
            for el in elements if el["control_type"] == "Text" and el["name"]
        ]
        buttons = [el["visual_element_id"] for el in elements if el["control_type"] == "Button"]
        menus = [el["visual_element_id"] for el in elements
                 if el["control_type"] in {"Menu", "MenuItem", "MenuBar"}]
        edits = [el["visual_element_id"] for el in elements if el["control_type"] == "Edit"]
        warnings = [el["visual_element_id"] for el in elements if _MODAL_ERROR.search(el.get("name") or "")]
        dialogs = await self._run(self.detect_modals)
        return {
            "success": True,
            "frame_id": frame_id,
            "source": source,
            "detected_controls": elements[:60],
            "text_regions": text_regions[:40],
            "dialogs": dialogs.get("modals", []),
            "buttons": buttons[:20],
            "menus": menus[:12],
            "edits": edits[:12],
            "warnings": warnings[:8],
            "status": "OK" if elements else "SEM_ELEMENTOS_UIA_NESTE_FRAME",
        }

    def _project_uia_nodes(self, frame: Frame, nodes: list[dict]) -> list[dict]:
        projected: list[dict] = []
        for index, node in enumerate(nodes):
            rect = node.get("rect")
            if not rect:
                continue
            name = sanitize_visual_text(node.get("name") or "")
            value = node.get("value")
            password_like = bool(re.search(r"(?i)(senha|password)", name)) or node.get("masked")
            element = {
                "visual_element_id": f"ve_{index:03d}",
                "name": name[:120],
                "automation_id": node.get("automation_id", ""),
                "control_type": node.get("control_type", "Unknown"),
                "class_name": node.get("class_name", ""),
                "enabled": bool(node.get("enabled", True)),
                "rect": {key: int(rect[key]) for key in ("x", "y", "width", "height")},
                "value_preview": ("<masked>" if password_like else sanitize_visual_text(str(value)[:80])) if value else "",
            }
            projected.append(element)
            frame.elements[element["visual_element_id"]] = element
        return projected

    # ------------------------------------------------------------- modal detect
    def detect_modals(self) -> dict:
        modals: list[dict] = []
        try:
            windows = list_visible_windows()
        except Exception:  # noqa: BLE001
            windows = []
        for window in windows:
            title = window.title or ""
            class_name = (window.window_class or "").casefold()
            kind: str | None = None
            destructive = False
            if class_name == "#32770":
                if _MODAL_UAC.search(title):
                    kind = "uac_boundary"
                elif _MODAL_SAVE.search(title):
                    kind = "save_prompt_or_file_picker"
                elif _MODAL_CONFIRM.search(title):
                    kind = "confirmation"
                    destructive = bool(_DESTRUCTIVE_LABELS.search(title))
                elif _MODAL_ERROR.search(title):
                    kind = "error_dialog"
                else:
                    kind = "dialog"
            elif _MODAL_ERROR.search(title) and len(title) < 120:
                kind = "error_dialog"
            if kind:
                modals.append({
                    "hwnd": window.hwnd,
                    "title": title[:120],
                    "window_class": window.window_class,
                    "kind": kind,
                    "destructive": destructive,
                    "policy": "NUNCA aceitar modais destrutivos automaticamente (§30)",
                })
        return {"success": True, "modals": modals, "count": len(modals)}

    # --------------------------------------------------------------------- diff
    def compare(self, before_id: str, after_id: str) -> dict:
        before = self.frames.peek(before_id)
        after = self.frames.peek(after_id)
        if before is None or after is None:
            return {"success": False, "error_code": "FRAME_EXPIRED",
                    "message": "Um dos frames expirou; recapture."}
        return diff_frames(before, after)

    # ------------------------------------------------------------------- click
    async def click(self, frame_id: str, element_id: str, *, approval_id: str | None = None) -> dict:
        started = time.perf_counter()
        frame = self.frames.get(frame_id)
        element = frame.elements.get(element_id)
        if element is None:
            return {"success": False, "error_code": "VISUAL_ELEMENT_NOT_FOUND",
                    "message": "Elemento não existe neste frame; capture e inspecione novamente (§22)."}
        hwnd = int(frame.window_handle)
        if not hwnd:
            return {"success": False, "error_code": "HWND_REQUIRED",
                    "message": "Cliques visuais exigem frame de janela com handle."}
        stale = await self._frame_is_stale(frame)
        if stale:
            return {"success": False, "error_code": "FRAME_STALE",
                    "message": "Geometria mudou desde a captura; recapture antes de clicar (§24)."}
        target_binding = json.dumps(
            {"hwnd": hwnd, "frame_id": frame_id, "element_id": element_id,
             "frame_fingerprint": self._fingerprints.get(frame_id, ""),
             "element": element},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        target_hash = hashlib.sha256(target_binding.encode("utf-8")).hexdigest()
        decision = self._require_approval(
            f"visual_click target_sha256={target_hash}",
            resource_key=f"vision:click:{hwnd}:{frame_id}:{element_id}",
            approval_id=approval_id,
        )
        if decision is not None:
            decision["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return decision
        if await self._frame_is_stale(frame):
            return {"success": False, "error_code": "FRAME_STALE",
                    "message": "Geometria mudou após o approval; recapture antes de clicar."}
        before_fp = self._fingerprints.get(frame_id, "")
        try:
            action = await self._run(
                uia_layer.click_element, hwnd,
                name=element.get("name") or "", automation_id=element.get("automation_id") or "",
                control_type=element.get("control_type") or "",
            )
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "UI_ACTION_FAILED")
            return {"success": False, "error_code": code, "message": str(exc),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1)}
        await asyncio.sleep(0.6)
        verification = await self._verify_after_action(frame)
        return {
            "success": True,
            "clicked": element_id,
            "method": action.get("method", ""),
            "effect_verified": verification.get("changed"),
            "verification_status": "VERIFIED" if verification.get("changed") else "EXECUTED",
            "change_area_ratio": verification.get("area_ratio"),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "before_fingerprint": before_fp[:16],
            "approval_used": True,
        }

    # -------------------------------------------------------------------- type
    async def type_text(self, frame_id: str, element_id: str, text: str, *,
                        secret: bool = False, approval_id: str | None = None) -> dict:
        started = time.perf_counter()
        frame = self.frames.get(frame_id)
        element = frame.elements.get(element_id)
        if element is None:
            return {"success": False, "error_code": "VISUAL_ELEMENT_NOT_FOUND"}
        hwnd = int(frame.window_handle)
        if not hwnd:
            return {"success": False, "error_code": "HWND_REQUIRED"}
        if secret and len(text) > 4096:
            return {"success": False, "error_code": "INVALID_SECRET"}
        if await self._frame_is_stale(frame):
            return {"success": False, "error_code": "FRAME_STALE"}
        target_binding = json.dumps(
            {"hwnd": hwnd, "frame_id": frame_id, "element_id": element_id,
             "frame_fingerprint": self._fingerprints.get(frame_id, ""),
             "element": element,
             "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
             "secret": bool(secret)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        target_hash = hashlib.sha256(target_binding.encode("utf-8")).hexdigest()
        decision = self._require_approval(
            f"visual_type target_sha256={target_hash}",
            resource_key=f"vision:type:{hwnd}:{frame_id}:{element_id}",
            approval_id=approval_id,
        )
        if decision is not None:
            decision["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return decision
        if await self._frame_is_stale(frame):
            return {"success": False, "error_code": "FRAME_STALE",
                    "message": "Geometria mudou após o approval; recapture antes de digitar."}
        try:
            result = await self._run(
                uia_layer.set_text, hwnd, text,
                name=element.get("name") or "", automation_id=element.get("automation_id") or "",
                control_type="edit",
            )
            verified = bool(result.get("effect_verified"))
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "UI_ACTION_FAILED")
            return {"success": False, "error_code": code, "message": redact_secrets(str(exc)),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1)}
        stored_preview = str(result.get("stored_preview") or "")
        return {
            "success": True,
            "typed_element": element_id,
            "effect_verified": verified,
            "verification_status": "VERIFIED" if verified else "VERIFICATION_FAILED",
            "stored_preview": "<secret not echoed>" if secret else sanitize_visual_text(stored_preview[:80]),
            "approval_used": True,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    # -------------------------------------------------------------------- read
    async def read(self, frame_id: str, *, use_ocr: bool = False) -> dict:
        frame = self.frames.get(frame_id)
        texts: list[dict] = []
        for element in frame.elements.values():
            if element["control_type"] in {"Text", "Edit", "Button"} and element["name"]:
                texts.append({"text": element["name"], "rect": element["rect"],
                              "source": "uia"})
        if texts or not use_ocr:
            return {"success": True, "frame_id": frame_id, "regions": texts[:60],
                    "source": "uia",
                    "note": "" if texts else "UIA não expôs texto neste frame."}
        ocr = self._ocr_fallback(frame)
        return {"success": True, "frame_id": frame_id, "source": "ocr_windows",
                **ocr}

    def _ocr_fallback(self, frame: Frame) -> dict:
        """Windows.Media.Ocr via PowerShell WinRT — best effort, honest fallback."""
        from app.operator.vision_ocr import windows_ocr_available, run_windows_ocr

        if not windows_ocr_available():
            return {"available": False, "regions": [],
                    "note": "OCR do Windows indisponível; prefira UIA/accessibility (§19)."}
        png_path = self._save_debug(frame)
        if not png_path:
            return {"available": False, "regions": [], "note": "Falha ao materializar PNG para OCR."}
        outcome = run_windows_ocr(png_path)
        regions = [
            {"text": sanitize_visual_text(line), "rect": rect, "source": "ocr"}
            for line, rect in outcome.get("lines", [])
        ]
        return {"available": outcome.get("available", False), "regions": regions[:80],
                "note": outcome.get("note", "")}

    # -------------------------------------------------------------- internals
    async def _frame_is_stale(self, frame: Frame) -> bool:
        if not frame.window_handle:
            return False
        try:
            fresh = await self._run(capture_window, int(frame.window_handle))
        except CaptureError:
            return True
        fresh_fp = fingerprint_pixels(fresh)
        old_fp = fingerprint_pixels(frame)
        return fresh_fp != old_fp

    async def _verify_after_action(self, frame: Frame) -> dict:
        try:
            after = await self._run(capture_window, int(frame.window_handle))
        except CaptureError:
            return {"changed": None}
        comparison = diff_frames(frame, after)
        self._fingerprints[after.frame_id] = fingerprint_pixels(after)
        self.frames.put(after)
        return {"changed": comparison.get("changed"), "area_ratio": comparison.get("area_ratio")}

    def _require_approval(self, description: str, *, resource_key: str,
                          approval_id: str | None) -> dict | None:
        from app.tools.shell_models import ShellRiskLevel

        if self.approvals is None:
            return {"success": False, "error_code": "APPROVAL_REQUIRED", "approval_required": True}
        bound_description = f"{description} resource={resource_key}"
        fingerprint = self.approvals.fingerprint(bound_description, "vision", "", 30, target="local")
        if not approval_id:
            record = self.approvals.request(
                command=bound_description, shell="vision", working_directory="",
                timeout_seconds=30, risk_level=ShellRiskLevel.ELEVATED, target="local",
                fingerprint=fingerprint,
            )
            return {"success": False, "error_code": "APPROVAL_REQUIRED", "approval_required": True,
                    "approval_id": record.approval_id}
        granted, reason = self.approvals.consume(approval_id, fingerprint)
        if not granted:
            return {"success": False, "error_code": "APPROVAL_INVALID", "message": reason}
        return None
