"""NYRA Computer Autonomy V1 (nyra-7c) — camadas 1..7 como UM pipeline.

Módulos:
  perception   → ComputerPerceptionService  (camada 1)
  state        → ComputerStateService       (camada 2)
  intent       → IntentUnderstandingService (camada 3)
  verification → EffectVerificationService  (camada 5)
  usage        → UsageLearningService       (camada 6)
  skills_memory→ SkillMemoryService         (camada 7)

A camada 4 (Universal Operator) permanece em app.desktop (DesktopController /
handle_universal) — este pacote apenas a orquestra.
"""

from app.computer.perception import ComputerPerceptionService
from app.computer.state import ComputerStateService
from app.computer.intent import IntentUnderstandingService, NormalizedUserIntent
from app.computer.verification import EffectVerificationService, VerifiedEffect
from app.computer.usage import UsageLearningService, WorkflowCandidate
from app.computer.skills_memory import SkillMemoryService
from app.computer.pipeline import ComputerAutonomyService, HandleResult
from app.computer.artifacts import (
    ArtifactContextService,
    RecentArtifact,
    RecentArtifactMemory,
)

__all__ = [
    "ComputerPerceptionService",
    "ComputerStateService",
    "IntentUnderstandingService",
    "NormalizedUserIntent",
    "EffectVerificationService",
    "VerifiedEffect",
    "UsageLearningService",
    "WorkflowCandidate",
    "SkillMemoryService",
    "ComputerAutonomyService",
    "HandleResult",
    "ArtifactContextService",
    "RecentArtifact",
    "RecentArtifactMemory",
]
