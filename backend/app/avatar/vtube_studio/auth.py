from __future__ import annotations
import json
import os
from pathlib import Path
from app.avatar.vtube_studio.client import VTubeStudioClient

PLUGIN_NAME = "NYRA Avatar Bridge"
PLUGIN_DEVELOPER = "NYRA Local"


class VTSAuth:
    def __init__(self, client: VTubeStudioClient, token_path: Path) -> None:
        self.client=client; self.token_path=token_path

    def load_token(self) -> str | None:
        try: return str(json.loads(self.token_path.read_text(encoding="utf-8"))["token"])
        except (OSError, ValueError, KeyError): return None

    def save_token(self, token: str) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp=self.token_path.with_suffix(".tmp"); tmp.write_text(json.dumps({"token":token})+"\n", encoding="utf-8")
        os.replace(tmp, self.token_path)

    def clear_token(self) -> None:
        try: self.token_path.unlink()
        except FileNotFoundError: pass

    async def state(self) -> dict:
        return (await self.client.call("APIStateRequest"))["data"]

    async def request_token(self) -> str:
        response=await self.client.call("AuthenticationTokenRequest", {"pluginName":PLUGIN_NAME, "pluginDeveloper":PLUGIN_DEVELOPER}, 120)
        token=str(response["data"]["authenticationToken"]); self.save_token(token); return token

    async def authenticate(self, token: str | None=None) -> bool:
        value=token or self.load_token()
        if not value: return False
        response=await self.client.call("AuthenticationRequest", {"pluginName":PLUGIN_NAME, "pluginDeveloper":PLUGIN_DEVELOPER, "authenticationToken":value})
        return bool(response.get("data", {}).get("authenticated"))
