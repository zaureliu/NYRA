async def health(client) -> dict:
    state=(await client.call("APIStateRequest")).get("data", {})
    return {"connected":True,"authenticated":bool(state.get("currentSessionAuthenticated")),"version":state.get("vTubeStudioVersion")}
