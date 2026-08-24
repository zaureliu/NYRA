from __future__ import annotations

from app.integrations.sentinel.models import SentinelHistoryQuery, SentinelSearchQuery
from app.tools.models import EmptyInput, RiskLevel
from app.tools.registry import ToolDefinition, ToolRegistry


def register_sentinel_tools(registry: ToolRegistry, connector) -> None:
    async def get_sentinel_status():
        return connector.status()

    async def get_sentinel_connection_status():
        status = connector.status()
        return {key: status[key] for key in (
            "enabled", "state", "host", "sentinel_version", "bridge_version",
            "connected_since", "last_error",
        )}

    async def get_sentinel_recent_events(hours: int = 24, limit: int = 50, severity: str = ""):
        return {"events": await connector.history.recent(hours, limit, severity)}

    async def get_sentinel_event_summary(hours: int = 1, limit: int = 50, severity: str = ""):
        return await connector.summary(hours)

    async def search_sentinel_events(query: str, hours: int = 720, limit: int = 50, severity: str = ""):
        return {"events": await connector.history.search(query, hours, limit, severity)}

    registry.register(ToolDefinition("get_sentinel_status", "Lê o status atual da integração Utamo Sentinel.", RiskLevel.READ_ONLY, EmptyInput, get_sentinel_status))
    registry.register(ToolDefinition("get_sentinel_connection_status", "Lê conexão, versão e host da bridge Sentinel.", RiskLevel.READ_ONLY, EmptyInput, get_sentinel_connection_status))
    registry.register(ToolDefinition("get_sentinel_recent_events", "Lista eventos recentes recebidos do Sentinel.", RiskLevel.READ_ONLY, SentinelHistoryQuery, get_sentinel_recent_events))
    registry.register(ToolDefinition("get_sentinel_event_summary", "Resume severidades e eventos recentes do Sentinel.", RiskLevel.READ_ONLY, SentinelHistoryQuery, get_sentinel_event_summary))
    registry.register(ToolDefinition("search_sentinel_events", "Pesquisa o histórico local read-only de eventos Sentinel.", RiskLevel.READ_ONLY, SentinelSearchQuery, search_sentinel_events))
