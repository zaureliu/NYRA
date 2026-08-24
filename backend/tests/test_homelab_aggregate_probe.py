"""Closure Parte 28: mensagem agregada do Homelab nunca contradiz hosts
individuais nem integrações READY (ex.: HA pronto + hosts inalcançáveis não
pode virar "NENHUM_HOST_ALCANÇÁVEL" genérico de erro duro)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.core.capabilities import _homelab_probe


@dataclass
class _FakeOverview:
    summary: dict[str, int] = field(default_factory=dict)
    hosts: list[Any] = field(default_factory=list)


class _FakeHomelab:
    def __init__(self, overview: _FakeOverview) -> None:
        self._overview = overview

    async def overview(self, *, force: bool = False) -> _FakeOverview:
        return self._overview


class _FakeServices:
    def __init__(self, homelab_enabled: bool = True, overview: _FakeOverview | None = None) -> None:
        self.settings = type("S", (), {"homelab_enabled": homelab_enabled})()
        self.homelab = _FakeHomelab(overview or _FakeOverview())


def _run(services: Any) -> dict[str, Any]:
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_homelab_probe(services))


def test_homelab_disabled_stays_disabled():
    result = _run(_FakeServices(homelab_enabled=False))
    assert result["state"] == "DISABLED"


def test_partial_reachability_is_degraded_with_honest_message():
    # 1 host OK, 2 indisponíveis: DEGRADED com contagem, sem erro duro.
    services = _FakeServices(overview=_FakeOverview(
        summary={"ONLINE": 1, "UNREACHABLE": 2}, hosts=[object(), object(), object()],
    ))
    result = _run(services)
    assert result["state"] == "DEGRADED"
    assert "Alguns componentes indisponíveis" in str(result["health"])
    assert "1/3" in str(result["health"])


def test_zero_reachable_reports_hosts_down_without_claiming_total_outage():
    services = _FakeServices(overview=_FakeOverview(
        summary={"UNREACHABLE": 3}, hosts=[object() for _ in range(3)],
    ))
    result = _run(services)
    assert result["state"] == "DEGRADED"
    assert result["last_error"] == "HOMELAB_HOSTS_UNREACHABLE"
    # Mensagem antiga e contraditória não pode voltar:
    assert "NENHUM_HOST_ALCANÇÁVEL" not in str(result.get("health"))


def test_auth_failed_host_counts_as_reachable():
    # Host up rejeitando credencial NÃO é host inalcançável (§7.2).
    services = _FakeServices(overview=_FakeOverview(
        summary={"AUTHENTICATION_FAILED": 1}, hosts=[object()],
    ))
    result = _run(services)
    assert result["state"] == "READY"
