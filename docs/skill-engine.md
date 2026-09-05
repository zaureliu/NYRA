# Skill Engine

`SkillRegistry` expõe somente handlers cadastrados e validados. Ele continua separado do `ToolRegistry`: shell arbitrário não é uma skill textual, mas a tool nativa e mediada `system_shell`. Cada skill declara nome, descrição, triggers, disponibilidade, prioridade, cooldown e permissão:

- `READ_ONLY`: pode executar;
- `CONFIRM_REQUIRED`: requer confirmação explícita;
- `DANGEROUS`: bloqueada por padrão.

Foram integradas as consultas existentes de rede, Sentinel, sistema, ping, DNS, serviço, alertas e memória, além de `get_active_app`, `get_idle_time`, `get_system_load`, `open_kazumi_dashboard`, `show_kazumi`, `hide_kazumi`, `mute_kazumi` e `unmute_kazumi`.

`open_application` continua preparado e desabilitado até existir allowlist explícita. PowerShell/CMD pertencem ao subsistema `system_shell`, onde classificação e approval são avaliados por comando; não são incorporados ao Skill Engine.

O painel `Settings > Skills` mostra permissão, disponibilidade, cooldown e último uso.
