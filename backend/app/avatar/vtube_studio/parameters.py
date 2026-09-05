from __future__ import annotations

PARAMETER_CANDIDATES = {
 "head_x":["FaceAngleX","ParamAngleX"], "head_y":["FaceAngleY","ParamAngleY"], "head_tilt":["FaceAngleZ","ParamAngleZ"],
 "eye_x":["EyeBallX","ParamEyeBallX","EyeLeftX","EyeRightX"], "eye_y":["EyeBallY","ParamEyeBallY","EyeLeftY","EyeRightY"],
 "mouth_open":["MouthOpen","VoiceVolumePlusMouthOpen","ParamMouthOpenY"], "mouth_form":["MouthSmile","ParamMouthForm"],
 "body_x":["BodyAngleX","ParamBodyAngleX"], "breathing":["ParamBreath"],
 "neural_link":["KazumiNeuralLink","ParamKazumiNeuralLink"], "attention":["KazumiAttention","ParamKazumiAttention"],
 "thinking":["KazumiThinking","ParamKazumiThinking"], "concern":["KazumiConcern","ParamKazumiConcern"], "amused":["KazumiAmused","ParamKazumiAmused"],
}

# Existing model rigs must not be renamed. Legacy custom parameters remain
# discoverable, after the preferred Kazumi names, for one migration release.
for _candidates in PARAMETER_CANDIDATES.values():
    _candidates.extend(item.replace('Kazumi', 'Nyra') for item in list(_candidates) if 'Kazumi' in item)


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


def mouse_parameter_values(frame, mapping: dict[str, list[str]]) -> list[dict]:
    """Map a normalized tracking frame only to parameters the model exposes."""
    from app.avatar.vtube_studio.models import MouseTrackingMode

    values: list[dict] = []
    if frame.mode != MouseTrackingMode.OFF or frame.reset_all:
        eye_x = 0.0 if frame.reset_all else frame.eye_x
        eye_y = 0.0 if frame.reset_all else frame.eye_y
        values.extend({"id": item, "value": eye_x, "weight": 1} for item in mapping.get("eye_x", []))
        values.extend({"id": item, "value": eye_y, "weight": 1} for item in mapping.get("eye_y", []))
    if frame.mode == MouseTrackingMode.HEAD_EYES or frame.reset_head or frame.reset_all:
        head_x = 0.0 if frame.reset_head or frame.reset_all else frame.head_x * 22.0
        head_y = 0.0 if frame.reset_head or frame.reset_all else frame.head_y * 14.0
        values.extend({"id": item, "value": head_x, "weight": 1} for item in mapping.get("head_x", []))
        values.extend({"id": item, "value": head_y, "weight": 1} for item in mapping.get("head_y", []))
    return values


def emotion_parameter_values(emotion: str, intensity: float, mapping: dict[str, list[str]]) -> list[dict]:
    """Only inject explicitly KAZUMI-owned custom emotion parameters.

    Generic face and mouth tracking parameters remain owned by VTube Studio and
    lip sync. Unsupported emotions therefore produce no parameter request.
    """
    selected = "concern" if emotion in {"concerned", "empathetic", "apologetic", "uncertain"} else "amused" if emotion == "amused" else None
    values: list[dict] = []
    for key in ("concern", "amused"):
        value = min(1.0, max(0.0, float(intensity) / .65)) if key == selected else 0.0
        values.extend({"id": parameter_id, "value": value, "weight": 1} for parameter_id in mapping.get(key, []))
    return values
