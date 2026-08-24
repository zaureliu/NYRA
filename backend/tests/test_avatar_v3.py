import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "frontend" / "public" / "avatar" / "nyra_v3"


def test_avatar_manifest_assets_and_fallback_exist():
    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"].startswith("3.")
    assert manifest["renderer"] == "layered"
    for key in ("desktop", "portrait", "symbol"):
        relative = manifest["assets"][key].removeprefix("/avatar/nyra_v3/")
        assert (PACK / relative).is_file(), key
    fallback = ROOT / "frontend" / "public" / manifest["assets"]["fallback"].removeprefix("/")
    assert fallback.is_file()


def test_avatar_rasters_have_real_alpha():
    for path in (PACK / "desktop" / "nyra-desktop-full.png", PACK / "portrait" / "nyra-portrait.png"):
        header = path.read_bytes()[:26]
        assert header[:8] == b"\x89PNG\r\n\x1a\n"
        assert header[25] in {4, 6}, f"{path.name} não possui canal alpha PNG"
