"""Clipboard local estruturado para o Universal Operator (kazumi-7c §39/§96).

A percepção lê somente metadados. Esta capability separada permite status,
escrita e limpeza explícitas. Conteúdo nunca é lido nem devolvido ao LLM,
logs ou eventos. Todas as operações são limitadas ao clipboard da sessão
interativa atual e verificadas por Win32.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading
import time
from typing import Protocol


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
MAX_FORMATS = 64


class ClipboardBackend(Protocol):
    def status(self) -> dict: ...
    def write_text(self, text: str) -> dict: ...
    def clear(self) -> dict: ...


class Win32ClipboardBackend:
    """Primitivas Win32; nenhuma delas lê o conteúdo armazenado."""

    def __init__(self) -> None:
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self.kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        self.kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self.kernel32.GlobalLock.restype = wintypes.LPVOID
        self.kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self.kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self.user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self.user32.SetClipboardData.restype = wintypes.HANDLE

    def _open(self, timeout_seconds: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.user32.OpenClipboard(0):
                return True
            time.sleep(0.025)
        return False

    def _sequence(self) -> int:
        return int(self.user32.GetClipboardSequenceNumber() or 0)

    def status(self) -> dict:
        if not self._open():
            return self._failure("CLIPBOARD_BUSY")
        try:
            formats: list[int] = []
            current = 0
            for _ in range(MAX_FORMATS):
                current = int(self.user32.EnumClipboardFormats(current) or 0)
                if not current:
                    break
                formats.append(current)
            return {
                "success": True,
                "available": bool(formats),
                "has_text": bool(self.user32.IsClipboardFormatAvailable(CF_UNICODETEXT)),
                "formats": formats,
                "sequence": self._sequence(),
                "effect_verified": True,
                "content_exposed": False,
            }
        finally:
            self.user32.CloseClipboard()

    def write_text(self, text: str) -> dict:
        if "\x00" in text:
            return self._failure("CLIPBOARD_INVALID_TEXT")
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not handle:
            return self._failure("CLIPBOARD_ALLOCATION_FAILED")
        transferred = False
        try:
            pointer = self.kernel32.GlobalLock(handle)
            if not pointer:
                return self._failure("CLIPBOARD_ALLOCATION_FAILED")
            try:
                ctypes.memmove(pointer, encoded, len(encoded))
            finally:
                self.kernel32.GlobalUnlock(handle)
            if not self._open():
                return self._failure("CLIPBOARD_BUSY")
            before = self._sequence()
            try:
                if not self.user32.EmptyClipboard():
                    return self._failure("CLIPBOARD_WRITE_FAILED")
                if not self.user32.SetClipboardData(CF_UNICODETEXT, handle):
                    return self._failure("CLIPBOARD_WRITE_FAILED")
                transferred = True  # ownership do HGLOBAL passou ao Windows
                after = self._sequence()
                verified = bool(
                    self.user32.IsClipboardFormatAvailable(CF_UNICODETEXT)
                    and after != before
                )
                return {
                    "success": verified,
                    "error_code": None if verified else "CLIPBOARD_VERIFY_FAILED",
                    "effect_verified": verified,
                    "length": len(text),
                    "sequence": after,
                    "content_exposed": False,
                }
            finally:
                self.user32.CloseClipboard()
        finally:
            if not transferred:
                self.kernel32.GlobalFree(handle)

    def clear(self) -> dict:
        if not self._open():
            return self._failure("CLIPBOARD_BUSY")
        before = self._sequence()
        try:
            emptied = bool(self.user32.EmptyClipboard())
            after = self._sequence()
            verified = emptied and not bool(self.user32.EnumClipboardFormats(0))
            return {
                "success": verified,
                "error_code": None if verified else "CLIPBOARD_CLEAR_FAILED",
                "effect_verified": verified,
                "changed": after != before,
                "sequence": after,
                "content_exposed": False,
            }
        finally:
            self.user32.CloseClipboard()

    @staticmethod
    def _failure(error_code: str) -> dict:
        return {
            "success": False,
            "error_code": error_code,
            "effect_verified": False,
            "content_exposed": False,
        }


class ClipboardController:
    """Façade thread-safe e injetável para testes sem tocar no clipboard real."""

    def __init__(self, backend: ClipboardBackend | None = None) -> None:
        self.backend = backend or Win32ClipboardBackend()
        self._lock = threading.Lock()

    def status(self) -> dict:
        with self._lock:
            return self.backend.status()

    def write_text(self, text: str) -> dict:
        with self._lock:
            return self.backend.write_text(text)

    def clear(self) -> dict:
        with self._lock:
            return self.backend.clear()
