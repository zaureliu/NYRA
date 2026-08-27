from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Any, Iterable

from app.selfdev.models import BenchmarkMeasurement, ValidationReport, ValidationStep
from app.selfdev.repository import RepositoryQueryEngine
from app.selfdev.workspace import TestCommandRunner


@dataclass(frozen=True)
class CommandSpec:
    name: str
    command: str
    cwd: Path
    timeout_seconds: int = 300


class TestSelector:
    def __init__(self, query: RepositoryQueryEngine | None = None) -> None:
        self.query = query

    def select(self, candidate_root: Path, changed_files: list[str]) -> list[CommandSpec]:
        commands: list[CommandSpec] = []
        python_files = [path for path in changed_files if path.startswith("backend/") and path.endswith(".py")]
        frontend_files = [path for path in changed_files if path.startswith("frontend/")]
        rust_files = [path for path in changed_files if path.startswith("desktop/src-tauri/")]
        tests: set[str] = set()
        if any(path.startswith("backend/app/selfdev/") for path in python_files):
            tests.add("tests/selfdev")
        if self.query:
            for path in python_files:
                tests.update(item.removeprefix("backend/") for item in self.query.related_tests(Path(path).stem) if item.startswith("backend/tests/"))
        if python_files:
            targets = " ".join(sorted(tests)) if tests else "tests/test_config.py tests/test_events.py"
            commands.append(CommandSpec("backend_targeted", f"python -m pytest -p no:cacheprovider {targets} -q", candidate_root / "backend"))
        if frontend_files:
            commands.extend([
                CommandSpec("frontend_tests", "npm.cmd test", candidate_root / "frontend"),
                CommandSpec("frontend_build", "npm.cmd run build", candidate_root / "frontend"),
            ])
        if rust_files:
            commands.extend([
                CommandSpec("tauri_check", "cargo check", candidate_root / "desktop" / "src-tauri"),
                CommandSpec("tauri_tests", "cargo test", candidate_root / "desktop" / "src-tauri"),
            ])
        commands.append(CommandSpec("git_diff_check", "git diff --check", candidate_root, 60))
        return commands


class SecurityScanner:
    SECRET_RULES = {
        "private_key": re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----"),
        "openai_key": re.compile(r"\bsk-(?:or-)?[A-Za-z0-9_-]{16,}"),
        "assigned_secret": re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*[\"'][^<>\"']{12,}[\"']"),
        "bearer": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}"),
    }
    PERSONAL_PATH = re.compile(r"(?i)\b[A-Z]:\\Users\\(?!<)|/Users/(?!<)|Desktop[\\/]nyra")
    BINARY_SUFFIXES = {".exe", ".msi", ".pdb", ".dmp", ".dll", ".so", ".bin"}
    RUNTIME_PARTS = {
        "data", "logs", "cache", "downloads", "node_modules", "target",
        "dist", "build", ".venv", "venv", ".tmp", ".test-temp",
    }

    def scan(self, root: Path, files: Iterable[str] | None = None, *, public: bool = False) -> list[str]:
        findings: list[str] = []
        candidates = [root / path for path in files] if files is not None else root.rglob("*")
        for path in candidates:
            try:
                relative = path.resolve().relative_to(root.resolve()).as_posix()
            except (OSError, ValueError):
                findings.append("path_escape")
                continue
            if not path.is_file() or ".git" in {part.casefold() for part in Path(relative).parts}:
                continue
            parts = {part.casefold() for part in Path(relative).parts}
            if parts & self.RUNTIME_PARTS:
                findings.append(f"{relative}:runtime_data")
                continue
            if path.suffix.casefold() in self.BINARY_SUFFIXES:
                findings.append(f"{relative}:binary")
                continue
            if path.stat().st_size > 2_000_000:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                for name, pattern in self.SECRET_RULES.items():
                    if pattern.search(line):
                        findings.append(f"{relative}:{line_number}:{name}")
                if public and self.PERSONAL_PATH.search(line):
                    findings.append(f"{relative}:{line_number}:personal_path")
        return sorted(set(findings))


class ValidationPipeline:
    def __init__(self, runner: TestCommandRunner, selector: TestSelector, scanner: SecurityScanner) -> None:
        self.runner = runner
        self.selector = selector
        self.scanner = scanner

    async def validate(self, issue_id: str, candidate_root: Path, changed_files: list[str]) -> ValidationReport:
        report = ValidationReport(issue_id=issue_id, candidate_path=str(candidate_root), changed_files=changed_files)
        findings = self.scanner.scan(candidate_root, changed_files)
        report.security_findings = findings
        report.steps.append(ValidationStep(
            name="security_scan",
            status="FAIL" if findings else "PASS",
            output_summary="; ".join(findings[:50]),
        ))
        if findings:
            report.completed_at = datetime.now(timezone.utc)
            return report
        for spec in self.selector.select(candidate_root, changed_files):
            started = time.perf_counter()
            try:
                result = await self.runner.run(spec.command, spec.cwd, spec.timeout_seconds, f"SelfDev validation {issue_id}: {spec.name}")
                success = bool(result.get("success"))
                output = str(result.get("stdout") or result.get("stderr") or result.get("message") or "")
                status = "PASS" if success else "BLOCKED" if result.get("approval_required") else "FAIL"
            except (OSError, RuntimeError, PermissionError) as error:
                status = "BLOCKED" if isinstance(error, PermissionError) else "FAIL"
                output = type(error).__name__
            report.steps.append(ValidationStep(
                name=spec.name,
                command=spec.command,
                status=status,
                elapsed_seconds=round(time.perf_counter() - started, 3),
                output_summary=output[-4000:],
            ))
            if status != "PASS":
                break
        report.passed = bool(report.steps) and all(step.status == "PASS" for step in report.steps)
        report.completed_at = datetime.now(timezone.utc)
        return report


class RegressionDetector:
    def compare(self, baseline: ValidationReport, candidate: ValidationReport) -> dict[str, Any]:
        base_failures = sum(step.status != "PASS" for step in baseline.steps)
        candidate_failures = sum(step.status != "PASS" for step in candidate.steps)
        base_seconds = sum(step.elapsed_seconds for step in baseline.steps)
        candidate_seconds = sum(step.elapsed_seconds for step in candidate.steps)
        return {
            "regression": candidate_failures > base_failures,
            "baseline_failures": base_failures,
            "candidate_failures": candidate_failures,
            "duration_delta_seconds": round(candidate_seconds - base_seconds, 3),
        }


class BenchmarkComparator:
    def compare(self, metric: str, before: list[float], after: list[float], *, lower_is_better: bool = True) -> BenchmarkMeasurement:
        if not before or not after:
            raise ValueError("benchmark requires before and after samples")
        baseline = sum(before) / len(before)
        candidate = sum(after) / len(after)
        delta = ((candidate - baseline) / baseline * 100) if baseline else 0.0
        improved = candidate < baseline if lower_is_better else candidate > baseline
        return BenchmarkMeasurement(
            metric=metric,
            before=round(baseline, 4),
            after=round(candidate, 4),
            delta_percent=round(delta, 4),
            sample_count=min(len(before), len(after)),
            improved=improved,
        )
