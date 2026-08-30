from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.intelligence.models import DiagnosisResult


Check = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]


class DiagnosticsEngine:
    """Evidence-only diagnostic plans; LLM output is never a check result."""

    def __init__(self, *, check_timeout_seconds: float = 8) -> None:
        self.check_timeout_seconds = max(1, check_timeout_seconds)
        self._plans: dict[str, list[tuple[str, Check]]] = {}

    def register(self, domain: str, name: str, check: Check) -> None:
        self._plans.setdefault(domain, []).append((name, check))

    async def run(self, domain: str) -> DiagnosisResult:
        checks = self._plans.get(domain)
        if not checks:
            raise KeyError("DIAGNOSTIC_DOMAIN_NOT_FOUND")
        evidence: list[dict[str, Any]] = []
        for name, check in checks:
            started = time.perf_counter()
            try:
                value = check()
                if asyncio.iscoroutine(value):
                    value = await asyncio.wait_for(value, timeout=self.check_timeout_seconds)
                result = value if isinstance(value, dict) else {"ok": bool(value)}
                ok = bool(result.get("ok", str(result.get("state") or "").upper() in {"READY", "ONLINE", "AVAILABLE"}))
                evidence.append({"check": name, "ok": ok, "result": result,
                                 "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
            except asyncio.TimeoutError:
                evidence.append({"check": name, "ok": False, "error_code": "TIMEOUT", "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
            except Exception as error:
                evidence.append({"check": name, "ok": False, "error_code": type(error).__name__, "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
        failed = [item for item in evidence if not item["ok"]]
        passed = [item for item in evidence if item["ok"]]
        if not failed:
            diagnosis, cause, confidence = f"{domain}: checks passed", None, 0.95
        elif passed:
            diagnosis, cause, confidence = f"{domain}: degraded", f"{failed[0]['check']} failed", min(0.9, 0.55 + len(failed) / max(1, len(evidence)) * 0.3)
        else:
            diagnosis, cause, confidence = f"{domain}: unavailable", "all configured checks failed", 0.85
        return DiagnosisResult(
            diagnosis=diagnosis, probable_cause=cause, confidence=confidence,
            evidence=evidence, failed_checks=failed, passed_checks=passed,
            recommended_action=f"Inspect {failed[0]['check']} evidence before remediation." if failed else None,
            optional_automated_action=None,
        )

    def domains(self) -> list[str]:
        return sorted(self._plans)
