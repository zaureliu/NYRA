# Persona & Emotional Runtime V1

`app.persona_runtime` é a autoridade local para a identidade comportamental da
KAZUMI. O Qwen continua responsável pela realização linguística; não define nem
persiste a identidade.

## Componentes e persistência

- `KazumiIdentity` e `PersonalityProfile`: contrato imutável versionado. O prompt
  pode pedir um estilo temporário, mas não reescreve esse contrato.
- `RelationshipState`: familiaridade e preferências de comunicação úteis.
  Evidência implícita precisa se repetir três vezes; preferência explícita pode
  ser aplicada imediatamente. Não existem métricas afetivas como amor ou
  lealdade.
- `EmotionalState`: emoção, intensidade, confiança, motivo e decay contextual.
  Histerese evita alternância sem causa; risco, erro próprio e recuperação real
  podem atravessar o hold.
- `DialoguePolicy`: seleciona o modo de resposta por regras e metadados. Sucesso
  operacional e falha crítica usam fast policy, sem uma segunda chamada LLM.
- `PersonaContextBuilder`: produz seis seções compactas antes da chamada ao Qwen
  e limita o bloco a 3.200 caracteres.

Identidade, relacionamento e o último estado emocional ficam nas tabelas
`nyra_identity_v1`, `relationship_state_v1` e `emotional_state_v1` do SQLite
local. Emoções antigas expiram: estados breves como `surprised` não são
restaurados dias depois.

## Integrações

O runtime recebe eventos estruturados de Tasks, Monitors, Runtime, Network,
SelfDev, Operator e approvals. Ele publica `KAZUMI_EMOTION_CHANGED`; o World State
expõe somente emoção/intensidade e policy atuais. Memory V2 fornece no máximo
três preferências ou episódios relevantes, tratados como dados sem autoridade.

Proactive Presence usa a mesma policy e estado emocional na mensagem final. A
interface de voz expõe `emotion`, `intensity` e `style`; quando o provider não
declara suporte nativo, `acoustic_emotion=neutral` e a degradação aparece
explicitamente nos metadados, sem simular capability.

Endpoints locais de diagnóstico:

- `GET /api/persona-runtime/status`
- `GET /api/persona-runtime/context-preview?q=...`
- `POST /api/persona-runtime/relationship/evidence`

Esses endpoints não concedem autorização operacional. Persona, memória e
notificação nunca contornam approval, risk policy, Action Budget ou grounding.
