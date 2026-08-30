from __future__ import annotations

PARAMETER_CANDIDATES = {
 "head_x":["FaceAngleX","ParamAngleX"], "head_y":["FaceAngleY","ParamAngleY"], "head_tilt":["FaceAngleZ","ParamAngleZ"],
 "eye_x":["EyeLeftX","EyeRightX","FacePositionX","ParamEyeBallX"], "eye_y":["EyeLeftY","EyeRightY","FacePositionY","ParamEyeBallY"],
 "mouth_open":["MouthOpen","VoiceVolumePlusMouthOpen","ParamMouthOpenY"], "mouth_form":["MouthSmile","ParamMouthForm"],
 "body_x":["BodyAngleX","ParamBodyAngleX"], "breathing":["ParamBreath"],
 "neural_link":["NyraNeuralLink","ParamNyraNeuralLink"], "attention":["NyraAttention","ParamNyraAttention"],
 "thinking":["NyraThinking","ParamNyraThinking"], "concern":["NyraConcern","ParamNyraConcern"], "amused":["NyraAmused","ParamNyraAmused"],
}


def discover_ids(data: dict) -> set[str]:
    return {str(item.get("name") or item.get("id")) for key in ("defaultParameters","customParameters") for item in data.get(key, [])}


def resolve_mapping(available: set[str]) -> dict[str, list[str]]:
    return {key:[item for item in candidates if item in available] for key,candidates in PARAMETER_CANDIDATES.items()}


def parameter_values(state, mapping: dict[str,list[str]]) -> list[dict]:
    values={"head_x":state.head_x*30,"head_y":state.head_y*30,"head_tilt":state.head_tilt*30,
            "eye_x":state.eye_x,"eye_y":state.eye_y,"mouth_open":state.mouth_open,
            "body_x":state.body_x*10,"breathing":state.breathing,
            "neural_link":0 if state.neural_link=="idle" else 1,
            "thinking":1 if state.neural_link=="thinking" else 0,
            "concern":1 if state.expression=="concerned" else 0,"amused":1 if state.expression=="amused" else 0}
    return [{"id":pid,"value":values.get(key,0),"weight":1} for key,ids in mapping.items() for pid in ids]


def mouth_parameter_values(state, mapping: dict[str, list[str]]) -> list[dict]:
    """Inject only lip-sync; VTS remains owner of head, eyes and tracking."""
    return [
        {"id": parameter_id, "value": state.mouth_open, "weight": 1}
        for parameter_id in mapping.get("mouth_open", [])
    ]
