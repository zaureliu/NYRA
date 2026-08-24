"""Mojibake detector/repair audit used by tests and scripts.

Detects two corruption classes:
1. non-utf8 files (raw bytes fail strict decode);
2. double-encoded text (valid UTF-8 containing latin-1 views of UTF-8 byte
   pairs, e.g. 'Ã©' for é, 'â€“' for –, plus U+FFFD replacements).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

SCAN_EXTENSIONS = {".ts", ".tsx", ".js", ".json", ".yaml", ".yml", ".md", ".html", ".css", ".py"}
SKIP_DIRS = {"node_modules", "dist", "__pycache__", ".git", ".venv", "build", "target"}

# Latin-1 view of UTF-8 lead/continuation bytes for common PT-BR ranges.
_MOJIBAKE_PAIRS = re.compile(
    "("
    "[ÃÂ][€‚ƒ„…†‡ˆ‰Š‹ŒŽ''""•–—˜™š›œžŸ¡¢£¤¥¦§¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾¿ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖ×ØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõö÷øùúûüýþ]"
    "|â€[™œž]"
    "|ï»¿"
    ")"
)

_REPLACEMENT_CHAR = "\ufffd"


def scan_file(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        raw = path.read_bytes()
    except OSError:
        return problems
    if b"\x00" in raw[:4096]:
        return ["binary"]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"not_utf8@{exc.start}"]
    if _REPLACEMENT_CHAR in text:
        return ["replacement_char"]
    matches = _MOJIBAKE_PAIRS.findall(text)
    if matches:
        # Heuristic guard: single isolated 'Ã' can be legit ("ÃO"); require a
        # classic pair to flag.
        classics = [m for m in matches if m.lower() in {
            "ã", "á", "â", "ã£", "ã©", "ã­", "ã³", "ãº", "ã§", "ã£o",
            "â€", "â€™", "â€œ", "â€", "Â ", "Âº",
        } or len(m) >= 2]
        if classics:
            problems.append(f"mojibake:{len(classics)}")
    return problems


def iter_targets(root: Path):
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.casefold() not in SCAN_EXTENSIONS:
            continue
        if not path.is_file():
            continue
        yield path


def main() -> int:
    targets = [
        PROJECT_ROOT / "frontend" / "src",
        PROJECT_ROOT / "backend" / "app",
        PROJECT_ROOT / "config",
        PROJECT_ROOT / "docs",
        PROJECT_ROOT / "identity",
        PROJECT_ROOT / "scripts",
        PROJECT_ROOT / "frontend" / "index.html",
        PROJECT_ROOT / "frontend" / "desktop.html",
    ]
    self_path = Path(__file__).resolve()
    offenders: list[tuple[Path, str]] = []
    for target in targets:
        if target.is_file():
            issues = scan_file(target)
            if issues:
                offenders.append((target, ",".join(issues)))
            continue
        for path in iter_targets(target):
            if path == self_path or path.name == "exotic_scan.py":
                continue  # estes contêm as classes de caracteres da própria auditoria
            issues = scan_file(path)
            if issues:
                offenders.append((path, ",".join(issues)))
    for path, issue in sorted(offenders)[:80]:
        print(f"{path.relative_to(PROJECT_ROOT)}: {issue}")
    print(f"\n{len(offenders)} arquivos com problemas de encoding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
