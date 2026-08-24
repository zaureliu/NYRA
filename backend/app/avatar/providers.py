from __future__ import annotations

from abc import ABC, abstractmethod
from app.avatar.controller import AvatarState


class AvatarProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def available(self) -> bool: ...

    @abstractmethod
    async def apply(self, state: AvatarState) -> None: ...


class CurrentRendererProvider(AvatarProvider):
    @property
    def name(self) -> str:
        return "current_renderer"

    async def available(self) -> bool:
        return True

    async def apply(self, state: AvatarState) -> None:
        return None


from app.avatar.vtube_studio.provider import VTubeStudioAvatarProvider
