"""Dynamic Application Discovery: locate installed apps without manual registry.

Sources searched safely, read-only:
  - well-known shell applications (notepad, calc, mspaint, explorer, ...)
  - Windows App Paths registry (HKLM/HKCU ...CurrentVersion\\App Paths)
  - PATH lookup (shutil.which)
  - Start Menu shortcuts (.lnk) from ProgramData and %AppData%
  - Get-StartApps output (covers UWP/MSIX apps via AppUserModelID)

Every candidate carries provenance and a confidence score. Exact display-name
matches win; multiple plausible candidates are returned as AMBIGUOUS so the
operator decides instead of KAZUMI guessing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from app.tools.redaction import redact_secrets


class LaunchMethod:
    EXE = "EXE"
    SHELL_EXECUTE = "SHELL_EXECUTE"
    APP_USER_MODEL_ID = "APP_USER_MODEL_ID"
    URI = "URI"
    START_MENU = "START_MENU"
    FILE_ASSOCIATION = "FILE_ASSOCIATION"


@dataclass
class ApplicationCandidate:
    id: str
    display_name: str
    source: str
    launch_method: str
    target: str
    confidence: float = 0.0
    expected_window: bool = True
    process_names: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "source": self.source,
            "launch_method": self.launch_method,
            "target": redact_secrets(self.target),
            "confidence": round(self.confidence, 3),
            "expected_window": self.expected_window,
            "process_names": list(self.process_names),
            "aliases": list(self.aliases),
        }


_KNOWN_APPS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("bloco de notas", "notepad", "notepad.exe"), "notepad.exe"),
    (("calculadora", "calculator", "calc", "calc.exe"), "calc.exe"),
    (("paint", "mspaint", "mspaint.exe", "paintbrush"), "mspaint.exe"),
    (("explorador de arquivos", "file explorer", "explorer", "explorer.exe"), "explorer.exe"),
    (("prompt de comando", "command prompt", "cmd", "cmd.exe"), "cmd.exe"),
    (("powershell", "powershell.exe"), "powershell.exe"),
    (("terminal", "windows terminal", "wt"), "wt.exe"),
    (("gerenciador de tarefas", "task manager", "taskmgr", "taskmgr.exe"), "taskmgr.exe"),
    (("painel de controle", "control panel", "control"), "control.exe"),
    (("vs code", "vscode", "visual studio code", "code.exe"),
     "%LOCALAPPDATA%\\Programs\\Microsoft VS Code\\Code.exe"),
    (("configuracoes", "configurações", "settings", "ms-settings:"), "ms-settings:"),
    (("executar", "run dialog", "run"), None),
)

_URIS = {"ms-settings:": "ms-settings:"}

_SANITIZE = re.compile(r"[^a-z0-9]+")
_TOKEN_SPLIT = re.compile(r"[\s\-_.]+")


def normalize(value: str) -> str:
    """Minúsculas sem acentos e sem não-alfanuméricos.

    Diacríticos são REMOVIDOS (NFKD + combining filter), não descartados
    junto com a pontuação: 'Músicas' e 'musicas' normalizam iguais.
    """
    decomposed = unicodedata.normalize("NFKD", (value or "").casefold().strip())
    unmarked = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _SANITIZE.sub("", unmarked)


def expand_launch_target(value: str) -> str:
    """Resolve local environment/user markers without invoking a shell."""
    target = (value or "").strip()
    if len(target) >= 2 and target[0] == target[-1] and target[0] in {'"', "'"}:
        target = target[1:-1]
    return os.path.expanduser(os.path.expandvars(target))


def query_tokens(value: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT.split((value or "").casefold()) if token]


def score_match(query: str, candidate_name: str) -> float:
    """0..1 similarity; exact normalized equality = 1.0."""
    q, c = normalize(query), normalize(candidate_name)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if c.startswith(q) or q.startswith(c):
        return 0.92 if min(len(q), len(c)) >= 4 else 0.8
    tokens_q = set(query_tokens(query))
    tokens_c = set(query_tokens(candidate_name))
    if tokens_q and tokens_c:
        overlap = len(tokens_q & tokens_c) / max(len(tokens_q), len(tokens_c))
        if overlap >= 0.5:
            return 0.7 + 0.15 * overlap
    if q in c:
        return 0.65
    return 0.0


@dataclass
class DiscoveryCache:
    entries: list[ApplicationCandidate] = field(default_factory=list)
    indexed_at: float = 0.0
    ttl_seconds: float = 600.0

    def valid(self) -> bool:
        return bool(self.entries) and (time.monotonic() - self.indexed_at) < self.ttl_seconds

    def invalidate(self) -> None:
        self.entries = []
        self.indexed_at = 0.0


class ApplicationDiscovery:
    def __init__(self, cache_ttl_seconds: float = 600.0, enabled: bool = True) -> None:
        self.cache = DiscoveryCache(ttl_seconds=cache_ttl_seconds)
        self.enabled = enabled

    # ------------------------------------------------------------- sources

    @staticmethod
    def _known_candidates() -> list[ApplicationCandidate]:
        candidates: list[ApplicationCandidate] = []
        for names, target in _KNOWN_APPS:
            if not target:
                continue
            primary = names[0]
            uri = _URIS.get(target)
            method = LaunchMethod.URI if uri else LaunchMethod.EXE
            candidates.append(ApplicationCandidate(
                id=normalize(primary),
                display_name=primary.title() if not primary.islower() else primary,
                source="shell_known",
                launch_method=method,
                target=uri or target,
                confidence=1.0,
            ))
        return candidates

    @staticmethod
    def _app_paths_candidates() -> list[ApplicationCandidate]:
        candidates: list[ApplicationCandidate] = []
        try:
            import winreg
        except ImportError:
            return candidates
        for hive, root_label in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
            try:
                key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
                with winreg.OpenKey(hive, key_path) as root:
                    index = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(root, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(root, subkey_name) as subkey:
                                target, _ = winreg.QueryValueEx(subkey, "")
                        except OSError:
                            continue
                        exe_name = Path(subkey_name).stem
                        candidates.append(ApplicationCandidate(
                            id=normalize(exe_name),
                            display_name=exe_name,
                            source=f"app_paths:{root_label}",
                            launch_method=LaunchMethod.EXE,
                            target=expand_launch_target(target or subkey_name),
                            confidence=0.9,
                        ))
            except OSError:
                continue
        return candidates

    @staticmethod
    def _path_candidates() -> list[ApplicationCandidate]:
        candidates: list[ApplicationCandidate] = []
        seen: set[str] = set()
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            try:
                entries = list(Path(directory).glob("*.exe"))
            except OSError:
                continue
            for entry in entries[:64]:
                stem = entry.stem.casefold()
                if stem in seen or not entry.is_file():
                    continue
                seen.add(stem)
                candidates.append(ApplicationCandidate(
                    id=normalize(stem),
                    display_name=entry.stem,
                    source="path",
                    launch_method=LaunchMethod.EXE,
                    target=str(entry),
                    confidence=0.6,
                ))
        return candidates

    @staticmethod
    def start_menu_directories() -> list[Path]:
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        app_data = os.environ.get("APPDATA", "")
        dirs = [Path(program_data) / "Microsoft" / "Windows" / "Start Menu"]
        if app_data:
            dirs.append(Path(app_data) / "Microsoft" / "Windows" / "Start Menu")
        return [item for item in dirs if item.is_dir()]

    @classmethod
    def _start_menu_candidates(cls) -> list[ApplicationCandidate]:
        candidates: list[ApplicationCandidate] = []
        seen: set[str] = set()
        for base in cls.start_menu_directories():
            try:
                links = list(base.rglob("*.lnk"))
            except OSError:
                continue
            for link in links[:400]:
                stem = link.stem
                key = normalize(stem)
                if not key or key in seen:
                    continue
                seen.add(key)
                candidates.append(ApplicationCandidate(
                    id=key,
                    display_name=stem,
                    source="start_menu",
                    launch_method=LaunchMethod.START_MENU,
                    target=str(link),
                    confidence=0.75,
                ))
        return candidates

    @staticmethod
    def _get_start_apps_candidates() -> list[ApplicationCandidate]:
        """UWP/MSIX + registered Start apps via Get-StartApps (cached at index time)."""
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "Get-StartApps | ForEach-Object { \"$($_.Name)`t$($_.AppID)\" }"
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=12, creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        candidates: list[ApplicationCandidate] = []
        for line in decode_lines(completed.stdout).splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            name, app_id = parts[0].strip(), parts[1].strip()
            if not name or not app_id or " " not in app_id and "\\" not in app_id and not app_id.startswith("{"):
                # AUMIDs contain a separator/dot pattern; plain paths are skipped here.
                if "." not in app_id and "\\" not in app_id:
                    continue
            is_executable_path = (
                (chr(92) in app_id or "/" in app_id)
                and app_id.casefold().endswith(".exe")
            )
            candidates.append(ApplicationCandidate(
                id=normalize(name),
                display_name=name,
                source="get_start_apps:executable" if is_executable_path else "get_start_apps",
                launch_method=LaunchMethod.EXE if is_executable_path else LaunchMethod.APP_USER_MODEL_ID,
                target=app_id,
                confidence=0.7,
            ))
        return candidates

    # -------------------------------------------------------------- index

    @staticmethod
    def _uninstall_candidates() -> list[ApplicationCandidate]:
        """Metadata de desinstalação (kazumi-full §2.3): DisplayName + DisplayIcon/InstallLocation."""
        import winreg

        hive_keys = (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        )
        candidates: list[ApplicationCandidate] = []
        seen_names: set[str] = set()
        for hive, key_path in hive_keys:
            try:
                with winreg.OpenKey(hive, key_path) as root:
                    index = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(root, index)
                        except OSError:
                            break
                        index += 1
                        if len(candidates) >= 400:
                            break
                        try:
                            with winreg.OpenKey(root, subkey_name) as sub:
                                def _value(name: str) -> str:
                                    try:
                                        value, _ = winreg.QueryValueEx(sub, name)
                                        return str(value).strip()
                                    except OSError:
                                        return ""

                                display = _value("DisplayName")
                                if not display or len(display) < 2:
                                    continue
                                norm_name = normalize(display)
                                if not norm_name or norm_name in seen_names:
                                    continue
                                system_component = _value("SystemComponent")
                                if system_component == "1":
                                    continue
                                icon = _value("DisplayIcon")
                                install_location = _value("InstallLocation")
                                target = ""
                                if icon:
                                    icon = icon.split(",")[0].strip().strip('"')
                                    if icon.casefold().endswith(".exe"):
                                        expanded = expand_launch_target(icon)
                                        if Path(expanded).is_file():
                                            target = expanded
                                if not target and install_location:
                                    base = expand_launch_target(install_location.rstrip("\\"))
                                    if base and Path(base).is_dir():
                                        for exe in sorted(Path(base).glob("*.exe"))[:6]:
                                            if exe.stem.casefold() in {"uninstall", "unins000", "setup", "update"}:
                                                continue
                                            target = str(exe)
                                            break
                                if not target:
                                    continue
                                seen_names.add(norm_name)
                                candidates.append(ApplicationCandidate(
                                    id=norm_name,
                                    display_name=display,
                                    source="uninstall_registry",
                                    launch_method=LaunchMethod.EXE,
                                    target=target,
                                    confidence=0.65,
                                ))
                        except OSError:
                            continue
            except OSError:
                continue
        return candidates

    @classmethod
    def _common_dirs_candidates(cls) -> list[ApplicationCandidate]:
        """Executáveis raiz em diretórios típicos (kazumi-full §2.6), com limites."""
        roots = []
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            roots.append(Path(local) / "Programs")
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        roots.append(Path(program_files))
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        if pf86 != program_files:
            roots.append(Path(pf86))
        candidates: list[ApplicationCandidate] = []
        skip_stems = {"uninstall", "unins000", "setup", "update", "crashpad_handler", "installer"}
        max_dirs_per_root = 220
        max_total = 500
        for root in roots:
            if len(candidates) >= max_total or not root.is_dir():
                continue
            scanned = 0
            try:
                children = sorted(root.iterdir())
            except OSError:
                continue
            for child in children:
                if scanned >= max_dirs_per_root or len(candidates) >= max_total:
                    break
                if not child.is_dir():
                    continue
                scanned += 1
                try:
                    exes = [exe for exe in child.glob("*.exe") if exe.stem.casefold() not in skip_stems]
                except OSError:
                    continue
                if len(exes) != 1:
                    # pastas com vários exes ficam cobertas por Atalhos do Menu Iniciar
                    continue
                exe = exes[0]
                candidates.append(ApplicationCandidate(
                    id=normalize(exe.stem),
                    display_name=child.name,
                    source="common_dirs",
                    launch_method=LaunchMethod.EXE,
                    target=str(exe),
                    confidence=0.55,
                ))
        return candidates

    def index(self, force: bool = False) -> list[ApplicationCandidate]:
        if not force and self.cache.valid():
            return self.cache.entries
        entries: dict[tuple[str, str, str], ApplicationCandidate] = {}
        for candidate in (
            self._known_candidates()
            + self._app_paths_candidates()
            + self._start_menu_candidates()
            + self._get_start_apps_candidates()
            + self._uninstall_candidates()
            + self._common_dirs_candidates()
            + self._path_candidates()
        ):
            key = (
                candidate.id,
                str(candidate.launch_method),
                expand_launch_target(candidate.target).casefold(),
            )
            existing = entries.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                entries[key] = candidate
        # Discovery sources describe launch routes, not distinct apps.
        from app.desktop.canonical_apps import canonicalize_candidates

        self.cache.entries = canonicalize_candidates(entries.values())
        self.cache.indexed_at = time.monotonic()
        return self.cache.entries

    def candidates_for(self, app_id: str) -> list[ApplicationCandidate]:
        """Return every discovered launch route for one logical application."""
        normalized = normalize(app_id)
        return [
            candidate for candidate in self.index()
            if normalize(candidate.id) == normalized
        ]

    # ------------------------------------------------------------- search

    def revalidate(self, candidate: ApplicationCandidate) -> bool:
        """Drop cached entries whose target disappeared from the host (#68)."""
        if candidate.launch_method == LaunchMethod.EXE:
            target = expand_launch_target(candidate.target)
            resolved = shutil.which(target) if ("\\" not in target and "/" not in target) else target
            return bool(resolved and Path(resolved).is_file())
        if candidate.launch_method == LaunchMethod.START_MENU:
            return Path(expand_launch_target(candidate.target)).is_file()
        if candidate.launch_method == LaunchMethod.URI:
            return True
        if candidate.launch_method == LaunchMethod.APP_USER_MODEL_ID:
            return True
        return False

    def search(self, query: str, limit: int = 8) -> list[ApplicationCandidate]:
        if not self.enabled or len(query.strip()) < 2:
            return []
        scored_by_identity: dict[str, ApplicationCandidate] = {}
        for candidate in self.index():
            names = (candidate.display_name, *candidate.aliases)
            confidence = max(score_match(query, name) for name in names if name)
            if normalize(candidate.id) == normalize(query):
                confidence = max(confidence, 1.0)
            if confidence <= 0:
                continue
            scored = ApplicationCandidate(
                id=candidate.id,
                display_name=candidate.display_name,
                source=candidate.source,
                launch_method=candidate.launch_method,
                target=candidate.target,
                confidence=min(confidence, 1.0),
                expected_window=candidate.expected_window,
                process_names=candidate.process_names,
                aliases=candidate.aliases,
            )
            previous = scored_by_identity.get(scored.id)
            if previous is None or scored.confidence > previous.confidence:
                scored_by_identity[scored.id] = scored
        scored = list(scored_by_identity.values())
        scored.sort(key=lambda item: (-item.confidence, item.display_name))
        return scored[:limit]

    def resolve(self, query: str) -> dict:
        """Resolve one free-text request into an actionable launch decision."""
        if not self.enabled:
            return {"status": "DISABLED", "candidates": [], "query": query}
        candidates = self.search(query, limit=8)
        decision = self._decide(query, candidates)
        if decision["status"] in {"EXACT_MATCH", "HIGH_CONFIDENCE"}:
            target_id = decision["candidate"]["id"]
            chosen = next(item for item in candidates if item.id == target_id)
            if not self.revalidate(chosen):
                # Cached target disappeared: invalidate once and retry (#68).
                self.cache.invalidate()
                candidates = self.search(query, limit=8)
                decision = self._decide(query, candidates)
        return decision

    def _decide(self, query: str, candidates: list[ApplicationCandidate]) -> dict:
        if not candidates:
            return {"status": "NOT_FOUND", "candidates": [], "query": query}
        exact = [item for item in candidates if item.confidence >= 1.0]
        high = [item for item in candidates if item.confidence >= 0.85]
        if len(exact) == 1:
            best = max(exact, key=lambda item: item.confidence)
            return {
                "status": "EXACT_MATCH",
                "candidate": best.public_dict(),
                "candidates": [item.public_dict() for item in candidates[:3]],
                "query": query,
            }
        if len(exact) > 1:
            # kazumi-full §31: mesmo executável por fontes diferentes NÃO é
            # ambiguidade real (ex.: "Microsoft Edge" lnk + App Paths msedge).
            def _final_key(item: ApplicationCandidate) -> tuple:
                try:
                    if item.launch_method == LaunchMethod.EXE:
                        expanded = Path(expand_launch_target(item.target))
                        return ("exe", expanded.name.casefold())
                    return (str(item.launch_method), item.target.casefold())
                except OSError:
                    return (str(item.launch_method), item.target.casefold())

            distinct = {_final_key(item) for item in exact}
            if len(distinct) == 1:
                best = max(exact, key=lambda item: (item.confidence, item.source == "shell_known"))
                return {
                    "status": "EXACT_MATCH",
                    "candidate": best.public_dict(),
                    "candidates": [item.public_dict() for item in candidates[:3]],
                    "query": query,
                }
            return {"status": "AMBIGUOUS", "candidates": [item.public_dict() for item in exact[:4]], "query": query}
        if len(high) == 1:
            return {
                "status": "HIGH_CONFIDENCE",
                "candidate": high[0].public_dict(),
                "candidates": [item.public_dict() for item in candidates[:3]],
                "query": query,
            }
        return {"status": "AMBIGUOUS", "candidates": [item.public_dict() for item in candidates[:4]], "query": query}


def decode_output_bytes(value: bytes) -> str:
    if not value:
        return ""
    for encoding in ("utf-8", "mbcs"):
        try:
            return value.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


decode_lines = decode_output_bytes
