"""Universal Intent Parser (nyra-full V3 §3/§4/§6/§7/§18).

Converte linguagem natural PT-BR em uma intenção operacional local SEM LLM.
Texto e voz convergem no mesmo NormalizedUserIntent porque ambos entram pelo
mesmo /api/chat → orchestrator → parse.

Escopo do fast path determinístico:
OPEN_APP/CLOSE_APP/MINIMIZE/MAXIMIZE/RESTORE/FOCUS/SWITCH,
OPEN_FOLDER (pastas conhecidas dinâmicas) e OPEN_FILE (arquivo resolvido).
Browser, shell, filesystem complexo e multi-step caem para o Agent Loop
(que já possui grounding e verificação).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class UniversalAction(StrEnum):
    OPEN_APP = "OPEN_APP"
    CLOSE_APP = "CLOSE_APP"
    MINIMIZE_APP = "MINIMIZE_APP"
    MAXIMIZE_APP = "MAXIMIZE_APP"
    RESTORE_APP = "RESTORE_APP"
    FOCUS_APP = "FOCUS_APP"
    SWITCH_APP = "SWITCH_APP"
    OPEN_FOLDER = "OPEN_FOLDER"
    OPEN_FILE = "OPEN_FILE"


_CONTEXTUAL_TARGETS = {"ele", "ela", "isso", "esse", "essa", "aquele", "aquela", "mesmo"}
_REOPEN_TARGETS = {"de novo", "novamente", "outra vez"}
_DEMONSTRATIVE_FILES = {"esse arquivo", "este arquivo", "esse", "este"}

_OPEN_VERBS = r"(?:abre|abra|abrir|inicia|inicie|iniciar|executa|execute|rod[ae]r?|sobe|suba)"
_CLOSE_VERBS = r"(?:fecha|feche|fechar|encer[ar]a?|encerre|mat[aao]|mata)"
_MIN_VERBS = r"(?:minimiz[ae]r?|minimiza)"
_MAX_VERBS = r"(?:maximiz[ae]r?|maximiza)"
_RESTORE_VERBS = r"(?:restaur[ae]r?|restaura|desminimiz[ae]r?)"
_FOCUS_VERBS = (
    r"(?:traz(?:\s+(?:o|a|pra|para))?(?:\s+pra\s+frente|\s+para\s+frente)?"
    r"|foc[ao]r?|va[i]?\s+(?:pra|para|pro)|volta(?:\s+(?:pra|para|pro))?|abre\s+na\s+frente)"
)
_SWITCH_VERBS = r"(?:altern[ae]r?|troca(?:r|e)?|mud[ae]r?)"

_ARTICLE = r"^(?:o|a|os|as|um|uma|meu|minha)\s+"

# Pastas conhecidas (chaves já normalizadas sem espaços/acentos — nyra-full §6).
# Valores: (display_name, shell URI). Resolução DINÂMICA: URIs shell nunca
# contêm username; caminhos por ambiente ficam no executor (control.py).
FOLDER_SHELL_URIS: dict[str, tuple[str, str]] = {
    "downloads": ("Downloads", "shell:Downloads"),
    "download": ("Downloads", "shell:Downloads"),
    "documentos": ("Documentos", "shell:Personal"),
    "meusdocumentos": ("Documentos", "shell:Personal"),
    "documentosrecentes": ("Recentes", "shell:Recent"),
    "recentes": ("Recentes", "shell:Recent"),
    "imagens": ("Imagens", "shell:My Pictures"),
    "minhasimagens": ("Imagens", "shell:My Pictures"),
    "fotos": ("Imagens", "shell:My Pictures"),
    "musicas": ("Músicas", "shell:My Music"),
    "minhasmusicas": ("Músicas", "shell:My Music"),
    "videos": ("Vídeos", "shell:My Video"),
    "meusvideos": ("Vídeos", "shell:My Video"),
    "desktop": ("Área de Trabalho", "shell:Desktop"),
    "areadetrabalho": ("Área de Trabalho", "shell:Desktop"),
}
# Pastas resolvidas por variável de ambiente (nunca hardcode de usuário).
FOLDER_ENV_KEYS = {"home", "appdata", "appdatelocal", "temp", "temporarios", "onedrive"}
KNOWN_FOLDER_KEYS = set(FOLDER_SHELL_URIS) | FOLDER_ENV_KEYS

# Alvos que NÃO são apps locais (browser/web/pesquisa ficam com o Agent Loop).
_NON_APP_HINTS = re.compile(
    r"(https?://|\.com\b|\.br\b|\.dev\b|\.org\b|pesquis|busqu|google|youtube|github|site|p[aá]gina|"
    r"painel do|dashboard|home assistant|proxmox|openwrt|sentinel)",
    re.IGNORECASE,
)

_COMMAND_SUFFIX = re.compile(
    r"[.!?\s]*(?:por\s+favor|pf[vv]?)?[.!?\s]*$",
    re.IGNORECASE,
)

_FOLDER_PHRASE = re.compile(
    r"^(?:pastas?|diret[oó]rios?|folders?)\s+(?:d[aeo]s?\s+|d[oa]s?\s+|de\s+)?(?P<name>.+)$"
)
_FILE_PHRASE = re.compile(r"^arquivos?\s+(?P<name>.+)$")
_FILE_EXTENSION = re.compile(r"(?:^|\s)[\w.\-()]+\.[a-z][a-z0-9]{0,4}$")


@dataclass(frozen=True)
class UniversalIntent:
    action: UniversalAction
    target: str
    contextual: bool = False
    explicit_new: bool = False

    @property
    def dedup_key(self) -> tuple[str, str]:
        from app.desktop.discovery import normalize

        return (self.action.value, normalize(self.target))


def _clean_target(value: str) -> str:
    target = _COMMAND_SUFFIX.sub("", value.strip(), count=1)
    target = re.sub(_ARTICLE, "", target, count=1)
    target = target.strip(" \"'")
    # remove prefixos conversacionais comuns
    target = re.sub(r"^(?:pra\s+mim\s+)?", "", target, count=1)
    return target.strip()


def parse_universal_intent(text: str) -> UniversalIntent | None:
    """Extrai uma intenção universal de aplicativo/janela/pasta/arquivo, ou None."""
    value = " ".join((text or "").strip().split())
    if not value or len(value) > 120:
        return None
    value = re.sub(r"^(?:nyra|ei nyra|oi nyra)\s*[,!:.]?\s*", "", value, count=1, flags=re.IGNORECASE)
    lowered = value.casefold()

    if _NON_APP_HINTS.search(lowered):
        return None

    match = re.match(
        rf"^(?:{_OPEN_VERBS})\s+(.+)$", lowered,
    )
    if match:
        target = _clean_target(match.group(1))
        if not target or len(target.split()) > 6:
            return None

        # 1) Reabertura contextual: "abre de novo" (nyra-full §18).
        if target in _REOPEN_TARGETS:
            return UniversalIntent(UniversalAction.OPEN_APP, target, True)

        # 2) Frase explícita de pasta: "abre a pasta Downloads" (§6).
        folder_match = _FOLDER_PHRASE.match(target)
        if folder_match:
            name = folder_match.group("name").strip()
            if name:
                return UniversalIntent(UniversalAction.OPEN_FOLDER, name)

        # 3) Pasta conhecida sem a palavra "pasta": "abre Documentos" (§6).
        from app.desktop.discovery import normalize

        if len(target.split()) <= 3 and normalize(target) in KNOWN_FOLDER_KEYS:
            return UniversalIntent(UniversalAction.OPEN_FOLDER, target)

        # 4) Arquivo demonstrativo: "abre esse arquivo" (§7).
        if target in _DEMONSTRATIVE_FILES:
            return UniversalIntent(UniversalAction.OPEN_FILE, target, True)

        # 5) Frase explícita de arquivo: "abre o arquivo X" (§7).
        file_match = _FILE_PHRASE.match(target)
        if file_match:
            name = file_match.group("name").strip()
            if name:
                return UniversalIntent(UniversalAction.OPEN_FILE, name)

        # 6) Nome com extensão plausível: "abre relatorio.pdf".
        if len(target.split()) <= 4 and _FILE_EXTENSION.search(target):
            return UniversalIntent(UniversalAction.OPEN_FILE, target)

        contextual = target in _CONTEXTUAL_TARGETS
        # §17: "abre outro X / nova janela de X" autoriza segunda instância.
        explicit_new = False
        quantifier = re.match(
            r"^(?:outr[oa]s?|mais\s+um[as]?|nov[oa]s?)\s+(?:o\s+|a\s+)?(?P<base>.+)$", target
        )
        if quantifier and quantifier.group("base").strip():
            target = quantifier.group("base").strip()
            explicit_new = True
        elif re.fullmatch(
            r"(?:(?:nova|outra)\s+janela|novo|outra|nova)", target
        ):
            explicit_new = True
        if contextual or " " not in target or len(target) >= 2:
            return UniversalIntent(UniversalAction.OPEN_APP, target, contextual, explicit_new)
        return None

    for action, verbs in (
        (UniversalAction.CLOSE_APP, _CLOSE_VERBS),
        (UniversalAction.MINIMIZE_APP, _MIN_VERBS),
        (UniversalAction.MAXIMIZE_APP, _MAX_VERBS),
        (UniversalAction.RESTORE_APP, _RESTORE_VERBS),
    ):
        match = re.match(rf"^(?:{verbs})\s+(.+)$", lowered)
        if match:
            target = _clean_target(match.group(1))
            if target and len(target.split()) <= 6:
                return UniversalIntent(action, target, target in _CONTEXTUAL_TARGETS)

    # alterna para o Code / troca pro navegador / muda para a calculadora
    match = re.match(rf"^(?:{_SWITCH_VERBS})\s+(?:pa?r?a\s+|pra\s+|pro\s+)?(.+)$", lowered)
    if match:
        target = _clean_target(match.group(1))
        if target and len(target.split()) <= 6:
            return UniversalIntent(UniversalAction.SWITCH_APP, target, target in _CONTEXTUAL_TARGETS)

    # traz o spotify (pra frente) / vai pro spotify / volta pra ela / foca o code
    match = re.match(rf"^(?:{_FOCUS_VERBS})\s+(?:o\s+|a\s+)?(.+?)(?:\s+pra\s+frente|\s+para\s+frente)?$",
                     lowered)
    if match:
        target = _clean_target(match.group(1))
        # "traz ele de volta" → alvo é "ele" (§25: voltar = focar/restaurar).
        target = re.sub(r"\s+de\s+volta$", "", target).strip()
        if target and len(target.split()) <= 6:
            return UniversalIntent(UniversalAction.FOCUS_APP, target, target in _CONTEXTUAL_TARGETS)

    return None


_NOTEPAD_MULTISTEP = re.compile(
    r"abre\s+(?:o\s+)?bloco\s+de\s+notas\s*,?\s*"
    r"escrev[ae]\s+[\"'‘’“”]?(?P<text>.+?)[\"'‘’“”]?\s*,?\s+"
    r"e\s+salva.*?como\s+"
    r"(?P<filename>[A-Za-z0-9_\- ]+\.(?:txt|log|md))"
    r"(?P<close_after>\s*,?\s+e\s+fech(?:a|e|ar)(?:\s+(?:ele|ela|o|a))?)?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def parse_notepad_multistep(text: str) -> dict | None:
    """nyra-full §26/§38: padrão canônico abrir→escrever→salvar, sem LLM."""
    value = " ".join((text or "").strip().split())
    match = _NOTEPAD_MULTISTEP.search(value)
    if not match:
        return None
    payload = match.groupdict()
    text_content = " ".join(payload["text"].split())
    filename = payload["filename"].strip()
    if not text_content or not filename:
        return None
    return {
        "text": text_content.strip("\"'‘’“”"),
        "filename": filename,
        "close_after": bool(payload.get("close_after")),
    }
