from __future__ import annotations
from uuid import uuid4

API_NAME = "VTubeStudioPublicAPI"
API_VERSION = "1.0"


def request(message_type: str, data: dict | None = None) -> dict:
    return {"apiName": API_NAME, "apiVersion": API_VERSION, "requestID": uuid4().hex[:32],
            "messageType": message_type, "data": data or {}}


class VTSProtocolError(RuntimeError):
    def __init__(self, response: dict) -> None:
        data = response.get("data", {})
        self.error_id = data.get("errorID")
        super().__init__(str(data.get("message") or response.get("messageType") or "VTube Studio API error"))
