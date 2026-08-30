# Self-Development Engine V1

O Self-Development Engine da NYRA transforma evidência local recorrente em melhorias pequenas, isoladas, testadas e reversíveis. Ele não é um executor de texto do LLM e não amplia as permissões do operador.

## Diretórios e dados

- raiz do repositório: código operacional canônico.
- `NYRA_SELFDEV_WORKSPACE`: workspace externo para índice, locks, relatórios e worktrees de candidatos.
- `NYRA_SELFDEV_PUBLIC_SNAPSHOT`: snapshot público sanitizado e separado do runtime.
- `%LOCALAPPDATA%\NYRA\selfdev`: métricas, fila, notificações, relatórios e estado de restart.

O índice incremental guarda caminho relativo, linguagem, símbolos, imports, rotas, referências, tamanho, mtime e SHA-256. Conteúdo do código, conversas, áudio, memória, IPs, MACs, credenciais e chain-of-thought não são persistidos no índice.

## Fluxo

1. `RuntimeObserver` agrega métricas numéricas em janelas de 1 hora, 24 horas e 7 dias.
2. `ImprovementDetector` exige repetição ou uma solicitação explícita do operador e deduplica por fingerprint.
3. `SelfDevPlanner` exige evidência e produz hipótese, arquivos/símbolos esperados, critérios, testes, benchmark e rollback.
4. `SelfDevRiskClassifier` marca áreas protegidas como HIGH_RISK. `AUTONOMOUS_SAFE` aceita somente LOW_RISK; `AUTONOMOUS_ADVANCED` pode aceitar MEDIUM_RISK.
5. `WorktreeManager` cria um Git worktree por issue. `CodeWorker` aceita apenas `PatchBundle` Pydantic com paths contidos, hashes e limites; conteúdo com aparência de secret é rejeitado.
6. `ValidationPipeline` executa scan, testes selecionados, build/checks aplicáveis e `git diff --check`, sempre através de `system_shell`.
7. `PromotionManager` bloqueia a árvore estável suja e usa cherry-pick sob lock. O restart fica pendente até health pós-restart; falha dispara `git revert`, nunca `reset --hard`.
8. `GitHubPublisher` opera somente no snapshot público, depois de scan sem achados. Não usa force push.

Mudanças de approval, credenciais, redaction, shell, SSH/host key, política de segurança e publicação são sempre HIGH_RISK. O motor pode detectá-las e preparar evidência, mas nunca as autopromove.

## Modos e configuração

As opções ficam em `config/default.yaml`, variáveis `NYRA_*` e `Settings > Self-Dev`:

- `OFF`: serviço inativo.
- `OBSERVE_ONLY`: observa e mantém evidência/fila, sem implementar.
- `AUTONOMOUS_SAFE`: candidatos/promovidos somente em LOW_RISK.
- `AUTONOMOUS_ADVANCED`: também permite MEDIUM_RISK; HIGH_RISK continua bloqueado.

O modelo padrão é configurável e deve existir no Ollama local. O router não baixa modelos e não usa provedores cloud. `selfdev_auto_publish_github` permanece `false` por padrão e só deve ser ativado explicitamente depois que todos os gates locais e públicos estiverem validados.

## API e interface

Endpoints locais sob `/api/selfdev` expõem status, issues, detalhe/diff, execução manual, consulta do índice, inventário de modelos, notificações e rollback. A Operations UI mostra um chip resumido no topo e o painel em `Configurações > Self-Dev`. Ações sensíveis exibem confirmação e continuam subordinadas ao approval de uso único do `system_shell`.

## Recuperação e auditoria

Fila, promoções e restart pendente sobrevivem a reinícios. Eventos `selfdev.*` contêm apenas identificadores, risco, contagens e estados. Falhas do SelfDev degradam o serviço isoladamente e não impedem chat, voz, memória ou controle local. Uma melhoria aplicada pode ser revertida por commit; conflitos ou árvore canônica suja bloqueiam a promoção para revisão do operador.

## Gates V2

Antes dos gates V1, o candidate passa por `REPRODUCE` e `ROOT_CAUSE_ANALYSIS`. A validação acrescenta `STATIC_ANALYSIS`, `REGRESSION_BENCHMARK`, `CANARY_VALIDATION` e `BEHAVIOR_COMPARISON`. O baseline executável só pode usar o mirror isolado `workspace/repository`; o runtime estável nunca vira diretório de testes. Ausência desse mirror bloqueia o ciclo. LOW_RISK continua subordinado a todos os gates, MEDIUM segue o modo Advanced e HIGH nunca é promovido autonomamente.

O restart continua preparado em duas fases. Health pós-restart ausente ou negativo mantém a promoção incompleta e aciona `git revert`. O teste seguro de rollback pode usar um repositório Git temporário e um health probe intencionalmente negativo; ele não deve usar `reset --hard` nem tocar o runtime estável.
