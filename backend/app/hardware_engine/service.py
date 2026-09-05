"""Hardware goals integrate with the existing tasks, memory and presentation."""
import asyncio
from collections import deque
import json
import logging
import os
from pathlib import Path
import re
import time

from app.core.turn import get_current_turn_id
from app.events import EventType
from app.intelligence.models import AutonomousTaskSpec, MemoryKind, MemoryWrite
from app.open_loops.models import OpenLoopCreate, OpenLoopState, ResolutionEvidence
from app.tools.redaction import redact_secrets
from app.usb.hardware import plain
from app.web_research.models import ResearchRequest
from app.web_research.service import WebResearchService, research_reply
from app.web_research.planner import natural_research_request
from .adapters import FAMILIES, PROFILES, identify_descriptor
from .build import BuildEngine
from .code import generate, resolve_led
from .discovery import HardwareDiscovery
from .execution import RecipeExecutor
from .flash import FlashEngine
from .identification import identify
from .models import BoardProfile, HardwareError, HardwareGoal, now
from .planner import understand, plan
from .projects import ProjectStore
from .serial import SerialRuntime
from .toolchains import Toolchains
from .engineering import CodeEngineering
from .project_workflow import ProjectWorkflow
from .technical_profile import technical_profile, profile_reply
from .replanning import PlanRevision, reconcile_target


logger = logging.getLogger('kazumi.hardware')
BLOCKERS = {
    'DEVICE_NOT_FOUND': 'Não encontrei nenhum dispositivo compatível conectado agora na USB/serial. Quando ele aparecer, consigo identificar e continuar.',
    'AMBIGUOUS_DEVICE': 'Há mais de um dispositivo possível. Preciso de uma distinção física entre eles para não atuar na placa errada.',
    'DISCOVERY_ERROR': 'Não consegui verificar a presença do dispositivo agora.',
    'SIMULATED_HARDWARE': 'SIMULATED: os dados disponíveis não comprovam hardware físico.',
    'BOARD_UNKNOWN': 'Identifiquei a interface ou o chip, mas ainda não comprovei a placa exata. Não vou escolher um pinout ou gravar firmware por suposição.',
    'LED_PIN_OR_DRIVER_UNVERIFIED': 'Ainda não comprovei o pino e o tipo de LED desta placa. Não acionei GPIO nem gravei firmware.',
    'BUTTON_WIRING_UNVERIFIED': 'O projeto foi preservado, mas falta comprovar o botão físico e sua ligação. Não vou inventar essa conexão.',
    'HARDWARE_RECIPE_UNAVAILABLE': 'Este dispositivo precisa de um adaptador de código e verificação que ainda não está disponível. O projeto e as fontes foram preservados.',
    'HARDWARE_AUTONOMY_DISABLED': 'A execução de hardware está desativada. A descoberta continua disponível; o modo FULL pode ser habilitado na área de Hardware.',
    'BUILD_ERROR': 'O build não passou. Registrei os diagnósticos e preservei o projeto para continuar; nenhum firmware foi gravado.',
    'DEVICE_DISCONNECTED': 'O dispositivo não voltou a aparecer. Não posso confirmar que o firmware funcionou.',
    'VERIFY_ERROR': 'Não obtive a verificação esperada do dispositivo. Não posso afirmar que o LED ou os pinos funcionaram.',
    'SERIAL_PROTOCOL_UNAVAILABLE': 'Não recebi uma resposta do protocolo compatível. Abrir a porta serial não prova que o dispositivo funcionou.',
    'BACKUP_RECOVERY_ADAPTER_UNAVAILABLE': 'O build está disponível, mas este adaptador ainda não possui backup e recuperação seguros para gravar a placa.',
    'PROJECT_NOT_FOUND': 'Não encontrei um projeto de hardware anterior para retomar.',
    'COMPONENT_EVIDENCE_REQUIRED': 'Preservei o projeto. Falta identificar o componente ou informar sua ligação/pino; não vou inventar hardware conectado.',
    'AMBIGUOUS_PROJECT': 'Encontrei mais de um projeto possível e o contexto não distingue qual deve ser alterado.',
    'PROJECT_TARGET_MISMATCH': 'O dispositivo mencionado não corresponde ao alvo deste projeto. Não alterei nem gravei firmware para uma placa diferente.',
    'ENGINEERING_PROPOSAL_INVALID': 'O modelo local não produziu uma proposta de código válida nesta tentativa. O projeto foi preservado.',
    'CODE_REVIEW_FAILED': 'A revisão encontrou uma alteração incompleta ou que não preservava os recursos existentes. Não apliquei a proposta.',
    'ENGINEERING_BLOCKED': 'A alteração precisa de uma API, biblioteca ou informação técnica ainda não comprovada. Preservei o projeto e o bloqueio para continuar.',
}


def is_research_request(text):
    value = plain(text)
    return bool(re.search(r'\b(?:pesquisa|pesquise|procura|procure|busca|busque|consulte)\b', value)
                and re.search(r'\b(?:documentacao|datasheet|biblioteca|versao|api|pinout|internet|web|repositorio)\b', value))


class HardwareEngineeringService:
    def __init__(self, services, state_root: Path, project_root=None):
        self.services, self.root = services, state_root
        self.config_path = state_root / 'settings.json'
        config = json.loads(self.config_path.read_text(encoding='utf8')) if self.config_path.exists() else {}
        project_root = Path(project_root or os.environ.get('KAZUMI_PROJECTS_ROOT') or
                            config.get('project_root') or (Path.home() / 'Kazumi-Projects'))
        self.full = config.get('full') is True
        self.research = WebResearchService(state_root / 'research-cache', provider=getattr(services, 'llm', None))
        self.research.catalog = [{'url': p.docs_url, 'title': p.name} for _, p in PROFILES]
        self.discovery = HardwareDiscovery(services.usb, services.world_state)
        self.projects = ProjectStore(project_root, state_root)
        self.executor = RecipeExecutor(services.shell, state_root / 'toolchain', project_root, full=self.full)
        self.toolchains = Toolchains(self.executor, self.research)
        self.serial = SerialRuntime(self.discovery)
        self.builder = BuildEngine(self.projects, self.executor)
        self.engineering = CodeEngineering(self.projects, self.research, self.builder, getattr(services, 'llm', None))
        self.project_workflow = ProjectWorkflow(self)
        self.flasher = FlashEngine(self.discovery, self.serial, self.projects, self.executor)
        self.goals = {}
        self._lock = asyncio.Lock()
        self._active_tasks = set()
        self.recent_removed = deque(maxlen=16)
        self.planner_ms = 0.0
        self.stopping = False

    async def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / 'goals.json'
        if path.is_file():
            for row in json.loads(path.read_text(encoding='utf8'))[-30:]:
                goal = HardwareGoal.model_validate(row)
                if goal.state not in ('COMPLETED', 'BLOCKED', 'CANCELLED'):
                    goal.state = 'BLOCKED'
                    goal.errors.append('INTERRUPTED_REVALIDATION_REQUIRED')
                self.goals[goal.goal_id] = goal
        self.services.intelligence.tasks.register('hardware_goal', self._task_handler, risk='ELEVATED')
        capabilities = self.services.intelligence.capabilities
        for name in ('hardware.discover', 'hardware.identify', 'hardware.inspect', 'hardware.serial.list'):
            capabilities.register(name, 'Descoberta nativa com provenance e freshness.', self.discovery_health)
        for name in ('hardware.goal', 'hardware.project.create', 'hardware.project.resume', 'hardware.project.open',
                     'hardware.code.generate', 'hardware.code.patch', 'hardware.toolchain.detect', 'hardware.toolchain.select',
                     'hardware.toolchain.install', 'hardware.toolchain.build', 'hardware.flash', 'hardware.verify',
                     'hardware.serial.open', 'hardware.serial.read', 'hardware.serial.write', 'hardware.serial.monitor',
                     'hardware.serial.wait_for', 'hardware.serial.close', 'hardware.backup_flash', 'hardware.read_flash_info', 'hardware.reset'):
            capabilities.register(name, 'Receita local tipada; execução depende do dispositivo e do adaptador real.',
                                  lambda: {'state': 'AVAILABLE' if self.full else 'DISABLED'})
        for name in ('hardware.erase', 'hardware.enter_bootloader', 'hardware.recover'):
            capabilities.register(name, 'Operação depende de procedimento de recuperação específico; nunca apagar por tentativa.',
                                  lambda: {'state': 'BLOCKED', 'error_code': 'RECOVERY_ADAPTER_REQUIRED'})
        for name in ('web.search', 'web.fetch', 'web.extract', 'web.research', 'web.download_document',
                     'web.find_official_docs', 'web.find_datasheet', 'web.find_repository', 'web.find_library'):
            capabilities.register(name, 'Pesquisa pública opt-in; disponibilidade da Internet verificada por chamada.',
                                  lambda: {'state': 'AVAILABLE', 'details': {'network_checked': False, 'providers': [p.name for p in self.research.providers]}})
        await self.services.event_bus.subscribe(self._usb_event)
        self.persist()

    def discovery_health(self):
        state = str(getattr(self.services.usb, 'state', 'STOPPED'))
        return {'state': 'AVAILABLE' if state == 'ACTIVE' else 'DEGRADED', 'details': {'monitor': state}}

    def configure(self, full: bool):
        self.full = self.executor.full = full
        self.config_path.write_text(json.dumps({'full': full, 'project_root': str(self.projects.root),
                                               'source': 'operator_api', 'at': now()}), encoding='utf8')
        return {'full': full}

    def persist(self):
        rows = list(self.goals.values())[-30:]
        temporary = self.root / 'goals.tmp'
        temporary.write_text(json.dumps([g.model_dump() for g in rows], ensure_ascii=False), encoding='utf8')
        temporary.replace(self.root / 'goals.json')
        self.goals = {g.goal_id: g for g in rows}

    def status(self):
        active = self.projects.read(self.projects.active) if self.projects.active else None
        return {'full': self.full, 'project_root': str(self.projects.root), 'project': active,
                'goals': [g.model_dump() for g in list(self.goals.values())[-10:]],
                'discovery': self.discovery.last, 'serial': {'open_handles': len(self.serial.handles), 'last': self.serial.last},
                'research': self.research.last, 'toolchains': self.toolchains.detect(),
                'families': list(FAMILIES), 'max_repair_cycles': self.builder.MAX_REPAIR_CYCLES,
                'metrics': {'discovery_ms': self.discovery.overhead_ms, 'planner_ms': self.planner_ms}}

    async def handle(self, text):
        start = time.perf_counter()
        intent = understand(text, active_project=bool(self.projects.active))
        if intent is not None:
            intent.text = redact_secrets(intent.text)
        self.planner_ms = round((time.perf_counter()-start)*1000, 3)
        if intent is None:
            if natural_research_request(text):
                return await self.research.answer(text)
            return None
        if natural_research_request(text) and not re.search(r'\b(?:essa|esse|conect|aqui|placa|dispositivo)\w*\b', plain(text)):
            return await self.research.answer(text)
        if intent.effect == 'info' and re.search(r'\b(?:reference|referencia|simulated|simulado|perfil de teste)\b', plain(text)):
            response, _ = await self.project_workflow.reference_info(text)
            return response
        goal = HardwareGoal(user_intent=intent, desired_effect=intent.effect, plan=plan(intent), turn_id=get_current_turn_id())
        self.goals[goal.goal_id] = goal
        if intent.project_only:
            try:
                meta = await self.project_workflow.resolve(text)
                goal.target_project = meta['project_id']
                if not self.full and intent.effect != 'resume':
                    raise HardwareError('HARDWARE_AUTONOMY_DISABLED')
            except HardwareError as error:
                return await self.block(goal, error.code)
            if intent.effect == 'resume':
                return (await self.run(goal.goal_id))['response']
            goal.state = 'QUEUED'
            self.persist()
            task = await self.services.intelligence.tasks.create(AutonomousTaskSpec(
                title='Projeto: ' + intent.effect, objective='Evoluir os arquivos do mesmo projeto, pesquisar APIs e compilar sem gravar dispositivo.',
                source_turn=goal.turn_id, action='hardware_goal', parameters={'goal_id': goal.goal_id}, risk='ELEVATED',
                max_retries=0, timeout_seconds=1800), approved=self.full)
            goal.task_id = task.task_id
            self.persist()
            return 'Retomei o mesmo projeto. Iniciei a tarefa de alteração e build; isso não confirma nenhum dispositivo ou efeito físico.'
        if intent.effect in ('info', 'research', 'resume'):
            result = await self.run(goal.goal_id)
            return result['response']
        # First observation is synchronous: never tell an absent board user
        # that a build/flash was started. The task revalidates again on execution.
        try:
            device = await self._resolve_goal_device(goal)
            goal.target_device = device
            if not self.full:
                raise HardwareError('HARDWARE_AUTONOMY_DISABLED')
            identity = identify_descriptor(device)
            if not identity['board']:
                raise HardwareError('BOARD_UNKNOWN')
        except HardwareError as error:
            return await self.block(goal, error.code)
        goal.state = 'QUEUED'
        self.persist()
        task = await self.services.intelligence.tasks.create(AutonomousTaskSpec(
            title='Hardware: ' + intent.effect, objective='Executar receita e verificar efeito no dispositivo observado.',
            source_turn=goal.turn_id, action='hardware_goal', parameters={'goal_id': goal.goal_id}, risk='ELEVATED',
            max_retries=0, timeout_seconds=1800), approved=self.full)
        goal.task_id = task.task_id
        self.persist()
        return f"Identifiquei {device.get('name', 'a placa')} na USB. Iniciei a tarefa de hardware; você pode acompanhar o projeto e a verificação na área de Hardware."

    async def _resolve_goal_device(self, goal):
        if goal.user_intent.effect in ('resume', 'button', 'build') and self.projects.active:
            meta = self.projects.read(self.projects.active)
            goal.target_project = meta['project_id']
            if meta.get('device_id'):
                return await self.discovery.resolve(device_id=meta['device_id'], physical=True)
        return await self.discovery.resolve(goal.user_intent.target, physical=goal.desired_effect != 'info')

    async def _task_handler(self, params):
        if set(params) != {'goal_id'} or params['goal_id'] not in self.goals:
            raise HardwareError('INVALID_HARDWARE_TASK')
        goal = self.goals[params['goal_id']]
        if goal.state != 'QUEUED' or not self.full or self.stopping:
            raise HardwareError('HARDWARE_TASK_NOT_RUNNABLE')
        result = await self.run(goal.goal_id)
        if not result['success']:
            raise HardwareError(goal.errors[-1] if goal.errors else 'VERIFY_ERROR')
        return result

    async def run(self, goal_id):
        task = asyncio.current_task()
        self._active_tasks.add(task)
        try:
            async with self._lock:
                goal = self.goals[goal_id]
                goal.state = 'RUNNING'
                if goal.user_intent.project_only:
                    return await self.project_workflow.run(goal)
                await self.progress(goal, 'discover')
                device = await self._resolve_goal_device(goal)
                goal.target_device = device
                await self.progress(goal, 'identify')
                identity = await identify(device)  # No serial reset needed for information-only requests.
                identity.update(name=device.get('name'), observed_at=device.get('observed_at'))
                goal.evidence.append(identity)
                if goal.desired_effect == 'info':
                    profile = await technical_profile(identity, self.research, getattr(self.services, 'llm', None))
                    goal.evidence.append({'source': 'technical_profile', **profile.model_dump()})
                    goal.sources = profile.sources
                    return await self.complete(goal, profile_reply(profile, goal.user_intent.text), physical=False)
                if goal.desired_effect == 'resume':
                    if not self.projects.active:
                        raise HardwareError('PROJECT_NOT_FOUND')
                    goal.target_project = self.projects.active
                    return await self.complete(goal, 'Recuperei o mesmo projeto e revalidei a presença do dispositivo. O estado do firmware ainda precisa de verificação.', physical=False)
                await self.progress(goal, 'researching')
                # Only technical board/chip terms leave the host, no USB serial,
                # paths, MACs, IPs or conversation/memory dump.
                query = (identity['board'] or {}).get('name') or identity['chip'] or identity['family']
                researched = await self.research.research(ResearchRequest(query=query + ' hardware documentation', kind='official_docs'))
                goal.sources = researched.get('sources', [])
                if goal.desired_effect == 'research':
                    if not researched.get('success'):
                        raise HardwareError('RESEARCH_ERROR')
                    return await self.complete(goal, research_reply(researched), physical=False)
                if not self.full:
                    raise HardwareError('HARDWARE_AUTONOMY_DISABLED')
                if not identity['board']:
                    raise HardwareError('BOARD_UNKNOWN')
                profile = BoardProfile.model_validate(identity['board'])
                profile = await self.toolchains.select(profile)
                if goal.desired_effect in ('button', 'display', 'sensor', 'web_server', 'modify'):
                    meta = self.projects.read(self.projects.active) if self.projects.active else None
                    if meta and meta.get('device_id') == device['device_id']:
                        revision = PlanRevision.model_validate(meta.get('plan_revision', {}))
                        reconcile_target(self.projects, meta, identity, revision)
                        goal.plan_revision = revision.model_dump()
                    else:
                        meta = self.projects.create(profile, device)
                    technical = await technical_profile(identity, self.research, getattr(self.services, 'llm', None))
                    for component, field in (('display', 'display'), ('button', 'button'), ('sensor', 'sensors')):
                        fact = technical.facts.get(field)
                        if fact:
                            meta.setdefault('hardware_context', {})[component] = {'source': 'official_document',
                                'description': fact.value, 'support': fact.support, 'url': fact.url, 'physical_verified': False}
                    self.projects.save(meta)
                    goal.target_project = meta['project_id']
                    return await self.project_workflow.run(goal)
                profile = await resolve_led(profile, self.research)
                if goal.desired_effect in ('led_on', 'led_off', 'led_blink'):
                    try:
                        await self.serial.request(device['device_id'], board_id=profile.board_id)
                    except HardwareError:
                        await self.serial.close_device(device['device_id'])
                    else:
                        goal.plan = plan(goal.user_intent, runtime_capable=True)
                        await self.progress(goal, 'runtime_control')
                        evidence = await self.serial.control(device['device_id'], goal.user_intent, profile)
                        goal.evidence.append(evidence)
                        if not evidence['effect_verified']:
                            raise HardwareError('VERIFY_ERROR')
                        return await self.complete(goal, 'O firmware confirmou o estado solicitado por leitura do GPIO. Essa é uma confirmação elétrica/serial, não uma observação visual do LED.', physical=True)
                await self.progress(goal, 'project')
                meta = self.projects.read(self.projects.active) if self.projects.active else None
                if meta and (meta['board']['board_id'] != profile.board_id or meta.get('device_id') != device['device_id']):
                    meta = None  # Replan instead of applying another board's configuration.
                if meta is None:
                    meta = self.projects.create(profile, device)
                goal.target_project = meta['project_id']
                goal.artifacts = [str(self.projects.path(meta['project_id']))]
                source = generate(profile, goal.user_intent)
                self.projects.write(meta['project_id'], 'src/main.cpp', source)
                self.projects.checkpoint(meta)
                artifacts = getattr(getattr(self.services, 'computer', None), 'artifacts', None)
                if artifacts:
                    for relative in ('src/main.cpp', 'platformio.ini'):
                        artifacts.register(str(self.projects.path(meta['project_id']) / relative),
                                           source_turn_id=goal.turn_id, exists_state='verified',
                                           source_type='created', source_tool='hardware_project')
                from .workspace import open_workspace
                goal.evidence.append({'source': 'desktop_operator', **await open_workspace(
                    self.projects, meta['project_id'], self.services.desktop)})
                await self.progress(goal, 'toolchain')
                await self.toolchains.ensure()
                result = await self.builder.build(meta, lambda phase, details: self.progress(goal, phase, details))
                goal.evidence.append({'source': 'compiler', **result})
                if goal.desired_effect in ('project', 'build'):
                    return await self.complete(goal, 'O projeto foi criado e compilado. Nenhum firmware foi gravado e nenhum efeito físico foi confirmado.', physical=False)
                await self.progress(goal, 'flashing')
                result = await self.flasher.flash(meta)
                goal.evidence.append({'source': 'flash_tool', **result})
                await self.progress(goal, 'verifying')
                evidence = await self.serial.control(meta['device_id'], goal.user_intent, profile)
                goal.evidence.append(evidence)
                if not evidence['effect_verified']:
                    raise HardwareError('VERIFY_ERROR')
                return await self.complete(goal, 'Compilei e gravei o firmware. O dispositivo voltou e confirmou o estado solicitado por leitura do GPIO; não foi uma observação visual do LED.', physical=True)
        except asyncio.CancelledError:
            goal = self.goals[goal_id]
            goal.state, goal.finished_at = 'CANCELLED', now()
            self.persist()
            raise
        except Exception as error:
            code = error.code if isinstance(error, HardwareError) else 'HARDWARE_ENGINE_ERROR'
            await self.block(self.goals[goal_id], code)
            return {'success': False, 'effect_verified': False, 'response': self.goals[goal_id].response}
        finally:
            self._active_tasks.discard(task)

    async def progress(self, goal, phase, details=None):
        goal.steps.append({'phase': phase, 'at': now(), **(details or {})})
        goal.steps = goal.steps[-40:]
        self.persist()
        logger.info('hardware_phase goal=%s phase=%s', goal.goal_id, phase)
        self.services.world_state.update_verified('hardware_activity', {'goal_id': goal.goal_id, 'phase': phase,
            'project': goal.target_project}, source='tool:hardware_engine', confidence=1, ttl_seconds=60)

    async def block(self, goal, code):
        goal.state, goal.finished_at = 'BLOCKED', now()
        goal.errors.append(code)
        goal.simulated = code == 'SIMULATED_HARDWARE'
        goal.response = BLOCKERS.get(code, 'Não consegui concluir esta etapa com segurança. Preservei as evidências e o projeto; nenhum sucesso físico foi presumido.')
        if code == 'DEVICE_NOT_FOUND' and goal.user_intent.target:
            goal.response = f'Não encontrei nenhum {goal.user_intent.target} conectado agora na USB/serial. Quando ele aparecer, consigo identificar e continuar.'
        self.persist()
        created = await self.services.intelligence.open_loops.create(OpenLoopCreate(
            title='Hardware: ' + goal.desired_effect, state=OpenLoopState.BLOCKED,
            source_turn=goal.turn_id, related_project=goal.target_project,
            related_task=[goal.task_id] if goal.task_id else [],
            waiting_for={'kind': code}, context={'last_confirmed_state': code},
            provenance={'source': 'tool:hardware_engine', 'goal_id': goal.goal_id}), actor='hardware_engine')
        if isinstance(created, tuple) and created:
            goal.loop_id = created[0].id
            self.persist()
        if goal.target_project:
            meta = self.projects.read(goal.target_project)
            goal.plan_revision = meta.get('plan_revision', goal.plan_revision)
            meta['pending_goals'] = list(dict.fromkeys(meta.get('pending_goals', []) + [goal.user_intent.text]))[-30:]
            meta['blocker'] = code
            self.projects.save(meta)
        return goal.response

    async def complete(self, goal, response, *, physical):
        goal.state, goal.finished_at, goal.response = 'COMPLETED', now(), response
        self.persist()
        if goal.target_project:
            meta = self.projects.read(goal.target_project)
            await self.services.intelligence.memory.write(MemoryWrite(kind=MemoryKind.PROJECT,
                content=response, project=goal.target_project, source='tool:hardware_engine', relevance=.9,
                metadata={'source_hashes': meta.get('source_hashes'), 'features': meta.get('completed_features', []),
                          'build': meta.get('build'), 'pending_goals': meta.get('pending_goals', [])},
                provenance={'goal_id': goal.goal_id, 'artifacts': goal.artifacts, 'sources': goal.sources}))
        if goal.loop_id:
            await self.services.intelligence.open_loops.resolve(goal.loop_id, ResolutionEvidence(kind='hardware_goal_verified',
                source='tool:hardware_engine', verified=True, reference_id=goal.goal_id,
                detail={'physical_effect_verified': physical}), actor='hardware_engine')
        # Task completion means its explicit objective (including info/build),
        # not physical illumination. Keep that distinction in the public result.
        return {'success': True, 'effect_verified': True, 'physical_effect_verified': physical, 'response': response,
                'goal_id': goal.goal_id, 'source': 'hardware_engine'}

    async def _usb_event(self, event):
        if event.type == EventType.USB_DEVICE_DISCONNECTED:
            device = event.payload.get('device', {})
            if device.get('device_id'):
                self.recent_removed.append({'device_id': device['device_id'], 'observed_at': now(), 'source': 'usb_monitor'})
                await self.serial.close_device(device['device_id'])
        if event.type in (EventType.USB_DEVICE_CONNECTED, EventType.USB_DEVICE_DISCONNECTED):
            # Existing Proactive Presence already subscribes to USB events.
            self.services.world_state.update_verified('recent_hardware_events', list(self.recent_removed),
                source='tool:hardware_engine', confidence=1, ttl_seconds=300)
        if event.type == EventType.USB_DEVICE_CONNECTED and self.full and not self.stopping:
            for goal in list(self.goals.values())[-10:]:
                if (goal.state == 'BLOCKED' and goal.errors and goal.errors[-1] in ('DEVICE_NOT_FOUND', 'DEVICE_DISCONNECTED', 'COMPONENT_EVIDENCE_REQUIRED')
                        and not goal.constraints.get('reconnect_resume_attempted') and not goal.simulated):
                    goal.constraints['reconnect_resume_attempted'] = True
                    task = asyncio.create_task(self._resume_observed_goal(goal.goal_id))
                    self._active_tasks.add(task)
                    task.add_done_callback(self._active_tasks.discard)

    async def _resume_observed_goal(self, goal_id):
        goal = self.goals.get(goal_id)
        if goal is None or self.stopping:
            return
        try:
            await self._resolve_goal_device(goal)  # Fresh native enumeration, not the event/user text.
            if goal.loop_id:
                await self.services.intelligence.open_loops.transition(goal.loop_id, OpenLoopState.ACTIVE,
                    reason='device_presence_revalidated', actor='hardware_engine')
            goal.state = 'QUEUED'
            self.persist()
            task = await self.services.intelligence.tasks.create(AutonomousTaskSpec(title='Hardware: retomar ' + goal.desired_effect,
                objective='Reavaliar objetivo bloqueado após descoberta real, sem repetir flash por estado antigo.',
                action='hardware_goal', parameters={'goal_id': goal_id}, risk='ELEVATED', max_retries=0, timeout_seconds=1800), approved=self.full)
            goal.task_id = task.task_id
            self.persist()
        except HardwareError:
            self.persist()
        except Exception:
            logger.warning('hardware_resume_unavailable goal=%s', goal_id)

    async def stop(self):
        self.stopping = True
        await self.services.event_bus.unsubscribe(self._usb_event)
        for task in tuple(self._active_tasks):
            task.cancel()
        await self.serial.close()
        await self.executor.close()
        await self.research.close()
        await asyncio.gather(*tuple(self._active_tasks), return_exceptions=True)
        self.persist()
