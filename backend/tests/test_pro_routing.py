from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.character.context import is_simple_conversation
from app.tools.models import EmptyInput, RiskLevel, ToolResult
from app.tools.registry import (
    DOMAIN_CONVERSATION,
    DOMAIN_DESKTOP,
    DOMAIN_FILESYSTEM,
    DOMAIN_HOMELAB_HA,
    DOMAIN_HOMELAB_PROXMOX,
    ToolDefinition,
    ToolRegistry,
    classify_domain,
)


class _In(BaseModel):
    query: str = ""


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def ok(**_kwargs) -> dict:
        return {"success": True}

    for name, risk in [
        ("desktop_launch", RiskLevel.LOW_RISK),
        ("desktop_close", RiskLevel.LOW_RISK),
        ("desktop_windows", RiskLevel.READ_ONLY),
        ("ui_set_text", RiskLevel.LOW_RISK),
        ("proxmox_list_vms", RiskLevel.READ_ONLY),
        ("home_assistant_get_states", RiskLevel.READ_ONLY),
        ("openwrt_status", RiskLevel.READ_ONLY),
        ("get_network_status", RiskLevel.READ_ONLY),
        ("filesystem_list_directory", RiskLevel.READ_ONLY),
        ("system_shell", RiskLevel.READ_ONLY),
    ]:
        registry.register(ToolDefinition(name, "tool", risk, _In, ok))
    return registry


@pytest.mark.parametrize(
    ("text", "domain"),
    [
        ("abre o bloco de notas", DOMAIN_DESKTOP),
        ("Kazumi, abre a calculadora.", DOMAIN_DESKTOP),
        ("fecha o vs code", DOMAIN_DESKTOP),
        ("verifica o Proxmox", DOMAIN_HOMELAB_PROXMOX),
        ("quais VMs estão rodando?", DOMAIN_HOMELAB_PROXMOX),
        ("o Home Assistant está online?", DOMAIN_HOMELAB_HA),
        ("lista arquivos dessa pasta", DOMAIN_FILESYSTEM),
        ("oi tudo bem?", DOMAIN_CONVERSATION),
        ("me explica isso", DOMAIN_CONVERSATION),
    ],
)
def test_classify_domain(text: str, domain: str):
    assert classify_domain(text) == domain


def test_llm_tools_subset_desktop_excludes_homelab_and_shell():
    registry = _registry()
    names = {item["function"]["name"] for item in registry.llm_tools(DOMAIN_DESKTOP)}
    assert {"desktop_launch", "desktop_close", "desktop_windows", "ui_set_text"} <= names
    assert "proxmox_list_vms" not in names
    assert "system_shell" not in names
    assert len(names) < len(registry.llm_tools())


def test_llm_tools_conversation_is_empty():
    registry = _registry()
    assert registry.llm_tools(DOMAIN_CONVERSATION) == []


def test_llm_tools_generic_keeps_all():
    registry = _registry()
    assert len(registry.llm_tools()) == 10


def test_router_imperative_without_known_target():
    registry = _registry()
    assert registry.should_route_to_agent("rode os testes") is True
    assert registry.should_route_to_agent("abre o zumbi runner") is True
    assert registry.should_route_to_agent("oi tudo bem?") is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Oi, tudo bem?", True),
        ("bom dia", True),
        ("obrigado!", True),
        ("abre o bloco de notas", False),
        ("verifica o home assistant", False),
        ("", False),
    ],
)
def test_is_simple_conversation(text: str, expected: bool):
    assert is_simple_conversation(text) is expected


@pytest.mark.asyncio
async def test_schema_cache_returns_same_content():
    import json

    registry = _registry()
    first = registry.llm_tools(DOMAIN_DESKTOP)
    second = registry.llm_tools(DOMAIN_DESKTOP)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
