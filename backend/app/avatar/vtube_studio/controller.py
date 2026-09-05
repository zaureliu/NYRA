from app.avatar.vtube_studio.parameters import emotion_parameter_values, mouth_parameter_values, mouse_parameter_values

async def inject_state(client, state, mapping):
    values=mouth_parameter_values(state,mapping)
    if not values: return None
    return await client.call("InjectParameterDataRequest", {"faceFound":False,"mode":"set","parameterValues":values})


async def inject_emotion(client, emotion: str, intensity: float, mapping):
    values=emotion_parameter_values(emotion,intensity,mapping)
    if not values: return None
    return await client.call("InjectParameterDataRequest", {"faceFound":False,"mode":"set","parameterValues":values})


async def inject_mouse_tracking(client, frame, mapping):
    values = mouse_parameter_values(frame, mapping)
    if not values:
        return None
    return await client.call("InjectParameterDataRequest", {"faceFound":False,"mode":"set","parameterValues":values})
