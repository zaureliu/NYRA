"""Canonical application identity for discovery results.

Discovery sources describe launch routes, not distinct applications.  This
module consolidates those routes before ranking so a Start Menu shortcut, an
App Paths executable and an AUMID for the same product cannot create a false
ambiguity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class CanonicalFamily:
    canonical_id: str
    display_name: str
    aliases: tuple[str, ...]
    process_names: tuple[str, ...] = ()


# These are bootstrap vocabulary, not application-specific control logic.
# Everything outside this small seed list is still consolidated from strong
# discovery identity signals (path, executable, package/AUMID, display name).
CANONICAL_FAMILIES: tuple[CanonicalFamily, ...] = (
    CanonicalFamily(
        "windows_notepad", "Bloco de Notas",
        ("bloco de notas", "notepad", "notepad.exe"),
        ("notepad", "notepad.exe"),
    ),
    CanonicalFamily(
        "visual_studio_code", "Visual Studio Code",
        ("code", "vscode", "vs code", "visual studio code", "code.exe"),
        ("code", "code.exe"),
    ),
    CanonicalFamily(
        "google_chrome", "Google Chrome",
        ("chrome", "google chrome", "chrome.exe"),
        ("chrome", "chrome.exe"),
    ),
    CanonicalFamily(
        "microsoft_edge", "Microsoft Edge",
        ("edge", "microsoft edge", "msedge", "msedge.exe"),
        ("msedge", "msedge.exe"),
    ),
    CanonicalFamily(
        "discord", "Discord", ("discord", "discord.exe"),
        ("discord", "discord.exe"),
    ),
    CanonicalFamily(
        "spotify", "Spotify", ("spotify", "spotify.exe"),
        ("spotify", "spotify.exe"),
    ),
    CanonicalFamily(
        "steam", "Steam", ("steam", "steam.exe"),
        ("steam", "steam.exe"),
    ),
    CanonicalFamily(
        "canva", "Canva", ("canva", "canva.exe"),
        ("canva", "canva.exe"),
    ),
    CanonicalFamily(
        "windows_calculator", "Calculadora",
        ("calculator", "calc", "calculadora", "calc.exe", "calculator.exe"),
        ("calculator", "calculator.exe", "calc", "calc.exe"),
    ),
    CanonicalFamily(
        "microsoft_paint", "Paint",
        ("paint", "mspaint", "mspaint.exe"),
        ("mspaint", "mspaint.exe"),
    ),
    CanonicalFamily(
        "file_explorer", "Explorador de Arquivos",
        ("explorer", "explorer.exe", "explorador", "explorador de arquivos", "file explorer"),
        ("explorer", "explorer.exe"),
    ),
)


def _normalize(value: str) -> str:
    # Local import avoids a discovery -> canonical_apps -> discovery cycle.
    from app.desktop.discovery import normalize

    return normalize(value)


def _family_index() -> dict[str, list[CanonicalFamily]]:
    index: dict[str, list[CanonicalFamily]] = {}
    for family in CANONICAL_FAMILIES:
        values = (family.canonical_id, family.display_name, *family.aliases, *family.process_names)
        for value in values:
            key = _normalize(value)
            if key:
                index.setdefault(key, []).append(family)
    return index


def family_for(value: str) -> CanonicalFamily | None:
    matches = _family_index().get(_normalize(value), [])
    unique = {item.canonical_id: item for item in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def aliases_for_canonical_id(canonical_id: str) -> tuple[str, ...]:
    for family in CANONICAL_FAMILIES:
        if family.canonical_id == canonical_id:
            return family.aliases
    return ()


def display_name_for(value: str) -> str:
    family = family_for(value)
    return family.display_name if family else value.strip()


def _candidate_signals(candidate) -> set[str]:
    from app.desktop.discovery import LaunchMethod, expand_launch_target

    signals: set[str] = set()

    def add(kind: str, value: str) -> None:
        key = _normalize(value)
        if key:
            signals.add(f"{kind}:{key}")

    add("name", candidate.id)
    add("name", candidate.display_name)
    # Aliases participate in query resolution but are not identity evidence by
    # themselves: two genuinely different apps may intentionally share one.
    for process_name in getattr(candidate, "process_names", ()):
        add("exe", Path(process_name).stem)

    method = str(candidate.launch_method)
    target = expand_launch_target(str(candidate.target))
    if method in {LaunchMethod.EXE, LaunchMethod.SHELL_EXECUTE}:
        path = Path(target)
        add("exe", path.stem)
        if path.is_absolute():
            add("path", str(path))
    elif method == LaunchMethod.START_MENU:
        add("name", Path(target).stem)
    elif method == LaunchMethod.APP_USER_MODEL_ID:
        add("aumid", target)
        package = target.split("!", 1)[0]
        if package:
            add("package", package)
    return signals


def _candidate_family(candidate) -> CanonicalFamily | None:
    values = [
        candidate.id,
        candidate.display_name,
        *getattr(candidate, "aliases", ()),
        *getattr(candidate, "process_names", ()),
    ]
    try:
        values.extend((Path(str(candidate.target)).name, Path(str(candidate.target)).stem))
    except (OSError, ValueError):
        pass
    families = {
        family.canonical_id: family
        for value in values
        if (family := family_for(value)) is not None
    }
    return next(iter(families.values())) if len(families) == 1 else None


def _preferred_display(group: list) -> str:
    def score(candidate) -> tuple[float, float, int]:
        name = str(candidate.display_name or "").strip()
        human = 1.0 if name and not name.casefold().endswith(".exe") else 0.0
        casing = 0.3 if any(char.isupper() for char in name) else 0.0
        return (human + casing, float(candidate.confidence), len(name))

    return max(group, key=score).display_name


def canonicalize_candidates(candidates: Iterable) -> list:
    """Return all launch routes with one stable identity per real app.

    A small union-find joins candidates only on exact strong signals.  Fuzzy
    similarity is deliberately excluded here; it belongs to ranking and may
    produce a real ambiguity, while canonicalization must never guess.
    """

    items = list(candidates)
    if not items:
        return []

    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        lroot, rroot = find(left), find(right)
        if lroot != rroot:
            parent[rroot] = lroot

    owners: dict[str, int] = {}
    family_owners: dict[str, int] = {}
    for index, candidate in enumerate(items):
        family = _candidate_family(candidate)
        if family is not None:
            previous = family_owners.setdefault(family.canonical_id, index)
            union(index, previous)
        for signal in _candidate_signals(candidate):
            previous = owners.setdefault(signal, index)
            union(index, previous)

    groups: dict[int, list] = {}
    for index, candidate in enumerate(items):
        groups.setdefault(find(index), []).append(candidate)

    output: list = []
    for group in groups.values():
        matched = {
            family.canonical_id: family
            for candidate in group
            if (family := _candidate_family(candidate)) is not None
        }
        family = next(iter(matched.values())) if len(matched) == 1 else None
        if family is not None:
            canonical_id = family.canonical_id
            display_name = family.display_name
        else:
            display_name = _preferred_display(group)
            explicit_ids = {
                str(candidate.id) for candidate in group if str(candidate.id).strip()
            }
            executable_stems = [
                Path(str(candidate.target)).stem
                for candidate in group
                if str(candidate.launch_method) in {"EXE", "SHELL_EXECUTE"}
                and Path(str(candidate.target)).stem
            ]
            canonical_id = (
                next(iter(explicit_ids))
                if len(explicit_ids) == 1
                else _normalize(executable_stems[0] if executable_stems else display_name)
            )

        aliases: set[str] = set(family.aliases if family else ())
        process_names: set[str] = set(family.process_names if family else ())
        for candidate in group:
            aliases.update((candidate.display_name, candidate.id))
            aliases.update(getattr(candidate, "aliases", ()))
            process_names.update(getattr(candidate, "process_names", ()))
            if str(candidate.launch_method) in {"EXE", "SHELL_EXECUTE"}:
                stem = Path(str(candidate.target)).stem
                if stem:
                    aliases.update((stem, f"{stem}.exe"))
                    process_names.update((stem, f"{stem}.exe"))
        clean_aliases = tuple(sorted(
            {alias.strip() for alias in aliases if alias.strip()},
            key=lambda value: (_normalize(value), value.casefold()),
        ))
        clean_processes = tuple(sorted({
            name.casefold() for name in process_names if name
        }))
        for candidate in group:
            output.append(replace(
                candidate,
                id=canonical_id,
                display_name=display_name,
                aliases=clean_aliases,
                process_names=clean_processes,
            ))
    return output
