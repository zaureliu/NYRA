"""Software continuation and context, deliberately independent of live USB presence."""
import re

from app.intelligence.models import MemoryKind
from .adapters import PROFILES
from .context import ProjectContext
from .models import BoardProfile, HardwareError
from .replanning import PlanRevision
from .technical_profile import technical_profile, profile_reply


class ProjectWorkflow:
    def __init__(self, engine):
        self.engine = engine
        self.context = ProjectContext(engine.projects)

    async def resolve(self, text):
        engine = self.engine
        artifacts = getattr(getattr(engine.services, 'computer', None), 'artifacts', None)
        paths = [a.path for a in reversed(getattr(artifacts, 'items', [])) if a.host_scope == 'local']
        workspace = None
        desktop = getattr(engine.services, 'desktop', None)
        if desktop and hasattr(desktop, 'status_windows'):
            windows = desktop.status_windows(query='Visual Studio Code').get('windows', [])
            matches = [key for key, name in engine.projects.index.items() if any(name in w.get('title', '') for w in windows)]
            if len(matches) == 1:
                workspace = engine.projects.path(matches[0])
        memories, loops = [], []
        intelligence = engine.services.intelligence
        if not engine.projects.active or re.search(r'\b(?:aquele|anterior)\b', text):
            if hasattr(intelligence.memory, 'retrieve'):
                memories = [m.project for m in await intelligence.memory.retrieve(text, kinds=[MemoryKind.PROJECT], limit=4)]
            if hasattr(intelligence.open_loops, 'find_relevant'):
                loop = await intelligence.open_loops.find_relevant(text)
                loops = [loop.related_project] if loop else []
        return self.context.resolve(text, artifact_paths=paths, workspace=workspace, memory_projects=memories, loop_projects=loops)

    async def reference(self, board_id, *, hardware_context=None):
        engine = self.engine
        if not engine.full:
            raise HardwareError('HARDWARE_AUTONOMY_DISABLED')
        profile = next((p.model_copy(deep=True) for _, p in PROFILES if p.board_id == board_id), None)
        if not profile:
            raise HardwareError('UNTRUSTED_BUILD_TARGET')
        profile = await engine.toolchains.select(profile)
        await engine.toolchains.ensure()
        meta = engine.projects.create(profile)
        meta.update(reference=True, hardware_context=hardware_context or {}, completed_features=[], pending_goals=[])
        engine.projects.save(meta)
        return meta

    async def run(self, goal):
        engine = self.engine
        meta = engine.projects.read(goal.target_project) if goal.target_project else await self.resolve(goal.user_intent.text)
        goal.target_project = meta['project_id']
        if goal.user_intent.target and goal.user_intent.target.lower().replace('-', '') not in (
                meta['board']['name'] + ' ' + meta['board']['chip']).lower().replace('-', ''):
            raise HardwareError('PROJECT_TARGET_MISMATCH')
        if goal.desired_effect == 'resume':
            return await engine.complete(goal, 'Recuperei o mesmo projeto e seus arquivos. A presença do dispositivo será revalidada antes de qualquer ação física.', physical=False)
        if not engine.full:
            raise HardwareError('HARDWARE_AUTONOMY_DISABLED')
        # A pin explicitly specified by the operator is a DESIGN requirement,
        # never an observed component. This path only builds, never flashes.
        pin = re.search(r'(?i)\bbot[aã]o\b.{0,90}?\b(?:gpio|pino|d)\s*(\d{1,2})\b', goal.user_intent.text)
        if pin:
            meta.setdefault('hardware_context', {})['button'] = {'pin': int(pin[1]), 'source': 'operator_specification',
                                                               'physical_verified': False, 'pullup': 'requires_official_api'}
            engine.projects.save(meta)
        profile = BoardProfile.model_validate(meta['board'])
        profile = await engine.toolchains.select(profile)
        await engine.toolchains.ensure()
        if not engine.projects.inspect(meta['project_id']).get('src/main.cpp'):
            # Neutral SDK entrypoint, not a dedicated feature recipe.
            engine.projects.write(meta['project_id'], 'src/main.cpp', '#include <Arduino.h>\nvoid setup() { Serial.begin(115200); }\nvoid loop() {}\n')
            engine.projects.checkpoint(meta)
        revision = PlanRevision.model_validate(goal.plan_revision or {})
        await engine.progress(goal, 'project.inspect')
        if goal.desired_effect == 'build':
            result = {'build': await engine.builder.build(meta), 'artifacts': [], 'plan_revision': revision.model_dump()}
        else:
            result = await engine.engineering.evolve(meta, goal.user_intent, revision=revision,
                progress=lambda phase, details: engine.progress(goal, phase, details))
        goal.plan_revision = result['plan_revision']
        goal.artifacts = result['artifacts']
        goal.sources = meta.get('sources', [])
        goal.evidence.append({'source': 'compiler', **result['build']})
        artifacts = getattr(getattr(engine.services, 'computer', None), 'artifacts', None)
        if artifacts:
            for path in goal.artifacts:
                artifacts.register(path, source_turn_id=goal.turn_id, exists_state='verified',
                                   source_type='modified', source_tool='hardware_code_patch')
            if hasattr(artifacts, 'save'):
                artifacts.save()
        return await engine.complete(goal, 'Atualizei e compilei o mesmo projeto, preservando os recursos anteriores nas verificações de código. Nenhum firmware foi gravado; o funcionamento físico ainda não foi confirmado.', physical=False)

    async def reference_info(self, text):
        meta = await self.resolve(text)
        profile = await technical_profile({'board': meta['board']}, self.engine.research,
                                          getattr(self.engine.services, 'llm', None), reference=True)
        return profile_reply(profile, text), profile
