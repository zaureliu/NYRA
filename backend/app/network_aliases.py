from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import unicodedata

from pydantic import BaseModel, Field

from app.core.paths import CONFIG_ROOT


class RemoteHostAccess(BaseModel):
    enabled: bool = False
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default="", pattern=r"^[A-Za-z0-9._-]{0,64}$")
    platform: str = Field(default="linux", pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    capabilities: list[str] = Field(default_factory=list, max_length=30)
    private_key_path: str | None = Field(default=None, max_length=2048)
    known_hosts_path: str = Field(default="%USERPROFILE%\\.ssh\\known_hosts", max_length=2048)
    use_ssh_agent: bool = True
    auto_remediation_actions: list[str] = Field(default_factory=list, max_length=30)
    managed_resources: dict[str, list[str]] = Field(default_factory=dict)

    def resolve_path(self, value: str | None) -> Path | None:
        if not value:
            return None
        expanded = os.path.expandvars(value)
        path = Path(expanded).expanduser()
        return path if path.is_absolute() else CONFIG_ROOT.parent / path


class NetworkHostAlias(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    address: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,253}$")
    aliases: list[str] = Field(min_length=1, max_length=20)
    remote_shell: RemoteHostAccess = Field(default_factory=RemoteHostAccess)


class NetworkAliasConfig(BaseModel):
    hosts: list[NetworkHostAlias] = Field(default_factory=list, max_length=100)


class NetworkAliasRegistry:
    def __init__(self, path: Path = CONFIG_ROOT / "network_aliases.json") -> None:
        self.path = path
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.config = NetworkAliasConfig.model_validate(raw)
        self._lookup = {
            self._normalize(alias): host
            for host in self.config.hosts
            for alias in {host.id, *host.aliases}
        }

    def resolve(self, alias: str) -> NetworkHostAlias | None:
        return self._lookup.get(self._normalize(alias))

    def resolve_remote(self, alias: str) -> NetworkHostAlias | None:
        return self.resolve(alias)

    def find_remote_in_text(self, value: str) -> NetworkHostAlias | None:
        normalized = self._normalize(value)
        candidates = sorted(
            (
                (self._normalize(alias), host)
                for host in self.config.hosts
                if host.remote_shell.enabled
                for alias in {host.id, *host.aliases}
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        return next(
            (host for alias, host in candidates if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized)),
            None,
        )

    def remote_prompt_summary(self) -> str:
        lines = []
        for host in self.config.hosts:
            remote = host.remote_shell
            state = "enabled" if remote.enabled else "disabled"
            capabilities = ", ".join(remote.capabilities) or "none"
            lines.append(
                f"- {host.id} ({', '.join(host.aliases)}): SSH {state}; "
                f"platform={remote.platform}; capabilities={capabilities}"
            )
        return "\n".join(lines)

    def public_remote_hosts(self) -> list[dict]:
        return [
            {
                "id": host.id,
                "aliases": host.aliases,
                "address": host.address,
                "enabled": host.remote_shell.enabled,
                "port": host.remote_shell.port,
                "platform": host.remote_shell.platform,
                "capabilities": host.remote_shell.capabilities,
                "auto_remediation_actions": host.remote_shell.auto_remediation_actions,
            }
            for host in self.config.hosts
        ]

    def prompt_summary(self) -> str:
        return "\n".join(
            f"- {host.id} ({', '.join(host.aliases)}): {host.address}"
            for host in self.config.hosts
        )

    @staticmethod
    def _normalize(value: str) -> str:
        plain = "".join(
            char for char in unicodedata.normalize("NFKD", value.casefold())
            if not unicodedata.combining(char)
        )
        return " ".join(plain.split())


@lru_cache(maxsize=1)
def get_network_aliases() -> NetworkAliasRegistry:
    return NetworkAliasRegistry()
