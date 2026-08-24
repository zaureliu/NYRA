import pytest
from pydantic import ValidationError

from app.tools import create_tool_registry


@pytest.mark.asyncio
async def test_local_stats_tool_is_read_only_and_works():
    registry = create_tool_registry()
    result = await registry.execute("get_local_system_stats", {})
    assert result.ok
    assert 0 <= result.data["memory_percent"] <= 100
    assert all(item["risk"] == "READ_ONLY" for item in registry.descriptions())


@pytest.mark.asyncio
async def test_tool_allowlist_and_input_validation():
    registry = create_tool_registry()
    with pytest.raises(KeyError):
        await registry.execute("run_shell", {"command": "whoami"})
    with pytest.raises(ValidationError):
        await registry.execute("tcp_port_check", {"host": "localhost; whoami", "port": 80})

