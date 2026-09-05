# Tool Grounding & Anti-Hallucination

Política de confiabilidade para afirmações da KAZUMI sobre estado real do sistema.
Regra fundamental: **toda afirmação sobre estado do computador, rede, processos,
serviços, arquivos ou infraestrutura deve estar fundamentada em evidência retornada
por uma tool.** A KAZUMI não inventa, completa, estima ou infere valores ausentes.

## Componentes

| Arquivo | Finalidade |
| --- | --- |
| `backend/app/tools/grounding.py` | Módulo de grounding: `VerificationStatus`, `ToolObservation` (provenance), `GroundingLedger` (correlação call→result e verificação de mutações), detectores de fabricação/efeito/ausência. |
| `backend/app/tools/agent.py` | Integração no Agent Loop: atribuição de `tool_call_id`, registro de observações, correção de grounding (`_grounded_final`) e fallback determinístico failure-closed (`_safe_grounding_fallback`). |
| `backend/app/llm/base.py` | `tool_call_id` opcional em `LLMToolCall` e `LLMMessage`. |
| `backend/app/tools/shell_models.py`, `remote_models.py` | Campos `execution_success`, `effect_verified`, `verification_status` nos resultados de shell local/remoto. |
| `backend/app/agent/models.py`, `controller.py` | `AgentStep.verification_status` persistido por passo; status de run `COMPLETED_WITH_UNVERIFIED_ACTION`. |
| `backend/tests/test_tool_grounding.py` | Suíte anti-hallucination (19 testes). |

## Provenance e correlação

Cada tool call recebe um `tool_call_id` (gerado pelo loop quando o provider não fornece).
A mensagem `role="tool"` devolvida ao LLM carrega o mesmo id, e uma `ToolObservation`
é registrada no `GroundingLedger` com:

```text
tool_call_id, tool_name, execution_id, agent_run_id, timestamp,
arguments_fingerprint, resource_key, risk_level, success, exit_code,
error_code, command, stdout/stderr (limitados), flags de truncamento,
verification_status, verified_by_call_id
```

Garantias:

```text
tool_call A -> result A   (nunca result B)
assistant(N calls) -> N tool messages na mesma ordem, cada uma com o id da chamada
```

Logs debug (`kazumi.grounding.*`): `tool_observation_recorded`,
`mutation_verification_matched`, `grounding_correction_triggered` — sempre com
redaction aplicada pelos serviços de shell; nunca chain-of-thought.

## Execution vs Effect

`COMMAND_EXECUTED` não implica `INTENDED_EFFECT_VERIFIED`.

Resultados de shell expõem:

```json
{
  "execution_success": true,
  "effect_verified": null,
  "verification_status": "EXECUTED"
}
```

Estados de verificação (`VerificationStatus`):

```text
NOT_REQUIRED        consulta read-only; o stdout é a própria observação
EXECUTED            mutação executada, aguardando verificação correlata
VERIFIED            probe read-only bem-sucedido confirmou o estado
VERIFICATION_FAILED probe executado mas inconclusivo/negativo (vazio, "False", erro)
EXECUTION_FAILED    comando/mutação falhou
```

### Regras de verificação de mutações (ACT → VERIFY → REPORT)

1. Mutação bem-sucedida fica `EXECUTED`.
2. Um probe read-only posterior marca `VERIFIED` quando produz evidência real
   (stdout/stderr não vazios). Atribuição prefere probes que compartilham assunto
   com o comando da mutação (ex.: `Start-Process notepad.exe` ↔ `Get-Process notepad`);
   sem match de assunto, qualquer probe bem-sucedido do turno verifica em nível de turno.
3. Probe com saída totalmente vazia ou resposta explicitamente negativa
   (`False`, `$false`, `0`) marca `VERIFICATION_FAILED` — boilerplate de `message`
   não é evidência.
4. Probe que falha (exit ≠ 0) e compartilha assunto marca `VERIFICATION_FAILED`.
5. Se o modelo tenta encerrar o relatório com mutação pendente, o loop injeta
   `VERIFY REQUIRED` (até 2 vezes). Esgotado o limite, o run termina como
   `COMPLETED_WITH_UNVERIFIED_ACTION` — nunca como sucesso pleno sem confirmação.

## Enforcement no backend (não é só prompt)

Antes da resposta final, `_grounded_final` valida o rascunho contra o ledger:

| Violação | Gatilho |
| --- | --- |
| `FABRICATED_VALUE` | Valor rotulado citado (PID, SessionId, HasExited, porta, exit code, latência ms, % CPU/RAM/perda) que não aparece em nenhuma evidência. |
| `TRUNCATED_UNVERIFIABLE` | Mesmo caso, mas há saída truncada no turno — o modelo deve dizer que não consegue confirmar sem consulta mais específica. |
| `UNVERIFIED_EFFECT` | Afirmação de efeito concluído ("iniciado com sucesso", "abri", "criei") sem mutação verificada no turno. |
| `ABSENCE_WITHOUT_EVIDENCE` | Afirmação de ausência ("nenhum processo", "não existe listener") quando todas as consultas falharam (ex.: access denied) — ausência exige probe bem-sucedido ou fallback real. |
| `CONTRADICTION` | Heurísticas pré-existentes (alegar acesso negado quando a evidência mostra sucesso; promessas futuras; credential-hunting pós SSH_AUTHENTICATION_FAILED). |

Com violações: mensagem `GROUNDING CORRECTION REQUIRED` lista os problemas e pede
reescrita baseada somente em `success/exit_code/stdout/stderr/message`. Se a reescrita
continuar inválida, entra o fallback determinístico failure-closed, que relata apenas
o que as observações suportam (incluindo "executado, mas não confirmado" e o caso
de saída vazia).

O Agent Loop também bloqueia cedo: `EFFECT CLAIM WITHOUT OBSERVATION` dá ao modelo
uma chance de executar/verificar de fato antes de finalizar quando ele afirma efeito
sem nenhuma execução no turno.

## Resultados vazios, parciais e truncados

```text
exit 0 + stdout vazio + stderr vazio -> "terminou sem dados que permitam confirmar X"
dado presente parcialmente           -> relate só a parte comprovada (PID sim, SessionId "não confirmado")
stdout_truncated=true                -> proibido concluir ausência do que pode ter sido truncado
```

## Roteamento

`should_route_to_agent` cobre pedidos de GUI/processos/PID ("abre o bloco de notas",
"qual o PID do notepad", "existe notepad rodando?") para que passem pelo Agent Loop
com grounding — pedidos de abrir aplicativos nunca devem ser respondidos por texto livre.

## Runtime Supervisor

Ainda não implementado em código (apenas snapshot `.kazumi-runtime.json` do launcher).
Quando existir, `runtime_status/runtime_health/runtime_restart` devem ser fonte
preferencial e seus estados (READY/RUNNING/FAILED/STOPPED/CRASH_LOOP) provenientes de
checks reais — o mesmo padrão EXECUTED vs VERIFIED se aplica (restart executado +
health falhando ⇒ "executado, mas o health check não passou").

## Exemplos

```text
Operador: Kazumi, abre o bloco de notas.
[Start-Process notepad.exe => exit 0]
[Get-Process notepad    => nenhum processo]
KAZUMI: A solicitação de abertura foi executada, mas não consegui confirmar uma instância ativa do Notepad.
```

```text
Operador: Qual é o PID?
[Get-Process notepad => exit 0, stdout vazio]
KAZUMI: O comando terminou sem erro, mas não retornou dados que permitam informar o PID.
```

```text
[New-Item => exit 0][Test-Path => False]
KAZUMI: O comando de criação foi executado, mas a verificação indicou que o arquivo não existe; não posso afirmar que foi criado.
```

## Testes

```bash
.venv\Scripts\python.exe -m pytest backend/tests/test_tool_grounding.py -q
```

19 testes cobrem: fabricação de PID/SessionId, campos parciais, sonda vazia,
`False` explícito, efeito sem mutação, incidente notepad (#65), saída vazia (#50),
"No process found" (#51), access denied→ausência (#54), fallback grounded (#55),
correlação multi-tool (#56/#59), barreira de streaming (#58/#31), truncamento
(#60/#45), criação de arquivo (#63), ação verificada positiva, status
`COMPLETED_WITH_UNVERIFIED_ACTION` e roteamento GUI/PID.
