from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, TYPE_CHECKING

from app.llm import LLMMessage, LLMProvider
from app.tools.grounding import (
    READ_ONLY_RISKS,
    GroundingLedger,
    GroundingViolation,
    absence_claims_without_evidence,
    fabricated_value_claims,
    unverified_effect_claims,
    VerificationStatus,
)
from app.tools.models import RiskLevel, ToolResult
from app.tools.redaction import redact_secrets
from app.tools.shell_models import ShellErrorCode

if TYPE_CHECKING:
    from app.agent.models import AgentLoopRuntime


logger = logging.getLogger("kazumi.grounding.loop")


class ToolAgentLoop:
    """Bounded native tool loop with optional persistent Agent Run controls."""

    def __init__(self, llm: LLMProvider, registry, max_shell_calls: int = 10) -> None:
        self.llm = llm
        self.registry = registry
        self.max_shell_calls = max_shell_calls

    _OPERATOR_PATTERN = re.compile(
        r"(?i)\b(abra|abre|abrir|feche|fecha|fechar|minimize|minimiza|maximize|restaura|traz|foco|focar|"
        "escreva|escreve|digite|digita|clique|clica|salve|salva|navegue|acesa|acessa)\\b"
    )
    _MONITOR_REQUEST_PATTERN = re.compile(
        r"(?i)\b(monitora|monitore|monitorar|monitoramento|acompanha|acompanhe|acompanhar|"
        r"avisa|avise|avisar|fica\s+de\s+olho)\b|"
        r"\bquando\b.{0,100}\b(?:mudar|ficar|chegar|cair|subir|terminar|concluir)\b"
    )

    @classmethod
    def _operator_directive(cls, messages: list[LLMMessage]) -> str | None:
        """Targeted steering when the request asks to operate desktop apps."""
        user_text = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        if not user_text or not cls._OPERATOR_PATTERN.search(user_text):
            return None
        return (
            "LOCAL OPERATOR MODE: o pedido opera aplicativos/janelas do Windows. Regras rígidas: "
            "(1) Abra com `desktop_open_application` UMA vez só; se `desktop_windows` já mostra a janela aberta, "
            "NÃO chame launch/find de novo. "
            "(2) Para ESCREVER texto: chame `ui_inspect` (query=<app>) para achar o controle, depois UMA chamada "
            "`ui_set_text` com control_type='Edit' e value=<texto>. NÃO use ui_click para digitar; "
            "ui_click é só para botões/menus. "
            "(3) Fechar/minimizar/restaurar/focar: `desktop_close`/`desktop_minimize`/`desktop_restore`/"
            "`desktop_focus` com query=<app>. "
            "(4) Cada tool retorna effect_verified: concluída SOMENTE com effect_verified=true; senão reporte o "
            "error_code exato. NUNCA afirme que executou sem ter chamado a tool neste turno."
        )

    async def run(self, messages: list[LLMMessage], runtime: AgentLoopRuntime | None = None, *, turn_id: str | None = None) -> str:
        from app.tools.registry import classify_domain

        working = list(messages)
        user_text = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        domain = classify_domain(user_text or "")
        monitor_request = bool(self._MONITOR_REQUEST_PATTERN.search(user_text or ""))
        try:
            schemas = self.registry.llm_tools(domain)
        except TypeError:
            # Registries alternativos (testes/fakes) podem expor llm_tools() sem domínio.
            schemas = self.registry.llm_tools()
        ledger = GroundingLedger(turn_id=turn_id)
        hardware_engine = getattr(self.registry, 'hardware_engine', None)
        if hardware_engine is not None:
            hardware_response = await hardware_engine.handle(user_text)
            if hardware_response is not None:
                return hardware_response
        from app.usb.hardware import hardware_request, presence_reply
        hardware_intent = hardware_request(user_text)
        if hardware_intent is not None:
            # The real discovery is mandatory; the model cannot skip it or
            # replace it with a shell echo/network result. No physical action
            # adapter exists in this baseline, so stop at the observed boundary.
            if runtime and (runtime.cancelled() or runtime.expired()):
                raise asyncio.CancelledError
            result = await self._execute("hardware_discover", hardware_intent.model_dump())
            observation = ledger.record(
                tool_call_id=ledger.new_call_id(), tool_name=result.tool,
                result_data=result.data, risk_level="READ_ONLY",
            )
            if runtime:
                await runtime.record_step(
                    result.tool, hardware_intent.model_dump(), {"risk_level": "READ_ONLY"},
                    result.model_dump(mode="json"), observation=observation,
                )
                runtime.stop_reason = (str(result.data.get("error_code") or "HARDWARE_DISCOVERY_UNAVAILABLE")
                                       if result.data.get("status") != "OBSERVED" else None)
            return presence_reply(result.data)
        shell_calls = 0
        execution_results: list[ToolResult] = []
        read_only_retries = 0
        textual_tool_retries = 0
        effect_claim_retries = 0
        verify_prompts = 0
        force_final = False
        consecutive_failures = 0
        remote_observation_retries = 0
        local_backend_retries = 0
        repeats: dict[tuple[str, str], int] = {}
        no_tool_nudges = 0
        monitor_nudges = 0
        max_rounds = (runtime.max_steps + 2) if runtime else (self.max_shell_calls + 6)
        operator_directive = self._operator_directive(messages)
        if operator_directive:
            working.append(LLMMessage(role="system", content=operator_directive))
        if monitor_request:
            working.append(LLMMessage(
                role="system",
                content=(
                    "MONITORING REQUIRED: este pedido exige acompanhamento futuro real. Chame `monitor_create` "
                    "com TODOS os campos planos: objective, probe_tool READ_ONLY, probe_params, condition_path, "
                    "condition_operator, target_value, interval_seconds e duration_seconds. Para NetworkWatch, "
                    "use probe_tool=get_network_status; para detectar qualquer atualização, use "
                    "condition_path=snapshot.timestamp e condition_operator=CHANGED. "
                    "Não diga 'vou monitorar/acompanhar/verificar depois' sem success=true e monitor_id "
                    "retornados por monitor_create neste turno. Se a criação falhar, informe a falha explicitamente."
                ),
            ))
        if runtime:
            remote_requirement = (
                f" REMOTE_TARGET={runtime.required_remote_host}; REGISTERED_ADDRESS={runtime.required_remote_address}; "
                "use that address for local prechecks and the logical host for remote_shell."
                if runtime.required_remote_host else ""
            )
            local_requirement = (
                f" LOCAL_BACKEND_TARGET=127.0.0.1:{runtime.local_backend_port}; PROJECT_ROOT={runtime.local_backend_root}; "
                "inspect local port/health/process/logs and do not query homelab hosts unless evidence points there."
                if runtime.required_local_backend else ""
            )
            working.append(LLMMessage(
                role="system",
                content=(
                    f"AGENT_RUN_ID={runtime.run.id}; GOAL={runtime.run.goal}; "
                    "execute OBSERVE, DIAGNOSE, PLAN, ACT, VERIFY, REPORT conforme necessário. "
                    "Inspecione antes de alterar, escolha a menor ação, e valide qualquer mudança. "
                    "Não exponha chain-of-thought; emita apenas calls de tools e conclusões operacionais curtas."
                    + remote_requirement
                    + local_requirement
                ),
            ))

        for _ in range(max_rounds):
            if runtime:
                if runtime.cancelled():
                    raise asyncio.CancelledError
                if runtime.expired() and not force_final:
                    runtime.stop_reason = "AGENT_RUNTIME_LIMIT"
                    working.append(LLMMessage(role="system", content="RUNTIME LIMIT: pare de usar tools e reporte fatos já observados."))
                    force_final = True
                if runtime.run.reasoning_steps >= runtime.max_steps and not force_final:
                    runtime.stop_reason = "AGENT_STEP_LIMIT"
                    working.append(LLMMessage(role="system", content="STEP LIMIT: pare de usar tools e reporte fatos já observados."))
                    force_final = True
                runtime.run.reasoning_steps += 1

            response = await self.llm.complete(working, None if force_final else schemas)
            for index, tool_call in enumerate(response.tool_calls):
                if not tool_call.tool_call_id:
                    tool_call.tool_call_id = f"call_r{index}_{GroundingLedger.new_call_id()}"
            working.append(response.as_message())
            if not response.tool_calls:
                content = response.content.strip()
                if not content:
                    raise RuntimeError("LLM completed the tool loop without a response")
                monitor_created = any(
                    result.tool == "monitor_create"
                    and result.ok
                    and result.data.get("success") is True
                    and bool((result.data.get("monitor") or {}).get("monitor_id"))
                    for result in execution_results
                )
                if monitor_request and not monitor_created and not force_final and monitor_nudges < 2:
                    monitor_nudges += 1
                    working.append(LLMMessage(
                        role="system",
                        content=(
                            "MONITOR JOB MISSING: ainda não existe MonitorJob real neste turno. "
                            "Chame monitor_create agora. Se uma tentativa falhou, corrija somente argumentos "
                            "estruturais com base no erro; caso não seja possível, pare e relate que o "
                            "monitoramento não foi criado."
                        ),
                    ))
                    continue
                if monitor_request and not monitor_created:
                    return self._monitor_creation_failure(execution_results)
                if monitor_created:
                    return self._monitor_confirmation(execution_results)
                if (
                    schemas
                    and not execution_results
                    and not force_final
                    and no_tool_nudges < 2
                    and len(content) > 30
                ):
                    # kazumi-full §26: tarefa multi-step exige tools nativas; prosa
                    # sem nenhuma observação ainda = lembrete determinístico.
                    no_tool_nudges += 1
                    working.append(LLMMessage(
                        role="system",
                        content=(
                            "TOOL CALL REQUIRED: esta tarefa só é concluída chamando as tools nativas "
                            "disponíveis (ex.: desktop_open_application → ui_inspect → ui_set_text → "
                            "ui_send_keys {ctrl+s} → filesystem/verificação). Responda SOMENTE com a "
                            "próxima tool call, sem texto."
                        ),
                    ))
                    continue
                if self._looks_like_textual_tool_call(content):
                    if not force_final and textual_tool_retries < 2:
                        textual_tool_retries += 1
                        working.append(LLMMessage(
                            role="system",
                            content=(
                                "TEXTUAL TOOL CALL REJECTED: texto, XML, JSON ou Markdown não executa tools. "
                                "Use agora o schema nativo disponível. Para remote_shell, `command` contém somente o comando que roda dentro do host "
                                "(ex.: `uptime`); nunca escreva `ssh`, opções de conexão, IP, user ou StrictHostKeyChecking."
                            ),
                        ))
                        continue
                    if runtime:
                        runtime.stop_reason = "TEXTUAL_TOOL_CALL_REJECTED"
                    return "Não executei a pseudo-tool call textual; a investigação foi interrompida com segurança."
                if runtime and runtime.needs_verification and not force_final:
                    if verify_prompts < 2:
                        verify_prompts += 1
                        working.append(LLMMessage(
                            role="system",
                            content=(
                                "VERIFY REQUIRED: uma alteração foi executada. Antes do relatório final, chame uma tool READ_ONLY "
                                "que confirme o estado resultante. Não repita a ação de mudança. "
                                "Se a verificação não for tecnicamente possível, relate a ação como executada e NÃO confirmada."
                            ),
                        ))
                        continue
                    runtime.unverified_action = True
                if (
                    not force_final
                    and effect_claim_retries < 1
                    and self._asserts_effect_without_observation(content, ledger)
                ):
                    effect_claim_retries += 1
                    pending = ledger.pending_mutations()
                    guidance = (
                        "chame agora uma tool READ_ONLY que confirme o estado resultante"
                        if pending or runtime and runtime.needs_verification
                        else "execute a tool adequada e depois verifique o resultado real antes de afirmar qualquer efeito"
                    )
                    working.append(LLMMessage(
                        role="system",
                        content=(
                            "EFFECT CLAIM WITHOUT OBSERVATION: o rascunho afirma alteração/effect concluído sem suporte em "
                            f"nenhuma observação verificada deste turno ({len(pending)} mutação(ões) pendente(s) de confirmação). "
                            f"{guidance}. Nunca alegue sucesso sem evidência literal do resultado."
                        ),
                    ))
                    continue
                if runtime and runtime.required_remote_host and not runtime.remote_attempted and not force_final:
                    if remote_observation_retries < 2:
                        remote_observation_retries += 1
                        working.append(LLMMessage(
                            role="system",
                            content=(
                                f"REMOTE OBSERVATION REQUIRED: o objetivo exige investigar {runtime.required_remote_host}. "
                                f"Após o precheck local de {runtime.required_remote_address}, chame remote_shell com host="
                                f"{runtime.required_remote_host} e um comando read-only como hostname/uptime. Não finalize antes da tentativa SSH."
                            ),
                        ))
                        continue
                    runtime.stop_reason = "REMOTE_OBSERVATION_NOT_PERFORMED"
                    return "A investigação remota foi interrompida porque o modelo não realizou a tentativa SSH obrigatória."
                if runtime and runtime.required_local_backend and runtime.local_backend_observations < 2 and not force_final:
                    if local_backend_retries < 2:
                        local_backend_retries += 1
                        working.append(LLMMessage(
                            role="system",
                            content=(
                                f"LOCAL BACKEND OBSERVATION REQUIRED: colete pelo menos porta/health e processo/log local para "
                                f"127.0.0.1:{runtime.local_backend_port} no projeto {runtime.local_backend_root}. "
                                "Use system_shell READ_ONLY e não consulte gateway/Proxmox sem evidência."
                            ),
                        ))
                        continue
                    runtime.stop_reason = "LOCAL_BACKEND_OBSERVATION_INCOMPLETE"
                    return "A investigação do backend foi interrompida sem evidência local suficiente."
                if (
                    not force_final
                    and shell_calls < self.max_shell_calls
                    and read_only_retries < 2
                    and self._needs_read_only_retry(content, execution_results)
                ):
                    read_only_retries += 1
                    working.append(LLMMessage(
                        role="system",
                        content=(
                            "READ_ONLY RETRY REQUIRED: a inspeção anterior falhou, mas existe fallback de leitura. "
                            "Não pare e não peça elevação. Para portas locais use netstat/findstr e correlacione PID; "
                            "para interfaces locais use ipconfig /all. Depois responda somente com resultados reais."
                        ),
                    ))
                    continue
                if runtime and runtime.needs_verification:
                    runtime.unverified_action = True
                grounded = await self._grounded_final(working, content, execution_results, ledger)
                from app.operator.monitoring import enforce_monitor_promise

                return enforce_monitor_promise(grounded, job_created=monitor_created)

            stop_batch = False
            for call in response.tool_calls:
                name = call.function.name
                arguments = dict(call.function.arguments)
                post_tool_instruction: str | None = None
                preflight = self.registry.preflight(name, arguments)
                risk_name = str(preflight.get("risk_level", RiskLevel.READ_ONLY.value))
                try:
                    risk = RiskLevel(risk_name)
                except ValueError:
                    risk = RiskLevel.ELEVATED

                if runtime and runtime.cancelled():
                    raise asyncio.CancelledError
                remote_address = str(preflight.get("address") or "")
                required_address = runtime.required_remote_address if runtime else None
                has_required_precheck = bool(required_address) and any(
                    item.tool == "system_shell" and required_address in str(item.data.get("command", ""))
                    for item in execution_results
                )
                remote_prechecked = not remote_address or any(
                    item.tool == "system_shell" and remote_address in str(item.data.get("command", ""))
                    for item in execution_results
                )
                if runtime and runtime.required_local_backend and name == "remote_shell":
                    result = self._blocked(
                        name, RiskLevel.ELEVATED, "REMOTE_OUT_OF_SCOPE",
                        "O objetivo atual está vinculado ao backend local da KAZUMI; SSH remoto exige nova evidência local que justifique o alvo.",
                    )
                elif (
                    runtime and runtime.required_local_backend and name == "system_shell"
                    and re.search(r"\b192\.168\.1\.\d+\b", str(arguments.get("command", "")))
                ):
                    result = self._blocked(
                        name, RiskLevel.READ_ONLY, "LOCAL_BACKEND_TARGET_REQUIRED",
                        f"Inspecione primeiro 127.0.0.1:{runtime.local_backend_port} e o projeto local, não um host do homelab.",
                    )
                    post_tool_instruction = f"Use system_shell READ_ONLY no backend local 127.0.0.1:{runtime.local_backend_port}."
                elif (
                    runtime and name == "system_shell" and required_address
                    and not has_required_precheck and required_address not in str(arguments.get("command", ""))
                ):
                    result = self._blocked(
                        name, RiskLevel.READ_ONLY, "REMOTE_REGISTERED_ADDRESS_REQUIRED",
                        f"Use o endereço cadastrado {required_address} no precheck local; não dependa de DNS para o alias remoto.",
                    )
                    post_tool_instruction = f"Refaça o precheck READ_ONLY usando exatamente o endereço cadastrado {required_address}."
                elif (
                    runtime and name == "remote_shell" and runtime.required_remote_host
                    and preflight.get("host") != runtime.required_remote_host
                ):
                    result = self._blocked(
                        name, RiskLevel.ELEVATED, "REMOTE_TARGET_MISMATCH",
                        f"Este run está vinculado ao host {runtime.required_remote_host}; outro alvo foi bloqueado.",
                    )
                elif runtime and name == "remote_shell" and not remote_prechecked:
                    result = self._blocked(
                        name, RiskLevel.READ_ONLY, "REMOTE_NETWORK_PRECHECK_REQUIRED",
                        f"Antes do SSH, teste do host local a conectividade do endereço cadastrado {remote_address} com system_shell (ping e, se necessário, porta 22). Depois tente remote_shell.",
                    )
                    post_tool_instruction = (
                        f"REMOTE PRECHECK REQUIRED for {preflight.get('host')}: use system_shell READ_ONLY para testar {remote_address}. "
                        "Falha de ping não prova host offline; prossiga com teste TCP/SSH quando apropriado."
                    )
                elif runtime and runtime.run.tool_calls >= runtime.max_tool_calls:
                    result = self._blocked(name, risk, "AGENT_TOOL_CALL_LIMIT", f"Limite de {runtime.max_tool_calls} tool calls atingido.")
                    runtime.stop_reason = "AGENT_TOOL_CALL_LIMIT"
                    force_final = True
                elif name == "system_shell" and shell_calls >= self.max_shell_calls:
                    result = self._blocked(name, risk, ShellErrorCode.SHELL_CALL_LIMIT.value, f"Limite de {self.max_shell_calls} chamadas de shell local atingido.")
                    if runtime:
                        runtime.stop_reason = ShellErrorCode.SHELL_CALL_LIMIT.value
                    force_final = True
                elif runtime and runtime.read_only and risk not in (RiskLevel.READ_ONLY, RiskLevel.LOW_RISK):
                    # Semântica corrigida: read_only bloqueia mutações reais
                    # (ELEVATED+), mas ações LOW_RISK autorizadas pela política —
                    # como desktop_launch verificado — seguem o fluxo normal de
                    # locks, grounding e approval.
                    result = self._blocked(name, risk, "AGENT_READ_ONLY", "O Agent Loop está em modo somente leitura; alteração bloqueada.")
                else:
                    if name == "system_shell":
                        shell_calls += 1
                    resource = str(preflight.get("resource_key") or name)
                    locked = False
                    mutable = risk != RiskLevel.READ_ONLY
                    if runtime:
                        if mutable:
                            await runtime.transition(self._state("PLAN"))
                            await runtime.transition(self._state("ACT"))
                            locked = await runtime.acquire_resource(resource)
                            if not locked:
                                result = self._blocked(name, risk, "RESOURCE_CONFLICT", "Outro Agent Run controla este recurso.")
                            else:
                                result = await self._execute(name, arguments)
                        else:
                            await runtime.transition(self._state("VERIFY" if runtime.needs_verification else ("OBSERVE" if not execution_results else "DIAGNOSE")))
                            result = await self._execute(name, arguments)
                    else:
                        result = await self._execute(name, arguments)
                    if runtime and locked:
                        if result.ok and mutable:
                            runtime.held_resources.add(resource)
                        else:
                            await runtime.release_resource(resource)

                execution_results.append(result)
                result_dump = result.model_dump(mode="json")
                observation = ledger.record(
                    tool_call_id=call.tool_call_id or GroundingLedger.new_call_id(),
                    tool_name=name,
                    result_data=result.data,
                    risk_level=str(result.risk.value),
                    resource_key=str(preflight.get("resource_key") or name),
                    arguments_fingerprint=self._arguments_fingerprint(name, arguments),
                    agent_run_id=runtime.run.id if runtime else None,
                    turn_id=turn_id,
                )
                monitor_create_verified = bool(
                    name == "monitor_create"
                    and result.ok
                    and result.data.get("success") is True
                    and result.data.get("effect_verified") is True
                    and (result.data.get("monitor") or {}).get("monitor_id")
                )
                if monitor_create_verified:
                    # The manager only returns effect_verified after the SQLite
                    # commit and a real initial probe. A second LLM-driven probe
                    # is unnecessary and can race a very short monitor.
                    observation.verification_status = VerificationStatus.VERIFIED
                if observation.success and observation.risk_level.upper() in READ_ONLY_RISKS:
                    ledger.record_verification_attempt(observation)
                if runtime:
                    await runtime.record_step(name, arguments, preflight, result_dump, observation=observation)
                working.append(LLMMessage(
                    role="tool",
                    tool_name=name,
                    tool_call_id=observation.tool_call_id,
                    content=json.dumps(self._model_safe_result(name, result_dump), ensure_ascii=False),
                ))
                if post_tool_instruction:
                    working.append(LLMMessage(role="system", content=post_tool_instruction))

                data = result.data
                error_code = str(data.get("error_code") or "")
                if runtime and runtime.required_local_backend and name == "system_shell" and error_code not in {
                    "LOCAL_BACKEND_TARGET_REQUIRED", "AGENT_READ_ONLY",
                }:
                    command_text = str(arguments.get("command", "")).casefold()
                    if re.search(rf"(?:127\.0\.0\.1|localhost|{runtime.local_backend_port}|uvicorn|python|health|backend|logs?)", command_text):
                        runtime.local_backend_observations += 1
                if runtime and name == "remote_shell" and error_code not in {
                    "REMOTE_NETWORK_PRECHECK_REQUIRED", "REMOTE_TARGET_MISMATCH",
                }:
                    runtime.remote_attempted = True
                if error_code == "APPROVAL_REQUIRED":
                    if runtime:
                        runtime.pending_approval_id = str(data.get("approval_id") or "") or None
                        runtime.run.pending_approval_id = runtime.pending_approval_id
                        await runtime.transition(self._state("WAITING_APPROVAL"))
                    working.append(LLMMessage(
                        role="system",
                        content=(
                            "WAITING_APPROVAL: interrompa o run. Informe objetivamente host/ação/impacto/risco e o approval_id. "
                            "Não proponha outra mutação e não diga que a ação já ocorreu."
                        ),
                    ))
                    force_final = True
                    stop_batch = True
                    break

                command_fp, result_fp = self._fingerprints(name, arguments, result)
                repeat_key = (command_fp, result_fp)
                repeats[repeat_key] = repeats.get(repeat_key, 0) + 1
                if runtime and repeats[repeat_key] > runtime.max_identical_repeats:
                    runtime.stop_reason = "AGENT_NO_PROGRESS"
                    working.append(LLMMessage(
                        role="system",
                        content="NO PROGRESS: o mesmo comando produziu o mesmo resultado repetidamente. Não o execute novamente; reporte o impasse.",
                    ))
                    force_final = True
                    stop_batch = True
                    break

                if result.ok:
                    consecutive_failures = 0
                    if runtime:
                        if monitor_create_verified:
                            runtime.needs_verification = False
                            for held_resource in list(runtime.held_resources):
                                await runtime.release_resource(held_resource)
                                runtime.held_resources.discard(held_resource)
                        elif risk != RiskLevel.READ_ONLY:
                            runtime.needs_verification = True
                            await runtime.transition(self._state("VERIFY"))
                        elif runtime.needs_verification:
                            runtime.needs_verification = False
                            for held_resource in list(runtime.held_resources):
                                await runtime.release_resource(held_resource)
                                runtime.held_resources.discard(held_resource)
                        else:
                            await runtime.transition(self._state("DIAGNOSE"))
                else:
                    consecutive_failures += 1
                    if runtime and consecutive_failures >= runtime.max_consecutive_failures:
                        runtime.stop_reason = "AGENT_CONSECUTIVE_FAILURES"
                        working.append(LLMMessage(
                            role="system",
                            content="FAILURE LIMIT: três tentativas consecutivas falharam. Pare de executar tools e reporte evidências e limitações.",
                        ))
                        force_final = True
                        stop_batch = True
                        break
            if stop_batch or force_final:
                continue

        if runtime:
            if runtime.needs_verification:
                runtime.unverified_action = True
            runtime.stop_reason = runtime.stop_reason or "AGENT_REASONING_LIMIT"
            return self._safe_grounding_fallback(execution_results, ledger)
        raise RuntimeError("Tool loop exceeded its bounded number of reasoning rounds")

    async def _grounded_final(
        self,
        messages: list[LLMMessage],
        draft: str,
        results: list[ToolResult],
        ledger: GroundingLedger | None = None,
    ) -> str:
        ledger = ledger or self._ledger_from_results(results)
        violations = self._grounding_violations(draft, results, ledger)
        if not violations:
            return draft
        if any(item.kind == "UNVERIFIED_HARDWARE" for item in violations):
            from app.usb.hardware import UNVERIFIED_RESPONSE
            return UNVERIFIED_RESPONSE
        logger.info(
            "grounding_correction_triggered",
            extra={
                "kinds": sorted({item.kind for item in violations}),
                "details": [item.detail for item in violations][:5],
            },
        )
        messages.append(LLMMessage(
            role="system",
            content=(
                "GROUNDING CORRECTION REQUIRED: a resposta contém afirmações sem suporte nos resultados reais das tools.\n"
                + "\n".join(f"- {item.kind}: {item.detail}" for item in violations)
                + "\nReescreva em português brasileiro usando somente success, exit_code, stdout, stderr e message reais. "
                "Valores ausentes (PID, SessionId, latência, exit codes) devem ser relatados como não confirmados; "
                "nunca invente, complete ou estime. Exit 0 vazio ou grep/findstr exit 1 vazio significa nenhuma correspondência. "
                "Mutações só podem ser relatadas como concluídas se existir verificação read-only correlata. "
                "Se houver SSH_AUTHENTICATION_FAILED, informe que a conta/chave cadastrada precisa ser provisionada fora do LLM; "
                "não prometa procurar chaves, não peça senha e não tente outro usuário."
            ),
        ))
        corrected = await self.llm.complete(messages, None)
        if (
            corrected.content.strip()
            and not corrected.tool_calls
            and not self._looks_like_textual_tool_call(corrected.content)
            and not self._grounding_violations(corrected.content, results, ledger)
        ):
            return corrected.content.strip()
        return self._safe_grounding_fallback(results, ledger)

    @staticmethod
    def _ledger_from_results(results: list[ToolResult]) -> GroundingLedger:
        ledger = GroundingLedger()
        for result in results:
            ledger.record(
                tool_call_id=GroundingLedger.new_call_id(),
                tool_name=result.tool,
                result_data=result.data,
                risk_level=str(result.risk.value),
            )
        return ledger

    @classmethod
    def _grounding_violations(cls, draft: str, results: list[ToolResult], ledger: GroundingLedger) -> list[GroundingViolation]:
        violations: list[GroundingViolation] = []
        violations.extend(fabricated_value_claims(draft, ledger))
        violations.extend(unverified_effect_claims(draft, ledger))
        violations.extend(absence_claims_without_evidence(draft, ledger))
        from app.usb.hardware import unsupported_hardware_claims
        violations.extend(GroundingViolation(kind="UNVERIFIED_HARDWARE", detail=kind)
                          for kind in unsupported_hardware_claims(draft, ledger.observations, ledger.turn_id))
        if cls._needs_grounding_correction(draft, results):
            violations.append(GroundingViolation(
                kind="CONTRADICTION",
                detail="O rascunho contradiz resultados estruturados de system_shell/remote_shell.",
            ))
        return violations

    @staticmethod
    def _asserts_effect_without_observation(content: str, ledger: GroundingLedger) -> bool:
        """True when the draft claims a completed effect that no observation supports."""
        return bool(unverified_effect_claims(content, ledger))

    @staticmethod
    def _needs_grounding_correction(draft: str, results: list[ToolResult]) -> bool:
        successful = [result.data for result in results if result.data.get("success") is True]
        all_data = [result.data for result in results]
        claim = draft.casefold()
        claims_failure = bool(re.search(
            r"(?:acesso negado|access denied|permission denied|falta de permiss|problema de permiss|"
            r"privil[eé]gios? de administrador|modo administrador|negad[oa] pelo sistema|erro de permiss)", claim,
        ))
        claims_incomplete = bool(re.search(
            r"(?:estou analisando|ainda estou analisando|\b(?:vou|vamos|irei|farei)\b|"
            r"verificarei|tentarei|solicitarei|aguarde enquanto)", claim,
        ))
        auth_failed = any(str(item.get("error_code")) == "SSH_AUTHENTICATION_FAILED" for item in all_data)
        unsafe_credential_followup = auth_failed and bool(re.search(
            r"(?:senha|password|credencia|chave|solicitar.*informa|tentar.*autentica|outro usu[aá]rio)", claim,
        ))
        if unsafe_credential_followup:
            return True
        if not successful:
            return False
        if claims_incomplete:
            return True
        if not claims_failure:
            return False
        evidence = "\n".join(
            f"{item.get('stdout', '')}\n{item.get('stderr', '')}\n{item.get('message', '')}".casefold()
            for item in successful
        )
        return not re.search(r"(?:acesso negado|access denied|permission denied|unauthorized)", evidence)

    @staticmethod
    def _needs_read_only_retry(draft: str, results: list[ToolResult]) -> bool:
        failed = [
            result for result in results
            if result.tool == "system_shell" and result.risk == RiskLevel.READ_ONLY and result.data.get("success") is False
        ]
        if not failed:
            return False
        evidence = "\n".join(f"{item.data.get('stderr', '')}\n{item.data.get('message', '')}".casefold() for item in failed[-2:])
        retryable = bool(re.search(r"(?:acesso negado|access denied|permissiondenied|n[aã]o reconhecido|not recognized|n[aã]o foi encontrado)", evidence))
        draft_wants_to_stop = bool(re.search(r"(?:n[aã]o consegui|n[aã]o consigo|posso tentar|vamos tentar|quer que eu|modo administrador|permiss)", draft.casefold()))
        return retryable and draft_wants_to_stop

    @staticmethod
    def _safe_grounding_fallback(results: list[ToolResult], ledger: GroundingLedger | None = None) -> str:
        if any(str(result.data.get("error_code")) == "SSH_AUTHENTICATION_FAILED" for result in results):
            return "O host respondeu ao teste local, mas rejeitou a autenticação SSH da conta/chave cadastrada. Provisione a chave autorizada no Trusted Host Registry fora da conversa; nenhuma senha foi solicitada ou exposta."
        if any(str(result.data.get("error_code")) == "AGENT_READ_ONLY" for result in results):
            return "Inspecionei o backend local e a tentativa de alteração foi bloqueada pelo modo somente leitura. Nenhuma mudança foi realizada; os resultados de porta/processo permanecem registrados no Agent Run."
        if ledger:
            unverified = ledger.pending_mutations() or [
                item for item in ledger.observations
                if item.verification_status == VerificationStatus.VERIFICATION_FAILED
            ]
            if unverified:
                commands = "; ".join(dict.fromkeys(item.command[:80] for item in unverified if item.command))
                detail = f" Ações sem confirmação correlata: {commands}." if commands else ""
                return (
                    "A alteração foi executada, mas não consegui confirmar o efeito com uma verificação read-only correlata."
                    + detail
                    + " Não há evidência suficiente para afirmar sucesso além disso."
                )
            if not ledger.has_any_output():
                return (
                    "Os comandos terminaram sem retornar dados que permitam confirmar o resultado solicitado. "
                    "Não há PID, estado de processo ou outro valor confirmável nesta consulta."
                )
        desktop_status = next(
            (
                result.data for result in reversed(results)
                if result.tool == "desktop_windows" and result.data.get("success") is True
            ),
            None,
        )
        if desktop_status is not None and isinstance(desktop_status.get("open"), bool):
            label = str(desktop_status.get("app") or desktop_status.get("query") or "aplicativo consultado")
            if desktop_status["open"]:
                windows = desktop_status.get("windows")
                count = len(windows) if isinstance(windows, list) else 0
                count_text = f"; a consulta confirmou {count} janela(s) visível(is)" if count else ""
                return f"Sim. {label} está aberto agora{count_text}."
            return f"Não encontrei janela visível de {label} nesta consulta atual."
        local_evidence = "\n".join(
            f"{result.data.get('command', '')}\n{result.data.get('stdout', '')}"
            for result in results if result.tool == "system_shell"
        )
        if re.search(r"(?i):8000\b", local_evidence) and re.search(r"(?i)\bLISTENING\b", local_evidence):
            pid_match = re.search(r"(?im):8000\s+\S+\s+LISTENING\s+(\d+)\s*$", local_evidence)
            pid_text = f", associada ao PID {pid_match.group(1)}" if pid_match else ""
            return f"A porta local 8000 está em LISTENING{pid_text}. A inspeção foi concluída sem alterar processos ou serviços."
        successful = [result.data for result in results if result.data.get("success") is True]
        if successful and not any(item.get("stdout") or item.get("stderr") for item in successful):
            return "A investigação terminou sem encontrar linhas correspondentes nos comandos executados."
        if successful:
            return "A investigação foi interrompida pelo limite de segurança; os resultados reais coletados permanecem registrados."
        return "A investigação não conseguiu obter evidência suficiente e foi interrompida pelo limite de segurança."

    @staticmethod
    def _monitor_confirmation(results: list[ToolResult]) -> str:
        result = next(
            item for item in reversed(results)
            if item.tool == "monitor_create"
            and item.ok
            and item.data.get("success") is True
            and (item.data.get("monitor") or {}).get("monitor_id")
        )
        monitor = result.data["monitor"]
        monitor_id = str(monitor["monitor_id"])
        objective = str(monitor.get("objective") or "acompanhamento solicitado")[:300]
        status = str(monitor.get("status") or "ACTIVE")
        reading = monitor.get("last_reading") or {}
        value = reading.get("value")
        reading_text = (
            f" A leitura inicial real foi {str(value)[:160]}."
            if reading.get("ok") is True and value is not None
            else " A primeira tentativa de leitura ficou registrada no job."
        )
        if status == "ACTIVE":
            return (
                f"Vou monitorar por meio do MonitorJob real {monitor_id}, criado e persistido para {objective}."
                f"{reading_text} Intervalo: {monitor.get('interval_seconds')} segundos; "
                f"prazo: {monitor.get('duration_seconds')} segundos. Avisarei automaticamente sobre "
                "mudança relevante, condição atingida, erro ou fim do prazo."
            )
        return (
            f"O MonitorJob real {monitor_id} foi criado para {objective} e já encerrou com status {status}."
            f"{reading_text} {str(monitor.get('final_summary') or '')[:500]}"
        ).strip()

    @staticmethod
    def _monitor_creation_failure(results: list[ToolResult]) -> str:
        failed = next(
            (item for item in reversed(results) if item.tool == "monitor_create" and not item.ok),
            None,
        )
        if failed is None:
            detail = "o modelo não conseguiu emitir uma chamada monitor_create válida"
        else:
            data = failed.data
            detail = str(
                data.get("message") or data.get("error_code")
                or data.get("error") or "falha sem detalhe"
            )
        return (
            "Não consegui criar um MonitorJob real; portanto, não há monitoramento ativo. "
            f"Falha: {redact_secrets(' '.join(detail.split()))[:360]}."
        )

    @staticmethod
    def _model_safe_result(name: str, result_dump: dict[str, Any]) -> dict[str, Any]:
        """Keep private desktop window titles out of the model context."""
        if not name.startswith("desktop_"):
            return result_dump
        safe = json.loads(json.dumps(result_dump, ensure_ascii=False))
        data = safe.get("data") if isinstance(safe, dict) else None
        if isinstance(data, dict) and isinstance(data.get("windows"), list):
            for window in data["windows"]:
                if isinstance(window, dict) and "title" in window:
                    window["title"] = "<janela visível>"
        return safe

    @staticmethod
    def _looks_like_textual_tool_call(content: str) -> bool:
        return bool(re.search(
            r"(?is)<\s*/?tool_call\b|(?:```(?:json|tool_call)?\s*)?\{\s*[\"']name[\"']\s*:\s*[\"'][a-z_][a-z_0-9]*[\"']",
            content,
        ))

    async def _execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            return await self.registry.execute(name, arguments, exposure="llm")
        except Exception as exc:
            return ToolResult(
                tool=name, risk=RiskLevel.READ_ONLY, ok=False,
                data={"success": False, "error_code": "TOOL_EXECUTION_ERROR", "message": redact_secrets(f"{type(exc).__name__}: {str(exc)[:300]}")},
                elapsed_ms=0,
            )

    @staticmethod
    def _blocked(name: str, risk: RiskLevel, code: str, message: str) -> ToolResult:
        return ToolResult(tool=name, risk=risk, ok=False, data={"success": False, "error_code": code, "message": message}, elapsed_ms=0)

    @staticmethod
    def _arguments_fingerprint(name: str, arguments: dict[str, Any]) -> str:
        safe_args = {key: value for key, value in arguments.items() if key not in {"approval_id", "reason"}}
        return hashlib.sha256(
            json.dumps({"tool": name, "args": safe_args}, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _fingerprints(name: str, arguments: dict, result: ToolResult) -> tuple[str, str]:
        safe_args = {key: value for key, value in arguments.items() if key not in {"approval_id", "reason"}}
        command_fp = hashlib.sha256(json.dumps({"tool": name, "args": safe_args}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        data = result.data
        result_fp = hashlib.sha256(json.dumps(
            {
                "ok": result.ok, "success": data.get("success"), "exit_code": data.get("exit_code"),
                "error_code": data.get("error_code"), "stdout": str(data.get("stdout", ""))[:2000],
                "stderr": str(data.get("stderr", ""))[:2000],
            }, sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return command_fp, result_fp

    @staticmethod
    def _state(value: str):
        from app.agent.models import AgentRunState
        return AgentRunState(value)
