"""Smoke do layout instalado: roda o bootstrap frozen sem compilar nada.

Simula o modo instalado (KAZUMI_FROZEN=1 + KAZUMI_DATA_HOME temporário) e valida:
    * subpastas config/data/logs/cache/backups/workflows criadas;
    * configs semeadas a partir do repo (apenas ausentes);
    * settings carregam (default.yaml + estado migrado);
    * Credential Broker resolve no novo DATA_ROOT.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
SANDBOX = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO.parent / ".tmp" / "kazumi-frozen-smoke"


def main() -> int:
    os.environ["KAZUMI_FROZEN"] = "1"
    os.environ["KAZUMI_DATA_HOME"] = str(SANDBOX)
    os.environ["KAZUMI_WATCHDOG_ENABLED"] = "0"
    # _MEIPASS não existe fora do exe: aponta RESOURCE_ROOT para o repo via monkeypatch tardio.
    sys.path.insert(0, str(BACKEND_DIR))

    from app.core import paths as paths_mod

    paths_mod.RESOURCE_ROOT = REPO  # simula o resource embutido (raiz do repo)
    paths_mod.FROZEN = True
    paths_mod.PROJECT_ROOT = SANDBOX
    paths_mod.CONFIG_ROOT = SANDBOX / "config"
    paths_mod.DATA_ROOT = SANDBOX / "data"
    paths_mod.LOG_ROOT = SANDBOX / "logs"

    paths_mod.ensure_runtime_directories()

    missing_dirs = [name for name in paths_mod.INSTALLED_SUBDIRS if not (SANDBOX / name).is_dir()]
    assert not missing_dirs, f"subpastas ausentes: {missing_dirs}"
    for expected in ("config/default.yaml", "config/homelab_hosts.yaml", "identity/system_prompt.md"):
        assert (SANDBOX / expected).is_file(), f"seed ausente: {expected}"

    from app.core.config import get_settings

    settings = get_settings()
    assert settings.database_path == SANDBOX / "data" / "kazumi.db", settings.database_path
    assert settings.homelab_registry_path == SANDBOX / "config" / "homelab_hosts.yaml"

    from app.operator.credentials import CredentialBroker

    broker = CredentialBroker(approvals=None)
    listing = broker.list_credentials()
    print(f"broker backend={listing.get('backend')} count={listing.get('count')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
