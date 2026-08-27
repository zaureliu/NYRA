# Perception Engine

`PCAwareness` produz um `PerceptionSnapshot` local e efêmero. Ele usa APIs nativas do Windows para janela ativa, idle, cursor/monitor e resolução; métricas de CPU/RAM/disco vêm das dependências já instaladas. Não há keylogger, clipboard, leitura de campos nem captura de tela.

Por padrão, a janela ativa guarda apenas o executável e uma classificação como VS Code, Browser ou Terminal. O título fica `null`; quando explicitamente autorizado, ainda é truncado e sanitizado. Cursor é somente posição relativa atual e atividade recente, sem trilha histórica.

Frequências atuais:

- foreground, idle e cursor: 2 Hz;
- métricas do sistema: 1 Hz efetivo no snapshot;
- CPU alta: somente após 10 segundos sustentados;
- reações: event-driven.

O master switch desliga e encerra os sensores opcionais em runtime. `ContextSelector` não envia o snapshot inteiro ao LLM: app ativa entra em perguntas sobre a atividade atual, rede em perguntas de rede e métricas apenas quando relevantes.

Eventos rotineiros não viram memória. Apenas o subsistema de memória já existente pode registrar um fato explicitamente relevante, como outage ou preferência confirmada.
