async def list_hotkeys(client):
    return (await client.call("HotkeysInCurrentModelRequest", {})).get("data", {}).get("availableHotkeys", [])

async def trigger_hotkey(client, hotkey_id: str):
    return await client.call("HotkeyTriggerRequest", {"hotkeyID":hotkey_id})
