import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "frontend" / "public" / "avatar" / "nyra_v2"
INTERNAL_MASTER = ROOT / "frontend" / "src" / "assets" / "nyra-v2" / "master" / "nyra-avatar-master.png"


def test_avatar_v2_manifest_has_one_coordinate_system():
    manifest = json.loads((PACK / "avatar-manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"].startswith("2.")
    assert manifest["pack"] == "nyra_v2"
    assert manifest["renderer"] == "unified-svg-layers"
    assert manifest["canvas"] == {
        "width": 1086,
        "height": 1448,
        "viewBox": "0 0 1086 1448",
        "preserveAspectRatio": "xMidYMid meet",
    }
    for landmark in ("leftEye", "rightEye", "mouth"):
        assert manifest[landmark]["center"] == manifest[landmark]["anchor"]
    assert manifest["headphones"]["group"] == "head"
    assert manifest["headphones"]["maxIndicatorScale"] <= 1.02


def test_avatar_v2_master_is_rgba_and_runtime_copy_is_exact():
    runtime_master = PACK / "master" / "nyra-avatar-master.png"
    internal = INTERNAL_MASTER.read_bytes()
    runtime = runtime_master.read_bytes()
    assert internal[:8] == b"\x89PNG\r\n\x1a\n"
    assert internal[25] == 6, "master interna deve ser PNG RGBA"
    assert hashlib.sha256(internal).digest() == hashlib.sha256(runtime).digest()


def test_avatar_v2_face_layers_preserve_full_canvas():
    layers = [
        PACK / "eyes" / name for name in (
            "open.svg", "seventy-five.svg", "half.svg", "twenty-five.svg", "gaze-base.svg", "closed.svg"
        )
    ] + [
        PACK / "mouth" / name for name in (
            "closed.svg", "small.svg", "medium.svg", "open.svg", "wide.svg", "smile.svg", "speaking-smile.svg"
        )
    ]
    geometry = re.compile(r'width="1086" height="1448" viewBox="0 0 1086 1448"')
    for layer in layers:
        assert layer.is_file()
        assert geometry.search(layer.read_text(encoding="utf-8")), layer.name


def test_avatar_v2_has_full_gaze_and_natural_blink_states():
    manifest = json.loads((PACK / "avatar-manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["gaze"]["directions"]) == {
        "front", "left_light", "left", "right_light", "right", "up_light", "up",
        "down_light", "down", "up_left", "up_right", "down_left", "down_right",
    }
    assert manifest["blink"]["sequence"] == [
        "open", "seventy_five", "half", "twenty_five", "closed",
        "twenty_five", "half", "seventy_five", "open",
    ]
