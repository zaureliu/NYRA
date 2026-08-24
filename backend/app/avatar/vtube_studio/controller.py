from app.avatar.vtube_studio.parameters import parameter_values

async def inject_state(client, state, mapping):
    values=parameter_values(state,mapping)
    if not values: return None
    return await client.call("InjectParameterDataRequest", {"faceFound":False,"mode":"set","parameterValues":values})
