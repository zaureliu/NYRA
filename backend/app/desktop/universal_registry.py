"""Universal Application Registry (kazumi-full §2/§3/§4/§6/§30).

Índice persistente e regenerável de todos os aplicativos utilizáveis no
Windows. Fontes de descoberta vivem em `discovery.ApplicationDiscovery`;
este módulo adiciona:

* persistência em DATA_ROOT/app-registry (nunca no repo);
* alias engine com aprendizado SOMENTE após sucesso verificado;
* estatísticas de uso (launch_count / last_launch_success / last_verified);
* resolução rápida por alias aprendido antes da busca fuzzy completa.

Nenhum secret é persistido.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.paths import DATA_ROOT
from app.desktop.discovery import (
    ApplicationCandidate,
    ApplicationDiscovery,
    LaunchMethod,
    expand_launch_target,
    normalize,
)

logger = logging.getLogger("kazumi.universal_apps")

REGISTRY_DIRNAME = "app-registry"
INDEX_FILENAME = "index.json"
LEARNED_FILENAME = "learned.json"

# Sementes PT-BR genéricas — fallback apenas (kazumi-full §4: nunca fonte única).
_GENERIC_ALIASES: dict[str, tuple[str, ...]] = {
    "code.exe": ("vs code", "vscode", "visual studio code", "editor", "editor de codigo"),
    "notepad.exe": ("bloco de notas", "editor de texto"),
    "msedge.exe": ("navegador da microsoft", "navegador edge", "edge", "microsoft edge"),
    "chrome.exe": ("google chrome", "navegador google"),
    "explorer.exe": ("explorador de arquivos", "gerenciador de arquivos"),
    "cmd.exe": ("prompt de comando", "terminal cmd"),
    "powershell.exe": ("terminal powershell",),
    "taskmgr.exe": ("gerenciador de tarefas",),
    "calc.exe": ("calculadora",),
    "mspaint.exe": ("paint", "paintbrush"),
}


@dataclass
class UniversalAppEntry:
    app_id: str
    display_name: str
    executable: str = ""
    launch_method: str = ""
    target: str = ""
    working_directory: str = ""
    aumid: str = ""
    start_menu_link: str = ""
    publisher: str = ""
    version: str = ""
    install_location: str = ""
    source: str = ""
    confidence: float = 0.0
    executable_paths: list[str] = field(default_factory=list)
    start_menu_entries: list[str] = field(default_factory=list)
    aumids: list[str] = field(default_factory=list)
    package_ids: list[str] = field(default_factory=list)
    process_names: list[str] = field(default_factory=list)
    launch_options: list[dict[str, Any]] = field(default_factory=list)
    preferred_launch_method: str = ""
    preferred_target: str = ""
    preferred_source: str = ""
    aliases: list[str] = field(default_factory=list)
    last_verified: float = 0.0
    launch_count: int = 0
    last_launch_success: float = 0.0


class UniversalAppRegistry:
    """Índice persistente + aprendizado de aliases verificados."""

    def __init__(
        self,
        discovery: ApplicationDiscovery | None = None,
        root: Path | None = None,
    ) -> None:
        self.discovery = discovery or ApplicationDiscovery()
        self.root = root or (DATA_ROOT / REGISTRY_DIRNAME)
        self.index_path = self.root / INDEX_FILENAME
        self.learned_path = self.root / LEARNED_FILENAME
        self._lock = threading.Lock()
        self.entries: dict[str, UniversalAppEntry] = {}
        self.learned_aliases: dict[str, str] = {}  # normalized alias -> app_id
        self.last_refresh: float = 0.0
        self.sources_count: dict[str, int] = {}
        self._load()

    # ------------------------------------------------------------ persistence

    def _ensure_dir(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("universal_registry_dir_unavailable path=%s", self.root)

    def _atomic_write(self, path: Path, payload: dict) -> None:
        self._ensure_dir()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(temporary, path)

    def _load(self) -> None:
        for path, target in ((self.index_path, "index"), (self.learned_path, "learned")):
            if not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            with self._lock:
                if target == "learned" and isinstance(raw.get("aliases"), dict):
                    self.learned_aliases = {
                        normalize(str(k)): str(v) for k, v in raw["aliases"].items()
                    }
                    continue
                if target == "index" and isinstance(raw.get("entries"), list):
                    entries: dict[str, UniversalAppEntry] = {}
                    for item in raw["entries"][:4000]:
                        try:
                            entry = UniversalAppEntry(
                                app_id=str(item.get("app_id") or ""),
                                display_name=str(item.get("display_name") or ""),
                                executable=str(item.get("executable") or ""),
                                launch_method=str(item.get("launch_method") or ""),
                                target=str(item.get("target") or ""),
                                aumid=str(item.get("aumid") or ""),
                                start_menu_link=str(item.get("start_menu_link") or ""),
                                source=str(item.get("source") or ""),
                                confidence=float(item.get("confidence") or 0.0),
                                executable_paths=[str(value) for value in item.get("executable_paths", [])][:32],
                                start_menu_entries=[str(value) for value in item.get("start_menu_entries", [])][:32],
                                aumids=[str(value) for value in item.get("aumids", [])][:32],
                                package_ids=[str(value) for value in item.get("package_ids", [])][:32],
                                process_names=[str(value) for value in item.get("process_names", [])][:32],
                                launch_options=[
                                    dict(option)
                                    for option in item.get("launch_options", [])
                                    if isinstance(option, dict)
                                ][:32],
                                preferred_launch_method=str(
                                    item.get("preferred_launch_method") or ""
                                ),
                                preferred_target=str(item.get("preferred_target") or ""),
                                preferred_source=str(item.get("preferred_source") or ""),
                                aliases=[str(a) for a in item.get("aliases", [])][:16],
                                last_verified=float(item.get("last_verified") or 0.0),
                                launch_count=int(item.get("launch_count") or 0),
                                last_launch_success=float(item.get("last_launch_success") or 0.0),
                            )
                        except (TypeError, ValueError):
                            continue
                        if entry.app_id and entry.display_name:
                            entries[entry.app_id] = entry
                    self.entries = entries
                    self.last_refresh = float(raw.get("refreshed_at") or 0.0)
                    sources = raw.get("sources")
                    self.sources_count = dict(sources) if isinstance(sources, dict) else {}

    def save_index(self) -> None:
        with self._lock:
            entries = [entry.__dict__.copy() for entry in self.entries.values()]
            payload = {
                "version": 2,
                "refreshed_at": self.last_refresh,
                "sources": self.sources_count,
                "entries": entries,
            }
        try:
            self._atomic_write(self.index_path, payload)
        except OSError as error:
            logger.warning("universal_registry_save_failed type=%s", type(error).__name__)

    def save_learned(self) -> None:
        with self._lock:
            payload = {"version": 1, "aliases": dict(self.learned_aliases)}
        try:
            self._atomic_write(self.learned_path, payload)
        except OSError as error:
            logger.warning("universal_learned_save_failed type=%s", type(error).__name__)

    # ---------------------------------------------------------------- refresh

    def refresh(self, force: bool = True) -> dict[str, int]:
        """Reconstrói o índice a partir das fontes do discovery (kazumi-full §6)."""
        from app.desktop.canonical_apps import canonicalize_candidates

        candidates = canonicalize_candidates(self.discovery.index(force=force))
        now = time.time()
        entries: dict[str, UniversalAppEntry] = {}
        sources: dict[str, int] = {}
        for candidate in candidates:
            entry = entries.get(candidate.id)
            if entry is None:
                entry = self._entry_from_candidate(candidate)
                entries[entry.app_id] = entry
            else:
                self._merge_candidate(entry, candidate)
            sources[candidate.source] = sources.get(candidate.source, 0) + 1
        for app_id, entry in entries.items():
            previous = self.entries.get(app_id)
            if previous is None:
                continue
            entry.launch_count = previous.launch_count
            entry.last_launch_success = previous.last_launch_success
            entry.last_verified = previous.last_verified
            learned = [
                alias for alias in previous.aliases
                if alias.startswith("learned:") and alias not in entry.aliases
            ]
            entry.aliases.extend(learned)
            preferred_key = (
                previous.preferred_launch_method,
                expand_launch_target(previous.preferred_target).casefold(),
            )
            available = {
                (
                    str(option.get("launch_method") or ""),
                    expand_launch_target(str(option.get("target") or "")).casefold(),
                )
                for option in entry.launch_options
            }
            available.update({
                (
                    LaunchMethod.SHELL_EXECUTE,
                    expand_launch_target(str(option.get("target") or "")).casefold(),
                )
                for option in entry.launch_options
                if str(option.get("launch_method") or "") == LaunchMethod.EXE
            })
            if preferred_key in available:
                entry.preferred_launch_method = previous.preferred_launch_method
                entry.preferred_target = previous.preferred_target
                entry.preferred_source = previous.preferred_source
                entry.launch_method = previous.preferred_launch_method
                entry.target = previous.preferred_target
                entry.source = previous.preferred_source or entry.source
            if any(
                self.discovery.revalidate(self._candidate_from_option(entry, option))
                for option in entry.launch_options
            ):
                entry.last_verified = now
        with self._lock:
            self.entries = entries
            migrated_aliases: dict[str, str] = {}
            for alias, old_app_id in self.learned_aliases.items():
                if old_app_id in entries:
                    migrated_aliases[alias] = old_app_id
                    continue
                matches = [
                    entry.app_id for entry in entries.values()
                    if alias in {
                        normalize(value.removeprefix("learned:"))
                        for value in entry.aliases
                    }
                ]
                if len(matches) == 1:
                    migrated_aliases[alias] = matches[0]
            self.learned_aliases = migrated_aliases
            self.sources_count = sources
            self.last_refresh = now
        self.save_index()
        logger.info(
            "universal_registry_refreshed apps=%s sources=%s",
            len(entries), sorted(sources.items()),
        )
        return sources

    @staticmethod
    def _entry_from_candidate(candidate: ApplicationCandidate) -> UniversalAppEntry:
        method = str(candidate.launch_method)
        entry = UniversalAppEntry(
            app_id=candidate.id,
            display_name=candidate.display_name,
            executable=Path(candidate.target).name if method == LaunchMethod.EXE else "",
            launch_method=method,
            target=candidate.target,
            aumid=candidate.target if method == LaunchMethod.APP_USER_MODEL_ID else "",
            start_menu_link=candidate.target if method == LaunchMethod.START_MENU else "",
            source=candidate.source,
            confidence=candidate.confidence,
            executable_paths=(
                [expand_launch_target(candidate.target)]
                if method == LaunchMethod.EXE else []
            ),
            start_menu_entries=(
                [candidate.target] if method == LaunchMethod.START_MENU else []
            ),
            aumids=(
                [candidate.target]
                if method == LaunchMethod.APP_USER_MODEL_ID else []
            ),
            package_ids=(
                [candidate.target.split("!", 1)[0]]
                if method == LaunchMethod.APP_USER_MODEL_ID else []
            ),
            process_names=list(candidate.process_names),
            launch_options=[UniversalAppRegistry._option_from_candidate(candidate)],
        )
        entry.aliases = list(dict.fromkeys([*build_aliases(entry), *candidate.aliases]))
        return entry

    @staticmethod
    def _option_from_candidate(candidate: ApplicationCandidate) -> dict[str, Any]:
        return {
            "launch_method": str(candidate.launch_method),
            "target": candidate.target,
            "source": candidate.source,
            "confidence": float(candidate.confidence),
            "expected_window": bool(candidate.expected_window),
            "process_names": list(candidate.process_names),
            "aliases": list(candidate.aliases),
        }

    @staticmethod
    def _candidate_from_option(
        entry: UniversalAppEntry, option: dict[str, Any]
    ) -> ApplicationCandidate:
        return ApplicationCandidate(
            id=entry.app_id,
            display_name=entry.display_name,
            source=str(option.get("source") or entry.source),
            launch_method=str(option.get("launch_method") or entry.launch_method),
            target=str(option.get("target") or entry.target),
            confidence=float(option.get("confidence") or entry.confidence),
            expected_window=bool(option.get("expected_window", True)),
            process_names=tuple(option.get("process_names") or entry.process_names),
            aliases=tuple(option.get("aliases") or entry.aliases),
        )

    @classmethod
    def _merge_candidate(
        cls, entry: UniversalAppEntry, candidate: ApplicationCandidate
    ) -> None:
        option = cls._option_from_candidate(candidate)
        key = (
            str(option["launch_method"]),
            expand_launch_target(str(option["target"])).casefold(),
        )
        existing_keys = {
            (
                str(item.get("launch_method") or ""),
                expand_launch_target(str(item.get("target") or "")).casefold(),
            )
            for item in entry.launch_options
        }
        if key not in existing_keys:
            entry.launch_options.append(option)
        method = str(candidate.launch_method)
        if method == LaunchMethod.EXE and not entry.executable:
            entry.executable = Path(candidate.target).name
        if method == LaunchMethod.EXE:
            expanded = expand_launch_target(candidate.target)
            if expanded and expanded not in entry.executable_paths:
                entry.executable_paths.append(expanded)
        elif method == LaunchMethod.APP_USER_MODEL_ID and not entry.aumid:
            entry.aumid = candidate.target
        if method == LaunchMethod.APP_USER_MODEL_ID:
            if candidate.target not in entry.aumids:
                entry.aumids.append(candidate.target)
            package = candidate.target.split("!", 1)[0]
            if package and package not in entry.package_ids:
                entry.package_ids.append(package)
        elif method == LaunchMethod.START_MENU and not entry.start_menu_link:
            entry.start_menu_link = candidate.target
        if method == LaunchMethod.START_MENU and candidate.target not in entry.start_menu_entries:
            entry.start_menu_entries.append(candidate.target)
        entry.process_names = list(dict.fromkeys([
            *entry.process_names, *candidate.process_names,
        ]))
        if candidate.confidence > entry.confidence:
            entry.display_name = candidate.display_name
            entry.launch_method = method
            entry.target = candidate.target
            entry.source = candidate.source
            entry.confidence = candidate.confidence
        entry.aliases = list(dict.fromkeys([*entry.aliases, *build_aliases(entry)]))
        entry.aliases = list(dict.fromkeys([*entry.aliases, *candidate.aliases]))

    # ------------------------------------------------------------- resolution

    def status(self) -> dict[str, Any]:
        with self._lock:
            total_aliases = sum(len(entry.aliases) for entry in self.entries.values())
            return {
                "apps": len(self.entries),
                "aliases": total_aliases + len(self.learned_aliases),
                "sources": dict(sorted(self.sources_count.items())),
                "last_refresh": self.last_refresh,
                "learned_aliases": len(self.learned_aliases),
                "persist_path": str(self.root),
            }

    def _resolve_entry(self, query: str) -> UniversalAppEntry | None:
        """Alias aprendido/exato → candidato direto sem busca fuzzy completa."""
        key = normalize(query)
        if not key:
            return None
        app_id = self.learned_aliases.get(key)
        if app_id is None:
            entry_exact = self.entries.get(key)
            app_id = entry_exact.app_id if entry_exact else None
        if app_id is None:
            matches = [
                entry for entry in self.entries.values()
                if key in {
                    normalize(alias.removeprefix("learned:"))
                    for alias in entry.aliases
                }
            ]
            # Exact shared aliases are a real ambiguity only when they map to
            # canonically different entries.  Never pick the first by order.
            if len(matches) == 1:
                app_id = matches[0].app_id
        if not app_id:
            return None
        return self.entries.get(app_id)

    def resolve_launch_candidates(
        self,
        query: str,
        *,
        fallback: ApplicationCandidate | None = None,
    ) -> list[ApplicationCandidate]:
        """Return every local route for one app, verified preference first."""
        from app.desktop.launch_policy import ordered_launch_candidates

        entry = self._resolve_entry(query)
        if entry is None and fallback is not None:
            entry = self.entries.get(fallback.id)
        candidates: list[ApplicationCandidate] = []
        preferred_method = ""
        preferred_target = ""
        if entry is not None:
            options = entry.launch_options or [{
                "launch_method": entry.launch_method or LaunchMethod.EXE,
                "target": entry.aumid or entry.start_menu_link or entry.target,
                "source": entry.source,
                "confidence": entry.confidence,
                "expected_window": True,
            }]
            candidates.extend(
                self._candidate_from_option(entry, option) for option in options
            )
            preferred_method = entry.preferred_launch_method
            preferred_target = entry.preferred_target
        if fallback is not None:
            candidates.extend(self.discovery.candidates_for(fallback.id))
            candidates.append(fallback)
        return ordered_launch_candidates(
            candidates,
            preferred_method=preferred_method,
            preferred_target=preferred_target,
        )

    def resolve_identity(self, query: str) -> dict[str, Any]:
        """Resolve query to canonical apps, separate from their launch routes."""
        key = normalize(query)
        if not key:
            return {"status": "NOT_FOUND", "entries": []}
        learned_id = self.learned_aliases.get(key)
        if learned_id in self.entries:
            return {"status": "EXACT_MATCH", "entry": self.entries[learned_id],
                    "entries": [self.entries[learned_id]], "confidence": 1.0}

        from app.desktop.discovery import score_match

        ranked: list[tuple[float, UniversalAppEntry]] = []
        for entry in self.entries.values():
            names = [entry.display_name, entry.app_id, *entry.aliases]
            confidence = max((score_match(query, name.removeprefix("learned:"))
                              for name in names if name), default=0.0)
            if confidence > 0:
                ranked.append((confidence, entry))
        ranked.sort(key=lambda item: (-item[0], -item[1].last_launch_success,
                                      item[1].display_name))
        exact = [item for item in ranked if item[0] >= 1.0]
        if len(exact) == 1:
            return {"status": "EXACT_MATCH", "entry": exact[0][1],
                    "entries": [exact[0][1]], "confidence": exact[0][0]}
        if len(exact) > 1:
            return {"status": "AMBIGUOUS", "entries": [item[1] for item in exact[:4]],
                    "confidence": 1.0}
        high = [item for item in ranked if item[0] >= 0.85]
        if len(high) == 1:
            return {"status": "HIGH_CONFIDENCE", "entry": high[0][1],
                    "entries": [high[0][1]], "confidence": high[0][0]}
        if len(high) > 1:
            return {"status": "AMBIGUOUS", "entries": [item[1] for item in high[:4]],
                    "confidence": high[0][0]}
        return {"status": "NOT_FOUND" if not ranked else "AMBIGUOUS",
                "entries": [item[1] for item in ranked[:4]],
                "confidence": ranked[0][0] if ranked else 0.0}

    def resolve_fast(self, query: str) -> ApplicationCandidate | None:
        """Return the preferred exact/learned candidate without fuzzy search."""
        candidates = self.resolve_launch_candidates(query)
        return candidates[0] if candidates else None

    def record_success(
        self,
        app_id: str,
        alias_query: str | None = None,
        launch_candidate: ApplicationCandidate | None = None,
    ) -> None:
        """Aprendizado pós-sucesso verificado (kazumi-full §30)."""
        entry = self.entries.get(app_id)
        if entry is None and launch_candidate is not None:
            entry = self._entry_from_candidate(launch_candidate)
            self.entries[entry.app_id] = entry
        if entry is None:
            return
        with self._lock:
            entry.launch_count += 1
            entry.last_launch_success = time.time()
            entry.last_verified = time.time()
            if launch_candidate is not None:
                self._merge_candidate(entry, launch_candidate)
                entry.preferred_launch_method = str(launch_candidate.launch_method)
                entry.preferred_target = launch_candidate.target
                entry.preferred_source = launch_candidate.source
                entry.launch_method = str(launch_candidate.launch_method)
                entry.target = launch_candidate.target
                entry.source = launch_candidate.source
            if alias_query:
                cleaned = " ".join(alias_query.casefold().split())
                norm_alias = normalize(cleaned)
                tagged = f"learned:{cleaned}"
                if norm_alias and norm_alias != entry.app_id and tagged not in entry.aliases:
                    entry.aliases.append(tagged)
                if norm_alias:
                    self.learned_aliases[norm_alias] = app_id
        self.save_index()
        self.save_learned()

    def record_failure(self, app_id: str) -> None:
        entry = self.entries.get(app_id)
        if entry is None:
            return
        entry.last_verified = time.time()

    def process_names_for(self, app_id: str) -> list[str]:
        """Nomes de processo plausíveis para localizar janelas do app."""
        entry = self.entries.get(app_id)
        if entry is None:
            return []
        names: list[str] = []
        names.extend(entry.process_names)
        if entry.executable:
            stem = Path(entry.executable).stem.casefold()
            names.extend({f"{stem}.exe", stem})
        for option in entry.launch_options:
            method = str(option.get("launch_method") or "")
            if method not in {LaunchMethod.EXE, LaunchMethod.SHELL_EXECUTE}:
                continue
            stem = Path(str(option.get("target") or "")).stem.casefold()
            if stem:
                names.extend({f"{stem}.exe", stem})
        target_stem = Path(entry.target).stem.casefold() if entry.target else ""
        if target_stem and target_stem not in names:
            names.append(target_stem)
        return names


def build_aliases(entry: UniversalAppEntry) -> list[str]:
    """Alias engine automática (kazumi-full §4): nome oficial + exe + seeds."""
    aliases: set[str] = {entry.app_id}
    display = entry.display_name.strip()
    if display:
        aliases.add(display.casefold())
        tokens = normalize(display)
        if tokens:
            aliases.add(tokens)
    exe_stem = Path(entry.executable).stem if entry.executable else ""
    if exe_stem and exe_stem.casefold() not in {"application", "app"}:
        aliases.add(exe_stem.casefold())
        aliases.add(f"{exe_stem.casefold()}.exe")
    link_stem = Path(entry.start_menu_link).stem if entry.start_menu_link else ""
    if link_stem:
        aliases.add(link_stem.casefold())
    for seed in _GENERIC_ALIASES.get(f"{exe_stem.casefold()}.exe", ()):  # fallback apenas
        aliases.add(seed)
    from app.desktop.canonical_apps import aliases_for_canonical_id

    aliases.update(aliases_for_canonical_id(entry.app_id))
    aliases.update(entry.process_names)
    return [alias for alias in aliases if alias]
