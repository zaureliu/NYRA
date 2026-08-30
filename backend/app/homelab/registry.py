"""Unified Host Registry.

Single source of truth for homelab hosts. Loaded from an ignored local registry
(path overridable via NYRA_HOMELAB_REGISTRY_PATH).
The registry stores no credentials: hosts point to a credentials_profile that
is resolved against settings or the Trusted Host Registry at execution time.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.core.config import Settings
from app.homelab.models import (
    HostDefinition,
    HomelabRegistryFile,
)


logger = logging.getLogger("nyra.homelab")

# A public clone must not guess or disclose an operator's topology. Real hosts
# are loaded only from the ignored local registry or an explicit override.
DEFAULT_HOSTS: list[dict] = []


class HomelabHostRegistry:
    def __init__(self, path: Path | None = None, hosts: list[HostDefinition] | None = None) -> None:
        self.path = path
        self._hosts: dict[str, HostDefinition] = {}
        self._alias_index: dict[str, str] = {}
        if hosts is not None:
            self._load_definitions(hosts)
        else:
            self.reload()

    def reload(self) -> None:
        definitions = self._read_file() if self.path else []
        if not definitions:
            definitions = [HostDefinition(**item) for item in DEFAULT_HOSTS]
        self._load_definitions(definitions)

    def _read_file(self) -> list[HostDefinition]:
        if not self.path or not Path(self.path).is_file():
            return []
        try:
            raw = yaml.safe_load(Path(self.path).read_text(encoding="utf-8")) or {}
            parsed = HomelabRegistryFile.model_validate(raw)
            return parsed.hosts
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("homelab_registry_unreadable", extra={"error_type": type(exc).__name__})
            raise
        except ValidationError:
            logger.exception("homelab_registry_invalid")
            raise

    def _load_definitions(self, definitions: list[HostDefinition]) -> None:
        hosts: dict[str, HostDefinition] = {}
        alias_index: dict[str, str] = {}
        for host in definitions:
            if host.id in hosts:
                raise ValueError(f"ID de host duplicado no registry: {host.id}")
            for alias in [host.id.casefold(), *host.aliases]:
                owner = alias_index.get(alias)
                if owner and owner != host.id:
                    raise ValueError(f"Alias duplicado {alias!r}: {owner} e {host.id}")
                alias_index[alias] = host.id
            hosts[host.id] = host
        self._hosts = hosts
        self._alias_index = alias_index

    @staticmethod
    def default_registry(settings: Settings) -> "HomelabHostRegistry":
        return HomelabHostRegistry(path=settings.homelab_registry_path)

    def all_hosts(self) -> list[HostDefinition]:
        return list(self._hosts.values())

    def get(self, host_id: str) -> HostDefinition | None:
        return self._hosts.get(host_id.strip().casefold())

    def resolve(self, reference: str) -> HostDefinition | None:
        """Resolve by id or any registered alias, case-insensitive."""
        key = re.sub(r"\s+", " ", str(reference or "").strip()).casefold()
        if not key:
            return None
        host_id = self._alias_index.get(key)
        if host_id:
            return self._hosts.get(host_id)
        # Tolerate display names ("VM do Home Assistant" style references).
        for candidate in self._hosts.values():
            if candidate.display_name.casefold() == key:
                return candidate
        return None

    def public_hosts(self) -> list[dict]:
        return [self.public_host(host) for host in self._hosts.values()]

    @staticmethod
    def public_host(host: HostDefinition) -> dict:
        return {
            "id": host.id,
            "display_name": host.display_name,
            "type": host.type.value,
            "address": host.address,
            "aliases": host.aliases,
            "enabled": host.enabled,
            "integration": host.integration.value,
            "credentials_profile": host.credentials_profile,
            "capabilities": host.capabilities.model_dump(),
            "metadata": host.metadata,
        }


def build_default_yaml_text() -> str:
    """Serialize a safe, empty registry template."""
    doc = {"version": 1, "hosts": DEFAULT_HOSTS}
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
