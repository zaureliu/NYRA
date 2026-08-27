"""Deterministic launch fallback policy for discovered Windows applications.

Only candidates produced by the local Application Registry enter this module.
It never accepts an operator supplied path and never executes anything itself.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.desktop.discovery import ApplicationCandidate, LaunchMethod, expand_launch_target


def candidate_key(candidate: ApplicationCandidate) -> tuple[str, str]:
    """Stable identity for one concrete launch route."""
    return (
        str(candidate.launch_method),
        expand_launch_target(candidate.target).casefold(),
    )


def option_key(method: str, target: str) -> tuple[str, str]:
    return (str(method), expand_launch_target(target).casefold())


def _route_rank(candidate: ApplicationCandidate) -> int:
    """Order the Windows launch routes requested by the universal launcher."""
    source = (candidate.source or "").casefold()
    method = str(candidate.launch_method)
    if method == LaunchMethod.START_MENU:
        return 10
    if method == LaunchMethod.EXE and not source.startswith("app_paths:") and source not in {
        "path", "shell_known"
    }:
        return 20
    if method == LaunchMethod.EXE and source.startswith("app_paths:"):
        return 30
    if method in {LaunchMethod.APP_USER_MODEL_ID, LaunchMethod.URI}:
        return 40
    if method == LaunchMethod.SHELL_EXECUTE:
        return 50
    if method == LaunchMethod.EXE:
        # PATH and command-name candidates are intentionally last: shutil.which
        # is the Get-Command equivalent used by the Python runtime.
        return 60
    return 70


def _process_names(candidates: list[ApplicationCandidate]) -> tuple[str, ...]:
    names: set[str] = set()
    for candidate in candidates:
        method = str(candidate.launch_method)
        target = expand_launch_target(candidate.target)
        if method in {LaunchMethod.EXE, LaunchMethod.SHELL_EXECUTE}:
            stem = Path(target).stem.casefold()
            if stem and stem not in {"application", "app"}:
                names.update({stem, f"{stem}.exe"})
        elif method == LaunchMethod.START_MENU:
            stem = Path(target).stem.casefold()
            if stem:
                names.update({stem, f"{stem}.exe"})
        elif method == LaunchMethod.APP_USER_MODEL_ID and "!" in target:
            tail = target.rsplit("!", 1)[-1].casefold()
            if tail and tail not in {"application", "app"}:
                names.update({tail, f"{tail}.exe"})
    return tuple(sorted(names))


def ordered_launch_candidates(
    candidates: list[ApplicationCandidate],
    *,
    preferred_method: str = "",
    preferred_target: str = "",
) -> list[ApplicationCandidate]:
    """Deduplicate, add safe ShellExecute fallbacks, and order all routes.

    ShellExecute variants are synthesized only from executable candidates that
    were already discovered locally. No external or arbitrary target is added.
    """
    unique: dict[tuple[str, str], ApplicationCandidate] = {}
    for candidate in candidates:
        if not candidate.target:
            continue
        key = candidate_key(candidate)
        existing = unique.get(key)
        if existing is None or candidate.confidence > existing.confidence:
            unique[key] = candidate

    original = list(unique.values())
    for candidate in original:
        if candidate.launch_method != LaunchMethod.EXE:
            continue
        shell_candidate = replace(
            candidate,
            source=f"shell_execute:{candidate.source}",
            launch_method=LaunchMethod.SHELL_EXECUTE,
        )
        unique.setdefault(candidate_key(shell_candidate), shell_candidate)

    process_names = _process_names(list(unique.values()))
    enriched = [replace(candidate, process_names=process_names) for candidate in unique.values()]
    preferred = option_key(preferred_method, preferred_target) if preferred_method and preferred_target else None
    enriched.sort(
        key=lambda candidate: (
            0 if preferred is not None and candidate_key(candidate) == preferred else 1,
            _route_rank(candidate),
            -candidate.confidence,
            candidate.source.casefold(),
            candidate.target.casefold(),
        )
    )
    return enriched
