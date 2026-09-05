"""Evidence-versioned plans; outdated tickets cannot execute downstream steps."""
from typing import Literal
from pydantic import BaseModel, Field

from .models import HardwareError, now


class PlanStep(BaseModel):
    name: str
    revision: int
    state: Literal['PENDING', 'RUNNING', 'COMPLETED', 'INVALIDATED'] = 'PENDING'
    depends_on: dict = Field(default_factory=dict)


class PlanRevision(BaseModel):
    revision: int = 0
    assumptions: dict = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    steps: list[PlanStep] = Field(default_factory=list)
    invalidated_steps: list[PlanStep] = Field(default_factory=list)
    changes: list[dict] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    current_step: str | None = None

    def revise(self, evidence, new_steps, *, reason, source):
        if source in ('user_claim', 'llm_assumption'):
            raise HardwareError('UNOBSERVED_PLAN_EVIDENCE')
        conflicts = {key for key, value in evidence.items()
                     if key in {**self.assumptions, **self.evidence} and {**self.assumptions, **self.evidence}[key] != value}
        for step in self.steps:
            if step.state != 'COMPLETED' or conflicts.intersection(step.depends_on):
                step.state = 'INVALIDATED'
                self.invalidated_steps.append(step.model_copy(deep=True))
        completed = [step for step in self.steps if step.state == 'COMPLETED']
        self.assumptions = {k: v for k, v in self.assumptions.items() if k not in evidence}
        self.evidence.update(evidence)
        self.revision += 1
        self.steps = completed + [PlanStep(name=name, revision=self.revision, depends_on=dict(self.evidence)) for name in new_steps]
        self.changes = (self.changes + [{'revision': self.revision, 'reason': reason, 'source': source,
                                      'invalidated_assumptions': sorted(conflicts), 'new_steps': new_steps, 'at': now()}])[-20:]
        self.invalidated_steps = self.invalidated_steps[-60:]
        self.current_step = None
        return self.revision

    def enter(self, name, revision):
        if revision != self.revision:
            raise HardwareError('PLAN_REVISION_INVALIDATED')
        step = next((s for s in self.steps if s.name == name and s.state == 'PENDING' and s.revision == revision), None)
        if step is None or any(self.evidence.get(k) != v for k, v in step.depends_on.items()):
            raise HardwareError('PLAN_STEP_INVALIDATED')
        step.state, self.current_step = 'RUNNING', name

    def finish(self, name, revision):
        if revision != self.revision:
            raise HardwareError('PLAN_REVISION_INVALIDATED')
        step = next((s for s in self.steps if s.name == name and s.state == 'RUNNING'), None)
        if step is None:
            raise HardwareError('PLAN_STEP_NOT_RUNNING')
        step.state, self.current_step = 'COMPLETED', None


def reconcile_target(projects, meta, identity, revision):
    """Probe/discovery mismatch invalidates pinout, library and upload choices together."""
    if identity.get('source') not in ('usb_discovery', 'serial_chip_probe') or identity.get('simulated'):
        raise HardwareError('UNOBSERVED_PLAN_EVIDENCE')
    previous = meta['board']
    chip = identity.get('chip')
    board = identity.get('board')
    changed = (chip and chip != previous['chip']) or (board and board['board_id'] != previous['board_id'])
    if not changed:
        return False
    revision.revise({'chip': chip, 'board': (board or {}).get('board_id')},
                    ['identify', 'research.pinout', 'toolchain.select', 'code.plan', 'code.edit', 'build', 'verify.target'],
                    reason='observed_target_conflicts_with_previous_plan', source=identity['source'])
    meta['build'], meta['flash'], meta['hardware_context'] = {}, {}, {}
    meta['libraries'] = []
    meta['plan_revision'] = revision.model_dump()
    if not board:
        meta['blocker'] = 'BOARD_UNKNOWN'
        projects.save(meta)
        raise HardwareError('BOARD_UNKNOWN')
    from .adapters import PROFILES
    trusted = next((p for _, p in PROFILES if p.board_id == board['board_id'] and p.chip == chip), None)
    if not trusted:
        raise HardwareError('UNTRUSTED_BUILD_TARGET')
    meta['board'], meta['framework'], meta['sources'] = trusted.model_dump(), trusted.framework, []
    config = f'[env:kazumi]\nplatform = {trusted.platform}\nboard = {trusted.board_id}\nframework = {trusted.framework}\nmonitor_speed = 115200\n'
    projects.write(meta['project_id'], 'platformio.ini', config)
    projects.checkpoint(meta)
    return True
