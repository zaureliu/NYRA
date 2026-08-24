async def list_expressions(client):
    return (await client.call("ExpressionStateRequest")).get("data", {}).get("expressions", [])

async def set_expression(client, file_name: str, active: bool=True):
    return await client.call("ExpressionActivationRequest", {"expressionFile":file_name,"active":active})
