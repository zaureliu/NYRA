from enum import StrEnum
from pydantic import BaseModel, Field


class VTSConnectionState(StrEnum):
    DISABLED="DISABLED"; NOT_INSTALLED="NOT_INSTALLED"; API_DISABLED="API_DISABLED"
    CONNECTING="CONNECTING"; CONNECTED="CONNECTED"; AUTHENTICATING="AUTHENTICATING"
    AUTH_REQUIRED="AUTH_REQUIRED"; AUTHENTICATED="AUTHENTICATED"; MODEL_MISSING="MODEL_MISSING"
    MODEL_LOADED="MODEL_LOADED"; READY="READY"; RECONNECTING="RECONNECTING"; ERROR="ERROR"


class VTubeStudioConfig(BaseModel):
    enabled: bool = True
    renderer: str = Field(default="AUTO", pattern=r"^(AUTO|VTUBE_STUDIO|INTERNAL|LIVE2D|CURRENT)$")
    host: str = Field(default="127.0.0.1", pattern=r"^(localhost|127\.0\.0\.1)$")
    port: int = Field(default=8001, ge=1024, le=65535)
    auto_connect: bool = True
    model_id: str | None = Field(default=None, max_length=128)
    lip_sync: bool = True
    cursor_attention: bool = False
    physics_intensity: float = Field(default=0.65, ge=0, le=1)
    target_fps: int = Field(default=30, ge=15, le=60)
    spout_sender: str = Field(default="AUTO", min_length=1, max_length=255)
    presence_scale: float = Field(default=1.0, ge=0.1, le=4.0)
    presence_offset_x: float = Field(default=0.0, ge=-1.0, le=1.0)
    presence_offset_y: float = Field(default=0.0, ge=-1.0, le=1.0)
    frame_watchdog_seconds: int = Field(default=12, ge=5, le=60)
    state_hotkeys: dict[str, str] = Field(default_factory=dict)
    debug: bool = False


class VTSSettingsUpdate(VTubeStudioConfig):
    pass
