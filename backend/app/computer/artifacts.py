"""Contexto recente e fast-path determinístico para artefatos.

Mantém somente metadados (nunca conteúdo) e roda antes do App Resolver.
Resolver um alvo não concede autorização: ações locais continuam no
DesktopController e leitura remota continua no RemoteShellService.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import shlex
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, Field


logger = logging.getLogger("nyra.computer.artifacts")

ArtifactScope = Literal["local", "remote", "url"]
ArtifactExistsState = Literal["planned", "created", "verified", "missing", "unknown"]
ArtifactAction = Literal[
    "OPEN_ARTIFACT", "READ_ARTIFACT", "SHOW_ARTIFACT", "TAIL_ARTIFACT",
    "REVEAL_ARTIFACT", "EDIT_ARTIFACT", "COPY_ARTIFACT",
    "DOWNLOAD_ARTIFACT", "PATH_ARTIFACT",
]

_TEXT_SUFFIXES = {
    ".log", ".txt", ".md", ".json", ".yaml", ".yml", ".xml", ".csv",
    ".py", ".ps1", ".ini", ".cfg", ".conf", ".toml", ".sh",
}
_LOG_SUFFIXES = {".log", ".out", ".err"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_AUDIO_SUFFIXES = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac"}
_REPORT_SUFFIXES = {".md", ".pdf", ".html", ".htm", ".csv"}

_TRAILING_FILLER = re.compile(
    r"(?:\s*[,;:]?\s*(?:agora|por\s+favor|pfv|por\s+gentileza|a[ií]|pra\s+mim|para\s+mim))"
    r"+[.!?\s]*$",
    re.IGNORECASE,
)
_QUOTED_PATH = re.compile(
    r"(?P<quote>[\"'])(?P<path>(?:[A-Za-z]:\\|\\\\|/|https?://)[^\"'\r\n]+)(?P=quote)",
    re.IGNORECASE,
)
_WINDOWS_FILE_PATH = re.compile(
    r"(?<![\w])(?P<path>[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*"
    r"[^\\/:*?\"<>|\r\n,;]+?\.[A-Za-z0-9]{1,10})(?=$|[\s,;.!?])"
)
_WINDOWS_FOLDER_PATH = re.compile(r"(?<![\w])(?P<path>[A-Za-z]:\\[^\r\n,;\"']+)")
_UNC_PATH = re.compile(r"(?<![\w])(?P<path>\\\\[^\\\s,;]+\\[^\r\n,;\"']+)")
_POSIX_PATH = re.compile(
    r"(?<![\w:])(?P<path>/(?:[A-Za-z0-9._~+@%=-]+/)*[A-Za-z0-9._~+@%=-]+/?)"
)
_URL = re.compile(r"(?P<path>https?://[^\s<>()\[\]{}\"']+)", re.IGNORECASE)

_REFERENCE_NOUNS = {
    "log": "log", "logs": "log", "arquivo": "file", "arquivos": "file",
    "relatório": "report", "relatorio": "report", "pasta": "folder",
    "diretório": "folder", "diretorio": "folder", "imagem": "image",
    "áudio": "audio", "audio": "audio", "documento": "document",
    "download": "download", "url": "url", "link": "url",
}
_CONTEXT_MARKERS = re.compile(
    r"\b(?:esse|essa|este|esta|isso|ele|ela|dele|dela|a[ií]|anterior|[uú]ltim[oa]|"
    r"que\s+voc[eê]\s+(?:gerou|criou|salvou|baixou|mostrou|mencionou)|"
    r"que\s+(?:foi\s+)?(?:gerado|criado|salvo|baixado|mostrado)|"
    r"acabou\s+de\s+(?:gerar|criar|salvar|baixar)|caminho\s+que\s+voc[eê]\s+mostrou)\b",
    re.IGNORECASE,
)
_TYPE_REFERENCE = re.compile(
    r"\b(?:logs?|arquivos?|relat[oó]rios?|pastas?|diret[oó]rios?|imagens?|"
    r"[aá]udios?|documentos?|downloads?|urls?|links?)\b",
    re.IGNORECASE,
)


class RecentArtifact(BaseModel):
    artifact_id: str = Field(default_factory=lambda: f"artifact_{uuid4().hex[:16]}")
    kind: str = "file"
    subtype: str = ""
    display_name: str
    path: str
    host_scope: ArtifactScope = "local"
    host_id: str | None = None
    conversation_id: str = "default"
    source_turn_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    last_referenced_at: float = Field(default_factory=time.time)
    mime_type: str | None = None
    exists_state: ArtifactExistsState = "unknown"
    can_read: bool = True
    can_open: bool = True
    can_download: bool = False
    preferred_action: str = "open"
    source_type: str = "mentioned"
    source_tool: str | None = None


class ArtifactRequest(BaseModel):
    action: ArtifactAction
    reference: str
    explicit_path: str | None = None
    wanted_kind: str | None = None
    parent_requested: bool = False
    line_count: int = 100


class ArtifactActionResult(BaseModel):
    handled: bool = True
    reply: str
    action: ArtifactAction
    artifact: RecentArtifact | None = None
    verified: bool | None = None
    app_resolver_called: bool = False
    remote_shell_calls: int = 0
    agent_run_calls: int = 0


def is_windows_path(value: str) -> bool:
    return bool(re.match(r"^(?:[A-Za-z]:\\|\\\\)", value or ""))


def is_posix_path(value: str) -> bool:
    return bool(value and value.startswith("/") and not value.startswith("//"))


def _strip_path_punctuation(value: str) -> str:
    clean = _TRAILING_FILLER.sub("", (value or "").strip())
    return clean.rstrip(".,;:!?").strip()


def extract_artifact_paths(text: str) -> list[str]:
    """Extrai paths literais sem transformar o restante da frase em alvo."""
    value = text or ""
    found: list[tuple[int, str]] = []
    occupied: list[tuple[int, int]] = []
    for match in _QUOTED_PATH.finditer(value):
        found.append((match.start("path"), _strip_path_punctuation(match.group("path"))))
        occupied.append(match.span())

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < end and span[1] > start for start, end in occupied)

    for pattern in (_URL, _WINDOWS_FILE_PATH, _UNC_PATH, _POSIX_PATH, _WINDOWS_FOLDER_PATH):
        for match in pattern.finditer(value):
            if overlaps(match.span()):
                continue
            path = _strip_path_punctuation(match.group("path"))
            path = re.sub(
                r"\s+(?:agora|por\s+favor|pfv|a[ií]|pra\s+mim|para\s+mim)$",
                "", path, flags=re.IGNORECASE,
            ).strip()
            if path:
                found.append((match.start("path"), path))
    result: list[str] = []
    seen: set[str] = set()
    for _, path in sorted(found, key=lambda item: item[0]):
        key = path.casefold() if is_windows_path(path) else path
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _scope_for_path(path: str) -> ArtifactScope:
    if urlsplit(path).scheme.casefold() in {"http", "https"}:
        return "url"
    if is_posix_path(path):
        return "remote"
    return "local"


def _display_name(path: str, scope: ArtifactScope) -> str:
    if scope == "url":
        parsed = urlsplit(path)
        return PurePosixPath(parsed.path).name or parsed.hostname or path
    if scope == "remote":
        return PurePosixPath(path).name or path
    if is_windows_path(path):
        return PureWindowsPath(path).name or path
    return Path(path).name or path


def _kind_for(path: str, hint: str | None = None) -> tuple[str, str]:
    if hint:
        normalized = _REFERENCE_NOUNS.get(hint.casefold(), hint.casefold())
        if normalized != "file":
            return normalized, normalized
    scope = _scope_for_path(path)
    if scope == "url":
        return "url", "url"
    suffix = (PurePosixPath(path).suffix if scope == "remote"
              else PureWindowsPath(path).suffix if is_windows_path(path)
              else Path(path).suffix).casefold()
    if suffix in _LOG_SUFFIXES or "log" in _display_name(path, scope).casefold():
        return "log", suffix.lstrip(".") or "log"
    if suffix in _IMAGE_SUFFIXES:
        return "image", suffix.lstrip(".")
    if suffix in _AUDIO_SUFFIXES:
        return "audio", suffix.lstrip(".")
    if suffix in _REPORT_SUFFIXES and "report" in _display_name(path, scope).casefold():
        return "report", suffix.lstrip(".")
    return "file", suffix.lstrip(".") or "file"


def _local_exists_state(path: str) -> ArtifactExistsState:
    try:
        return "verified" if Path(path).exists() else "missing"
    except OSError:
        return "unknown"


def parse_artifact_request(text: str) -> ArtifactRequest | None:
    """Reconhece somente ações cujo alvo é plausivelmente um artefato."""
    value = " ".join((text or "").strip().split())
    if not value or len(value) > 500:
        return None
    normalized = _TRAILING_FILLER.sub("", value).strip(" .!?")
    lowered = normalized.casefold()
    paths = extract_artifact_paths(normalized)

    action: ArtifactAction | None = None
    if re.search(r"\b(?:qual|mostra|mostre|diz|diga)\b.{0,30}\bcaminho\s+exato\b", lowered):
        action = "PATH_ARTIFACT"
    elif re.search(r"\b(?:[uú]ltimas?|finais?)\s+(?:\d+\s+)?linhas?\b|\btail\b", lowered):
        action = "TAIL_ARTIFACT"
    elif re.match(r"^(?:abre|abra|abrir|visualiza|visualize|visualizar)\b", lowered):
        action = "OPEN_ARTIFACT"
    elif re.match(r"^(?:mostra|mostre|mostrar|l[eê]|leia|ler|exibe|exiba|exibir|inspeciona|inspecione|inspecionar)\b", lowered):
        action = "SHOW_ARTIFACT" if re.match(r"^(?:mostra|mostre|mostrar|exibe|exiba|exibir)", lowered) else "READ_ARTIFACT"
    elif re.match(r"^(?:revela|revele|revelar)\b", lowered):
        action = "REVEAL_ARTIFACT"
    elif re.match(r"^(?:edita|edite|editar)\b", lowered):
        action = "EDIT_ARTIFACT"
    elif re.match(r"^(?:copia|copie|copiar)\b", lowered):
        action = "COPY_ARTIFACT"
    elif re.match(r"^(?:baixa|baixe|baixar|download)\b", lowered):
        action = "DOWNLOAD_ARTIFACT"
    if action is None:
        return None

    parent_requested = bool(re.search(
        r"\b(?:pasta|diret[oó]rio)\s+(?:d[eo]l[ea]|desse|dessa)\b|\bno\s+explorador\b",
        lowered,
    ))
    if parent_requested and action in {"SHOW_ARTIFACT", "OPEN_ARTIFACT", "REVEAL_ARTIFACT"}:
        action = "REVEAL_ARTIFACT"

    wanted_kind = None
    for token, kind in _REFERENCE_NOUNS.items():
        if re.search(rf"\b{re.escape(token)}s?\b", lowered):
            wanted_kind = kind
            break
    explicit_path = paths[0] if paths else None
    contextual = bool(_CONTEXT_MARKERS.search(lowered))
    typed_reference = bool(_TYPE_REFERENCE.search(lowered))
    if action == "OPEN_ARTIFACT" and explicit_path is None and not contextual:
        from app.desktop.discovery import normalize
        from app.desktop.intents import KNOWN_FOLDER_KEYS

        bare_target = re.sub(
            r"^(?:abre|abra|abrir)\s+(?:o\s+|a\s+)?",
            "", lowered,
        ).strip()
        if normalize(bare_target) in KNOWN_FOLDER_KEYS:
            return None
    if explicit_path is None and action != "PATH_ARTIFACT" and not (contextual or typed_reference):
        return None
    return ArtifactRequest(
        action=action, reference=normalized, explicit_path=explicit_path,
        wanted_kind=wanted_kind, parent_requested=parent_requested,
    )


class RecentArtifactMemory:
    """Pilha curta por conversa, persistindo apenas metadados locais."""

    def __init__(self, *, max_items: int = 50, persistence_path: Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.max_items = max(20, min(int(max_items), 50))
        self.persistence_path = persistence_path
        self.clock = clock
        self._items: list[RecentArtifact] = []
        self._turn_conversations: dict[str, str] = {}
        self._remote_hosts_by_conversation: dict[str, str] = {}
        self.load()

    def note_turn(self, conversation_id: str, turn_id: str | None) -> None:
        if turn_id:
            self._turn_conversations[turn_id] = conversation_id or "default"
            if len(self._turn_conversations) > 256:
                self._turn_conversations.pop(next(iter(self._turn_conversations)), None)

    def conversation_for_turn(self, turn_id: str | None) -> str:
        return self._turn_conversations.get(turn_id or "", "default")

    def note_remote_host(self, host_id: str, turn_id: str | None) -> None:
        if not host_id:
            return
        conversation = self.conversation_for_turn(turn_id)
        self._remote_hosts_by_conversation[conversation] = host_id
        if len(self._remote_hosts_by_conversation) > 256:
            self._remote_hosts_by_conversation.pop(
                next(iter(self._remote_hosts_by_conversation)), None,
            )

    @property
    def items(self) -> list[RecentArtifact]:
        return list(self._items)

    def register(
        self, path: str, *, kind: str | None = None, subtype: str | None = None,
        host_scope: ArtifactScope | None = None, host_id: str | None = None,
        conversation_id: str = "default", source_turn_id: str | None = None,
        exists_state: ArtifactExistsState = "unknown", source_type: str = "mentioned",
        source_tool: str | None = None, preferred_action: str | None = None,
    ) -> RecentArtifact:
        clean = _strip_path_punctuation(path)
        scope = host_scope or _scope_for_path(clean)
        detected_kind, detected_subtype = _kind_for(clean, kind)
        now = self.clock()
        conversation = conversation_id or "default"
        existing = next((
            item for item in reversed(self._items)
            if item.conversation_id == conversation and item.host_scope == scope
            and (item.host_id or "") == (host_id or "")
            and ((item.path.casefold() == clean.casefold()) if scope == "local" else item.path == clean)
        ), None)
        if existing is not None:
            existing.kind = kind or existing.kind or detected_kind
            existing.subtype = subtype or existing.subtype or detected_subtype
            existing.display_name = _display_name(clean, scope)
            existing.last_referenced_at = now
            existing.source_turn_id = source_turn_id or existing.source_turn_id
            existing.host_id = host_id or existing.host_id
            existing.source_tool = source_tool or existing.source_tool
            existing.source_type = source_type or existing.source_type
            existing.preferred_action = preferred_action or existing.preferred_action
            if exists_state != "unknown":
                existing.exists_state = exists_state
            self._items.remove(existing)
            self._items.append(existing)
            return existing
        mime_type = mimetypes.guess_type(clean)[0]
        item = RecentArtifact(
            kind=kind or detected_kind, subtype=subtype or detected_subtype,
            display_name=_display_name(clean, scope), path=clean, host_scope=scope,
            host_id=host_id, conversation_id=conversation,
            source_turn_id=source_turn_id, created_at=now, last_referenced_at=now,
            mime_type=mime_type, exists_state=exists_state,
            can_read=(scope == "remote" or detected_kind in {
                "log", "file", "report", "document", "download",
            }),
            can_open=True, can_download=scope == "remote",
            preferred_action=preferred_action or ("read" if detected_kind == "log" else "open"),
            source_type=source_type, source_tool=source_tool,
        )
        self._items.append(item)
        del self._items[:-self.max_items]
        return item

    def mark(self, artifact: RecentArtifact, state: ArtifactExistsState) -> None:
        artifact.exists_state = state
        artifact.last_referenced_at = self.clock()
        if artifact in self._items:
            self._items.remove(artifact)
            self._items.append(artifact)

    def resolve(self, request: ArtifactRequest, *, conversation_id: str,
                turn_id: str | None = None) -> RecentArtifact | None:
        conversation = conversation_id or "default"
        if request.explicit_path:
            scope = _scope_for_path(request.explicit_path)
            state: ArtifactExistsState = (
                _local_exists_state(request.explicit_path) if scope == "local" else "unknown"
            )
            return self.register(
                request.explicit_path, kind=request.wanted_kind, host_scope=scope,
                host_id=self.latest_remote_host(conversation) if scope == "remote" else None,
                conversation_id=conversation, source_turn_id=turn_id,
                exists_state=state, source_type="explicit_user_path",
            )
        candidates = [item for item in self._items if item.conversation_id == conversation]
        if not candidates:
            return None
        reference = request.reference.casefold()
        generated_reference = bool(re.search(
            r"(?:gerou|criou|salvou|baixou|acabou\s+de\s+(?:gerar|criar|salvar|baixar))",
            reference,
        ))
        scored: list[tuple[float, int, RecentArtifact]] = []
        for index, item in enumerate(candidates):
            score = float(index) * 2.0
            if request.wanted_kind:
                wanted = _REFERENCE_NOUNS.get(request.wanted_kind, request.wanted_kind)
                score += 50.0 if item.kind == wanted or (
                    wanted == "file" and item.kind != "folder"
                ) else -40.0
            if item.display_name.casefold() in reference or item.path.casefold() in reference:
                score += 80.0
            if generated_reference and item.source_type in {"tool_result", "operator_created"}:
                score += 35.0
            if item.exists_state in {"created", "verified"}:
                score += 12.0
            if item.source_turn_id and turn_id and item.source_turn_id == turn_id:
                score += 8.0
            if request.action in {"READ_ARTIFACT", "SHOW_ARTIFACT", "TAIL_ARTIFACT"} and item.can_read:
                score += 15.0
            if request.action in {"OPEN_ARTIFACT", "REVEAL_ARTIFACT", "EDIT_ARTIFACT"} and item.can_open:
                score += 10.0
            scored.append((score, index, item))
        scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        selected = (
            scored[1][2]
            if "anterior" in reference and len(scored) > 1
            else scored[0][2]
        )
        selected.last_referenced_at = self.clock()
        if selected in self._items:
            self._items.remove(selected)
            self._items.append(selected)
        return selected

    def latest_remote_host(self, conversation_id: str) -> str | None:
        artifact_host = next((
            item.host_id for item in reversed(self._items)
            if item.conversation_id == (conversation_id or "default")
            and item.host_scope == "remote" and item.host_id
        ), None)
        return artifact_host or self._remote_hosts_by_conversation.get(
            conversation_id or "default",
        )

    def persist(self) -> bool:
        if self.persistence_path is None:
            return True
        try:
            self.persistence_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1, "saved_at": self.clock(),
                "artifacts": [
                    item.model_dump(mode="json")
                    for item in self._items[-self.max_items:]
                ],
            }
            temporary = self.persistence_path.with_suffix(
                self.persistence_path.suffix + ".tmp",
            )
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8",
            )
            temporary.replace(self.persistence_path)
            return True
        except (OSError, ValueError):
            logger.warning("recent_artifact_context_save_failed", exc_info=False)
            return False

    def load(self) -> bool:
        if self.persistence_path is None or not self.persistence_path.is_file():
            return False
        try:
            payload = json.loads(
                self.persistence_path.read_text(encoding="utf-8"),
            )
            self._items = [
                RecentArtifact.model_validate(item)
                for item in (payload.get("artifacts") or [])[-self.max_items:]
            ]
            return True
        except (OSError, ValueError, TypeError):
            self._items = []
            return False


class ArtifactContextService:
    """ReferenceResolver + ArtifactResolver + ArtifactActionRouter."""

    def __init__(self, *, memory: RecentArtifactMemory | None = None, desktop=None,
                 remote_shell=None, state=None) -> None:
        self.memory = memory or RecentArtifactMemory()
        self.desktop = desktop
        self.remote_shell = remote_shell
        self.state = state
        self.metrics: dict[str, int] = {
            "artifact_reference_resolved": 0,
            "app_resolver_called_for_artifact": 0,
            "remote_shell_calls": 0,
            "agent_run_calls": 0,
        }

    def note_turn(self, conversation_id: str, turn_id: str | None) -> None:
        self.memory.note_turn(conversation_id, turn_id)

    def register(self, path: str, **metadata: Any) -> RecentArtifact:
        return self.memory.register(path, **metadata)

    async def try_handle(self, text: str, *, conversation_id: str = "default",
                         turn_id: str | None = None) -> ArtifactActionResult | None:
        self.note_turn(conversation_id, turn_id)
        request = parse_artifact_request(text)
        if request is None:
            return None
        artifact = self.memory.resolve(
            request, conversation_id=conversation_id, turn_id=turn_id,
        )
        if artifact is None:
            artifact = self._state_fallback(request, conversation_id, turn_id)
        if artifact is None:
            # Uma referência tipada inequívoca nunca vira nome de aplicativo.
            if request.wanted_kind or _TYPE_REFERENCE.search(request.reference):
                return ArtifactActionResult(
                    reply="Não tenho um artefato recente correspondente a essa referência.",
                    action=request.action,
                )
            return None

        host_from_text = self._remote_host_from_text(text)
        if artifact.host_scope == "remote" and host_from_text:
            artifact.host_id = host_from_text
        if request.parent_requested:
            artifact = self._parent_artifact(artifact)
        self.metrics["artifact_reference_resolved"] += 1
        logger.info(
            "artifact_reference_resolved",
            extra={
                "artifact_reference_resolved": True,
                "artifact_type": artifact.kind,
                "artifact_action": request.action,
                "host_scope": artifact.host_scope,
                "host_id": artifact.host_id,
                "app_resolver_called": False,
                "source_turn_id": artifact.source_turn_id,
                "turn_id": turn_id,
            },
        )
        if request.action == "PATH_ARTIFACT":
            location = (
                f"{artifact.host_id}:{artifact.path}"
                if artifact.host_scope == "remote" and artifact.host_id
                else artifact.path
            )
            return ArtifactActionResult(
                reply=location, action=request.action,
                artifact=artifact, verified=True,
            )
        return await self._route(request, artifact, turn_id=turn_id)

    def _state_fallback(self, request: ArtifactRequest, conversation_id: str,
                        turn_id: str | None) -> RecentArtifact | None:
        if self.state is None or not hasattr(self.state, "resolve_reference"):
            return None
        token = (
            "esse arquivo"
            if request.wanted_kind in {None, "file", "log", "report"}
            else request.reference
        )
        resolved = self.state.resolve_reference(
            token, conversation_id=conversation_id, turn_id=turn_id,
        )
        if (
            resolved is None
            or resolved.kind not in {"file", "folder"}
            or not resolved.path
        ):
            return None
        return self.memory.register(
            resolved.path, kind=request.wanted_kind or resolved.kind,
            conversation_id=conversation_id, source_turn_id=turn_id,
            exists_state=_local_exists_state(resolved.path),
            source_type="computer_state",
        )

    def _remote_host_from_text(self, text: str) -> str | None:
        hosts = getattr(self.remote_shell, "hosts", None)
        if hosts is None or not hasattr(hosts, "find_remote_in_text"):
            return None
        try:
            match = hosts.find_remote_in_text(text)
            return str(match.id) if match is not None else None
        except (AttributeError, ValueError):
            return None

    @staticmethod
    def _parent_artifact(source: RecentArtifact) -> RecentArtifact:
        if source.host_scope == "remote":
            parent = str(PurePosixPath(source.path).parent)
        elif is_windows_path(source.path):
            parent = str(PureWindowsPath(source.path).parent)
        else:
            parent = str(Path(source.path).parent)
        return RecentArtifact(
            artifact_id=f"{source.artifact_id}_parent",
            kind="folder", subtype="directory",
            display_name=_display_name(parent, source.host_scope),
            path=parent, host_scope=source.host_scope, host_id=source.host_id,
            conversation_id=source.conversation_id,
            source_turn_id=source.source_turn_id,
            created_at=source.created_at,
            last_referenced_at=source.last_referenced_at,
            exists_state=source.exists_state,
            can_read=False, can_open=True, can_download=False,
            preferred_action="reveal", source_type="derived_parent",
        )

    async def _route(self, request: ArtifactRequest, artifact: RecentArtifact,
                     *, turn_id: str | None) -> ArtifactActionResult:
        if artifact.host_scope == "remote":
            return await self._route_remote(request, artifact)
        if artifact.host_scope == "url":
            return await self._route_url(request, artifact)
        return await self._route_local(request, artifact, turn_id=turn_id)

    async def _route_local(self, request: ArtifactRequest,
                           artifact: RecentArtifact, *,
                           turn_id: str | None) -> ArtifactActionResult:
        path = Path(artifact.path)
        try:
            exists = path.exists()
            is_dir = path.is_dir()
            is_file = path.is_file()
        except OSError:
            exists = is_dir = is_file = False
        if not exists:
            self.memory.mark(artifact, "missing")
            return ArtifactActionResult(
                reply=(
                    f"Resolvi {artifact.display_name} como {artifact.path}, "
                    "mas o artefato não existe mais."
                ),
                action=request.action, artifact=artifact, verified=False,
            )
        self.memory.mark(artifact, "verified")
        if is_dir:
            artifact.kind = "folder"
        if request.action in {
            "READ_ARTIFACT", "SHOW_ARTIFACT", "TAIL_ARTIFACT",
        }:
            if not is_file:
                return ArtifactActionResult(
                    reply=(
                        f"Resolvi {artifact.path}, mas ele é uma pasta; "
                        "peça para abri-la."
                    ),
                    action=request.action, artifact=artifact, verified=False,
                )
            if path.suffix.casefold() not in _TEXT_SUFFIXES:
                return ArtifactActionResult(
                    reply=(
                        f"Resolvi {artifact.path}, mas esse formato não "
                        "permite leitura textual direta."
                    ),
                    action=request.action, artifact=artifact, verified=False,
                )
            try:
                lines = path.read_text(
                    encoding="utf-8", errors="replace",
                ).splitlines()
            except OSError:
                return ArtifactActionResult(
                    reply=(
                        f"Resolvi {artifact.path}, mas não consegui ler o arquivo."
                    ),
                    action=request.action, artifact=artifact, verified=False,
                )
            if request.action == "TAIL_ARTIFACT" or artifact.kind == "log":
                shown = lines[-request.line_count:]
                description = (
                    f"últimas {min(request.line_count, len(lines))} linhas"
                )
            else:
                shown = lines[:200]
                description = f"primeiras {min(200, len(lines))} linhas"
            content = "\n".join(shown)
            reply = f"Li {artifact.path} e estou mostrando as {description}."
            if content:
                reply += f"\n\n{content}"
            return ArtifactActionResult(
                reply=reply, action=request.action,
                artifact=artifact, verified=True,
            )
        if request.action in {
            "OPEN_ARTIFACT", "REVEAL_ARTIFACT", "EDIT_ARTIFACT",
        }:
            if self.desktop is None:
                return ArtifactActionResult(
                    reply=(
                        f"Resolvi {artifact.path}, mas o controle do desktop "
                        "está indisponível."
                    ),
                    action=request.action, artifact=artifact, verified=False,
                )
            from app.desktop.intents import UniversalAction, UniversalIntent

            action = (
                UniversalAction.OPEN_FOLDER
                if is_dir else UniversalAction.OPEN_FILE
            )
            handled, reply = await self.desktop.handle_universal(
                UniversalIntent(action=action, target=str(path)),
                turn_id=turn_id,
            )
            operation = getattr(
                self.desktop, "last_operation_result", None,
            ) or {}
            verified = operation.get("effect_verified")
            if verified is None and operation.get("execution_success") is not True:
                verified = bool(operation.get("success")) if operation else None
            return ArtifactActionResult(
                handled=bool(handled), reply=reply, action=request.action,
                artifact=artifact, verified=verified,
            )
        return ArtifactActionResult(
            reply=(
                f"Resolvi o artefato como {artifact.path}, mas essa ação "
                "precisa do fluxo seguro de arquivos existente."
            ),
            action=request.action, artifact=artifact, verified=False,
        )

    async def _route_remote(self, request: ArtifactRequest,
                            artifact: RecentArtifact) -> ArtifactActionResult:
        if request.action not in {
            "OPEN_ARTIFACT", "READ_ARTIFACT", "SHOW_ARTIFACT", "TAIL_ARTIFACT",
        }:
            return ArtifactActionResult(
                reply=(
                    f"Resolvi o artefato remoto como {artifact.path}, mas "
                    "essa ação exige o fluxo remoto seguro correspondente."
                ),
                action=request.action, artifact=artifact, verified=False,
            )
        host = (
            artifact.host_id
            or self.memory.latest_remote_host(artifact.conversation_id)
        )
        if not host:
            candidates = self._remote_host_candidates()
            if len(candidates) == 1:
                host = candidates[0]
                artifact.host_id = host
        if not host:
            return ArtifactActionResult(
                reply=(
                    f"Resolvi o path remoto {artifact.path}, mas não "
                    "consegui determinar o host lógico."
                ),
                action=request.action, artifact=artifact, verified=False,
            )
        if self.remote_shell is None:
            return ArtifactActionResult(
                reply=(
                    f"Resolvi {host}:{artifact.path}, mas o acesso remoto "
                    "está indisponível."
                ),
                action=request.action, artifact=artifact, verified=False,
            )
        line_count = (
            request.line_count
            if request.action == "TAIL_ARTIFACT"
            else 100
        )
        command = f"tail -n {line_count} -- {shlex.quote(artifact.path)}"
        self.metrics["remote_shell_calls"] += 1
        result = await self.remote_shell.execute(
            host=host, command=command, timeout_seconds=15,
            reason="Leitura contextual read-only de artefato recente",
        )
        if not result.get("success"):
            stderr = str(result.get("stderr") or "")
            if re.search(
                r"no such file|not found|cannot open",
                stderr,
                re.IGNORECASE,
            ):
                self.memory.mark(artifact, "missing")
                reply = (
                    f"Resolvi o log como {host}:{artifact.path}, mas o "
                    "arquivo não existe mais."
                )
            else:
                reply = (
                    f"Resolvi {host}:{artifact.path}, mas não consegui ler "
                    f"o arquivo remoto: {result.get('message') or result.get('error_code') or 'falha remota'}."
                )
            return ArtifactActionResult(
                reply=reply, action=request.action, artifact=artifact,
                verified=False, remote_shell_calls=1,
            )
        self.memory.mark(artifact, "verified")
        content = str(result.get("stdout") or "")
        label = "log" if artifact.kind == "log" else "arquivo"
        reply = (
            f"Li o {label} remoto e estou mostrando as últimas {line_count} "
            f"linhas de {host}:{artifact.path}."
        )
        if content:
            reply += f"\n\n{content.rstrip()}"
        return ArtifactActionResult(
            reply=reply, action=request.action, artifact=artifact,
            verified=True, remote_shell_calls=1,
        )

    def _remote_host_candidates(self) -> list[str]:
        hosts = getattr(self.remote_shell, "hosts", None)
        if hosts is None or not hasattr(hosts, "public_remote_hosts"):
            return []
        try:
            public = hosts.public_remote_hosts()
        except (AttributeError, ValueError):
            return []
        values = public.values() if isinstance(public, dict) else public
        result: list[str] = []
        for item in values or []:
            if isinstance(item, dict):
                host_id = item.get("id") or item.get("host_id")
                enabled = item.get("enabled", True)
            else:
                host_id = getattr(item, "id", None)
                enabled = getattr(item, "enabled", True)
            if host_id and enabled:
                result.append(str(host_id))
        return result

    async def _route_url(self, request: ArtifactRequest,
                         artifact: RecentArtifact) -> ArtifactActionResult:
        if request.action != "OPEN_ARTIFACT" or self.desktop is None:
            return ArtifactActionResult(
                reply=(
                    f"Resolvi a URL como {artifact.path}, mas essa ação "
                    "não foi executada."
                ),
                action=request.action, artifact=artifact, verified=False,
            )
        result = await self.desktop.open_url(artifact.path)
        return ArtifactActionResult(
            reply=(
                "Abri a URL."
                if result.get("success")
                else f"Não consegui abrir a URL: {result.get('message') or 'falha real'}."
            ),
            action=request.action, artifact=artifact,
            verified=result.get("effect_verified"),
        )

    def observe_tool_result(self, tool_name: str, payload: dict[str, Any],
                            result, turn_id: str | None = None) -> None:
        """Registra paths grounded retornados por qualquer tool estruturada."""
        data = (
            result.data
            if hasattr(result, "data")
            else result.get("data", result)
            if isinstance(result, dict)
            else {}
        )
        ok = bool(getattr(result, "ok", data.get("success", False)))
        if not isinstance(data, dict) or not ok:
            return
        conversation = self.memory.conversation_for_turn(turn_id)
        remote = tool_name == "remote_shell"
        host_id = str(
            data.get("host") or payload.get("host") or "",
        ) or None
        if remote and host_id:
            self.memory.note_remote_host(host_id, turn_id)
        generated_tool = bool(re.search(
            r"(?:generate|create|export|download|screenshot|backup|report|log|save)",
            tool_name,
            re.IGNORECASE,
        ))
        path_keys = {
            "path", "file", "file_path", "filepath", "output_path",
            "log_path", "artifact_path", "download_path", "saved_to",
            "destination", "directory", "folder",
        }
        candidates: list[tuple[str, bool]] = []

        def walk(value: Any, key: str = "", depth: int = 0) -> None:
            if depth > 5:
                return
            if isinstance(value, dict):
                for child_key, child in value.items():
                    walk(child, str(child_key).casefold(), depth + 1)
            elif isinstance(value, (list, tuple)):
                for child in value[:100]:
                    walk(child, key, depth + 1)
            elif isinstance(value, str):
                if (
                    key in path_keys
                    and (
                        _scope_for_path(value) != "local"
                        or is_windows_path(value)
                        or Path(value).is_absolute()
                    )
                ):
                    candidates.append((value, True))
                elif key in {"stdout", "message", "result"}:
                    candidates.extend(
                        (path, False)
                        for path in extract_artifact_paths(value)
                    )

        walk(data)
        seen: set[tuple[str, str | None]] = set()
        for path, structured in candidates:
            scope: ArtifactScope = (
                "remote"
                if remote and is_posix_path(path)
                else _scope_for_path(path)
            )
            key = (
                path.casefold() if scope == "local" else path,
                host_id,
            )
            if key in seen:
                continue
            seen.add(key)
            if scope == "local":
                state: ArtifactExistsState = _local_exists_state(path)
            elif structured and generated_tool:
                state = "created"
            else:
                state = "unknown"
            self.memory.register(
                path, host_scope=scope,
                host_id=host_id if scope == "remote" else None,
                conversation_id=conversation, source_turn_id=turn_id,
                exists_state=state, source_type="tool_result",
                source_tool=tool_name,
            )

    def observe_assistant_response(
        self, response: str, *, conversation_id: str,
        turn_id: str | None, grounded: bool = False,
    ) -> None:
        """Mantém menções úteis sem promover texto livre a criação verificada."""
        for path in extract_artifact_paths(response):
            scope = _scope_for_path(path)
            existing = next((
                item for item in reversed(self.memory.items)
                if item.conversation_id == (conversation_id or "default")
                and item.path == path
            ), None)
            if existing is not None:
                existing.last_referenced_at = self.memory.clock()
                continue
            if scope == "local":
                state: ArtifactExistsState = _local_exists_state(path)
            else:
                # A frase do modelo sozinha não prova criação remota.
                state = "unknown" if grounded else "planned"
            self.memory.register(
                path, host_scope=scope,
                host_id=(
                    self.memory.latest_remote_host(conversation_id)
                    if scope == "remote"
                    else None
                ),
                conversation_id=conversation_id,
                source_turn_id=turn_id, exists_state=state,
                source_type="assistant_mention",
            )

    def persist(self) -> bool:
        return self.memory.persist()
