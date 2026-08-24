"""One-off scan: exotic Latin Extended glyphs that never occur in PT-BR text."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

EXOTIC = re.compile(
    "[ǞǟǠǡǤǥǦǧǨǩǪǫǬǭǮǯǰǴǵǶǷǸǹǺǻǼǽǾǿ"
    "ȀȁȂȃȄȅȆȇȈȉȊȋȌȍȎȏȔȕȖȗȘșȚțȜȝȞȟȠȢȣȤȥ"
    "ȦȧȨȩȪȫȬȭȮȯȰȱȲȳǛǜǙǚǕǖǗǘ]"
)


def iter_targets(root: Path):
    skip = {"node_modules", "dist", "__pycache__", ".git", ".venv", "build", "target"}
    for path in root.rglob("*"):
        if any(part in skip for part in path.parts):
            continue
        if path.suffix.casefold() not in {".ts", ".tsx", ".json", ".yaml", ".yml", ".md", ".html", ".css", ".py"}:
            continue
        if path.is_file():
            yield path


def main() -> int:
    out: list[str] = []
    total = 0
    for root in [ROOT / "frontend" / "src", ROOT / "backend" / "app", ROOT / "config",
                 ROOT / "identity", ROOT / "docs"]:
        for path in iter_targets(root):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                out.append(f"NOT-UTF8: {path}")
                continue
            hits = [(i, line) for i, line in enumerate(text.splitlines(), 1) if EXOTIC.search(line)]
            if hits:
                total += len(hits)
                out.append(f"== {path.relative_to(ROOT)}")
                for i, line in hits[:10]:
                    marked = EXOTIC.sub(lambda m: f"[[{m.group(0)} U+{ord(m.group(0)):04X}]]", line.strip())
                    out.append(f"  {i}: {marked[:170]}")
    out.append(f"TOTAL: {total}")
    report = Path(__file__).with_name("exotic_scan_report.txt")
    report.write_text("\n".join(out), encoding="utf-8")
    print(f"lines={total} report={report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
