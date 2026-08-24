from enum import StrEnum
from pydantic import BaseModel, Field


class VTSConnectionState(StrEnum):
    DISABLED="DISABLED"; NOT_INSTALLED="NOT_INSTALLED"; API_DISABLED="API_DISABLED"
    CONNECTING="CONNECTING"; CONNECTED="CONNECTED"; AUTHENTICATING="AUTHENTICATING"
    AUTH_REQUIRED="AUTH_REQUIRED"; AUTHENTICATED="AUTHENTICATED"; MODEL_MISSING="MODEL_MISSING"
    MODEL_LOADED="MODEL_LOADED"; READY="READY"; RECONNECTING="RECONNECTING"; ERROR="ERROR"


class VTubeStudioConfig(BaseModel):
    enabled: bool = False
    renderer: str = Field(default="AUTO", pattern=r"^(AUTO|LIVE2D|CURRENT)$")
    host: str = Field(default="127.0.0.1", pattern=r"^(localhost|127\.0\.0\.1)$")
    port: int = Field(default=8001, ge=1024, le=65535)
    auto_connect: bool = True
    model_id: str | None = Field(default=None, max_length=128)
    lip_sync: bool = True
    cursor_attention: bool = True
    physics_intensity: float = Field(default=0.65, ge=0, le=1)
    target_fps: int = Field(default=30, ge=15, le=60)
    debug: bool = False


class VTSSettingsUpdate(VTubeStudioConfig):
    pass
