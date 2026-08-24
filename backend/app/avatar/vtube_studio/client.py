from __future__ import annotations
import asyncio
import json
from typing import Any
from websockets.asyncio.client import connect

from app.avatar.vtube_studio.protocol import VTSProtocolError, request


class VTubeStudioClient:
    def __init__(self, host: str="127.0.0.1", port: int=8001) -> None:
        self.host=host; self.port=port; self.socket=None; self._lock=asyncio.Lock()
        self.last_error: str | None=None; self.requests_sent=0; self.last_message_type: str|None=None

    @property
    def url(self) -> str: return f"ws://{self.host}:{self.port}"

    async def connect(self) -> None:
        if self.socket is not None: return
        self.socket = await connect(self.url, open_timeout=3, close_timeout=1, max_size=2**20)

    async def close(self) -> None:
        if self.socket is not None:
            await self.socket.close(); self.socket=None

    async def call(self, message_type: str, data: dict | None=None, timeout: float=10) -> dict[str, Any]:
        async with self._lock:
            await self.connect(); payload=request(message_type, data)
            try:
                await self.socket.send(json.dumps(payload)); self.requests_sent+=1; self.last_message_type=message_type
                raw=await asyncio.wait_for(self.socket.recv(), timeout)
                response=json.loads(raw)
                self.last_error=None
            except Exception as error:
                self.last_error=type(error).__name__
                await self.close(); raise
            if response.get("messageType") == "APIError": raise VTSProtocolError(response)
            return response
