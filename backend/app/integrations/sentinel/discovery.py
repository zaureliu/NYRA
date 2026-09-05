from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.core.paths import DATA_ROOT
from app.integrations.sentinel.models import BRIDGE_VERSION, SentinelFingerprint, SentinelSettingsUpdate


LAST_KNOWN_PATH = DATA_ROOT / "sentinel" / "last-known.json"


@dataclass(frozen=True)
class SentinelCandidate:
    base_url: str
    fingerprint: SentinelFingerprint


class SentinelDiscovery:
    """Lightweight discovery: known hosts first, allowlisted private LAN last."""

    def __init__(self, timeout_seconds: float = 1.5, concurrency: int = 8, last_known_path: Path = LAST_KNOWN_PATH) -> None:
        self.timeout_seconds = timeout_seconds
        self.concurrency = max(1, min(concurrency, 12))
        self.last_known_path = last_known_path

    def candidates(self, config: SentinelSettingsUpdate) -> list[str]:
        values: list[str] = []
        if config.host and config.prefer_manual_host:
            values.append(self._base_url(config.host, config.port))
        last = self.load_last_known()
        if last:
            values.append(last)
        values.append(f"http://127.0.0.1:{config.port}")
        if config.host and not config.prefer_manual_host:
            values.append(self._base_url(config.host, config.port))
        return list(dict.fromkeys(values))

    async def discover(self, config: SentinelSettingsUpdate) -> SentinelCandidate | None:
        for url in self.candidates(config):
            candidate = await self.probe(url)
            if candidate:
                self.save_last_known(candidate)
                return candidate
        if not config.auto_discovery or not config.discovery_allowlist:
            return None
        return await self._discover_allowlisted(config)

    async def probe(self, base_url: str) -> SentinelCandidate | None:
        try:
            if not await asyncio.to_thread(self._is_local_url, base_url):
                return None
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False) as client:
                response = await client.get(f"{base_url.rstrip('/')}/api/integrations/kazumi/health")
            if response.status_code != 200 or len(response.content) > 32 * 1024:
                return None
            fingerprint = SentinelFingerprint.model_validate(response.json())
            return SentinelCandidate(base_url.rstrip("/"), fingerprint)
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def _discover_allowlisted(self, config: SentinelSettingsUpdate) -> SentinelCandidate | None:
        semaphore = asyncio.Semaphore(self.concurrency)
        found = asyncio.Event()

        async def inspect(host: str) -> SentinelCandidate | None:
            if found.is_set():
                return None
            async with semaphore:
                result = await self.probe(f"https://{host}:{config.port}")
                if result:
                    found.set()
                return result

        hosts: list[str] = []
        for raw in config.discovery_allowlist:
            network = ipaddress.ip_network(raw, strict=False)
            if network.is_private and network.version == 4 and network.num_addresses <= 256:
                hosts.extend(str(host) for host in network.hosts())
        tasks = [asyncio.create_task(inspect(host)) for host in dict.fromkeys(hosts)]
        try:
            for completed in asyncio.as_completed(tasks):
                result = await completed
                if result:
                    self.save_last_known(result)
                    return result
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    def save_last_known(self, candidate: SentinelCandidate) -> None:
        self.last_known_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.last_known_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "base_url": candidate.base_url,
            "instance_id": candidate.fingerprint.instance_id,
            "bridge_version": candidate.fingerprint.api_version,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.last_known_path)

    def load_last_known(self) -> str:
        try:
            value = json.loads(self.last_known_path.read_text(encoding="utf-8"))
            base_url = str(value.get("base_url") or "").strip().rstrip("/")
            parsed = urlsplit(base_url)
            try:
                address = ipaddress.ip_address(parsed.hostname or "")
            except ValueError:
                return ""
            if parsed.scheme == "http" and not address.is_loopback:
                # Migração fail-closed de registros antigos: LAN só via HTTPS.
                base_url = parsed._replace(scheme="https").geturl().rstrip("/")
            return base_url if self._is_local_url(base_url) else ""
        except (OSError, ValueError, TypeError):
            return ""

    def clear_last_known(self) -> None:
        self.last_known_path.unlink(missing_ok=True)

    @staticmethod
    def compatible(candidate: SentinelCandidate) -> bool:
        return candidate.fingerprint.api_version == str(BRIDGE_VERSION)

    @staticmethod
    def _base_url(host: str, port: int) -> str:
        if host.startswith(("http://", "https://")):
            parsed = urlsplit(host)
            scheme = parsed.scheme
            try:
                if scheme == "http" and not ipaddress.ip_address(parsed.hostname or "").is_loopback:
                    scheme = "https"
            except ValueError:
                scheme = "https"
            if parsed.port:
                authority = f"[{parsed.hostname}]" if parsed.hostname and ":" in parsed.hostname else parsed.hostname
                return f"{scheme}://{authority}:{parsed.port}"
            return f"{scheme}://{parsed.hostname}:{port}"
        if ":" in host and not host.startswith("["):
            try:
                address = ipaddress.ip_address(host)
                scheme = "http" if address.is_loopback else "https"
                return f"{scheme}://[{host}]:{port}"
            except ValueError:
                pass
        try:
            address = ipaddress.ip_address(host)
            scheme = "http" if address.is_loopback else "https"
        except ValueError:
            scheme = "https"
        return f"{scheme}://{host}:{port}"

    @staticmethod
    def _is_local_url(base_url: str) -> bool:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return False
        try:
            address = ipaddress.ip_address(parsed.hostname)
            _ = parsed.port
        except ValueError:
            return False
        is_local = address.is_private or address.is_loopback or address.is_link_local
        if parsed.scheme == "http":
            return is_local and address.is_loopback
        return is_local
