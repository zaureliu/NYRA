r"""Central path resolution for NYRA (dev repo vs installed/frozen layout).

Dev mode: source lives in the repository while mutable runtime state lives in
``%LOCALAPPDATA%\\NYRA`` (or ``NYRA_DATA_HOME``).

Installed mode (PyInstaller ``nyra-backend.exe`` spawned by the Tauri shell):

    * read-only assets shipped inside the bundle  -> ``RESOURCE_ROOT``
      (``sys._MEIPASS`` of the onedir build; config/identity templates);
    * writable state                              -> ``%LOCALAPPDATA%\NYRA``
      (``PROJECT_ROOT``/``CONFIG_ROOT``/``DATA_ROOT``/``LOG_ROOT`` plus the
      cache/backups/workflows subfolders).

The cwd is NEVER used as source of truth in installed mode. Secrets remain in
the Credential Broker; the DPAPI fallback vault migrates once via
:meth:`ensure_runtime_directories` without ever leaving the user profile.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger("nyra.paths")

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) or os.environ.get("NYRA_FROZEN") == "1"


def _installed_root() -> Path:
    override = os.environ.get("NYRA_DATA_HOME")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "NYRA"


FROZEN = _is_frozen()
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", _REPO_ROOT)) if FROZEN else _REPO_ROOT


def resolve_packaged_path(value: Path) -> Path:
    """Resolve um asset empacotado sem depender de cwd/repo/.venv no instalado.

    Absoluto -> intocado (override explícito do operador). Relativo:
    dev -> repo; instalado -> resource embutido (``_internal`` do onedir),
    com queda para o root gravável caso o asset não tenha sido empacotado.
    """
    if value.is_absolute():
        return value
    if FROZEN:
        bundled = RESOURCE_ROOT / value
        if bundled.exists():
            return bundled
        return PROJECT_ROOT / value
    return PROJECT_ROOT / value

if FROZEN:
    PROJECT_ROOT = _installed_root()
    RUNTIME_ROOT = PROJECT_ROOT
else:
    PROJECT_ROOT = _REPO_ROOT
    RUNTIME_ROOT = _installed_root()

BACKEND_ROOT = PROJECT_ROOT / "backend"
# Identidade vive no root GRAVÁVEL (overrides de pronúncia escrevem aqui);
# o bootstrap semeia o conteúdo a partir do resource somente-leitura.
IDENTITY_ROOT = PROJECT_ROOT / "identity"
DATA_ROOT = RUNTIME_ROOT / "data"
LOG_ROOT = RUNTIME_ROOT / "logs"
CONFIG_ROOT = PROJECT_ROOT / "config"

# Subpastas exigidas no modo instalado (§3 do packaging fix).
INSTALLED_SUBDIRS = ("config", "data", "logs", "cache", "backups", "workflows")

_CONFIG_TEMPLATE_FILES = (
    "default.yaml",
    "homelab_hosts.yaml",
    "runtime_services.yaml",
    "desktop_apps.yaml",
    "live2d_reactions.yaml",
    "vtube_parameter_mapping.yaml",
    "workflow_templates.json",
    "network_aliases.json",
)


def _copy_if_absent(source: Path, destination: Path) -> bool:
    if not source.is_file() or destination.exists():
        return False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        logger.info("installed_layout_seeded file=%s", destination.name)
        return True
    except OSError as error:
        logger.warning("seed_copy_failed src=%s error=%s", source.name, type(error).__name__)
        return False


def ensure_runtime_directories() -> None:
    """Cria a árvore de runtime; no modo instalado semeia templates ausentes.

    Preservação garantida: arquivos existentes nunca são sobrescritos. Estado
    do operador e o Credential Broker não são lidos nem incorporados ao pacote.
    """
    if FROZEN:
        for name in INSTALLED_SUBDIRS:
            try:
                (PROJECT_ROOT / name).mkdir(parents=True, exist_ok=True)
            except OSError as error:
                logger.warning("installed_dir_failed dir=%s error=%s", name, type(error).__name__)
        # Configs: template embutido -> %LOCALAPPDATA%\NYRA\config (só ausentes).
        bundled_config = RESOURCE_ROOT / "config"
        if bundled_config.is_dir():
            for name in _CONFIG_TEMPLATE_FILES:
                _copy_if_absent(bundled_config / name, CONFIG_ROOT / name)
            for extra in bundled_config.glob("*"):
                if extra.is_file():
                    _copy_if_absent(extra, CONFIG_ROOT / extra.name)
        # Identidade: só copia para o root gravável se ainda não existir lá
        # (IDENTITY_ROOT aponta para o resource embutido somente-leitura).
        bundled_identity = RESOURCE_ROOT / "identity"
        live_identity = PROJECT_ROOT / "identity"
        if bundled_identity.is_dir():
            live_identity.mkdir(parents=True, exist_ok=True)
            for extra in bundled_identity.glob("*"):
                if extra.is_file():
                    _copy_if_absent(extra, live_identity / extra.name)
        # Estado do operador nunca é empacotado: banco, credenciais, settings,
        # histórico, memória e topologia nascem somente no runtime local.
    for directory in (
        DATA_ROOT,
        LOG_ROOT,
        RUNTIME_ROOT / "cache",
        RUNTIME_ROOT / "downloads",
        RUNTIME_ROOT / "reports",
        RUNTIME_ROOT / "tmp",
        RUNTIME_ROOT / "selfdev",
        DATA_ROOT / "audio",
        DATA_ROOT / "recordings",
    ):
        directory.mkdir(parents=True, exist_ok=True)
