# Instalação

## Windows / PowerShell

Pré-requisitos: Windows 10/11, Ollama com `qwen3:8b`, Node 20+ e Python 3.11. Na máquina auditada esses itens já estão disponíveis.

```powershell
cd .\NYRA
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
npm run dev
```

O setup inicial cria o ambiente Python, copia `.env.example` quando necessário e prepara os modelos locais. Depois disso, `npm run dev` é suficiente. O bootstrap usa os locks reais de `frontend/` e `desktop/`, grava marcadores locais pelo hash dos locks e só executa `npm ci` quando a árvore correspondente está ausente ou inconsistente. Antes de um reparo, encerra apenas processos cuja origem pertence a esta raiz da NYRA, evitando `EPERM` sem afetar Node/Cargo de outros projetos.

O sidecar PyInstaller em `packaging/dist/nyra-backend` é validado por fingerprint das fontes e reconstruído automaticamente quando ausente ou stale, sempre antes de Tauri dev/build. `node_modules/`, `frontend/dist/`, `packaging/dist/` e `desktop/src-tauri/target/` são artefatos locais regeneráveis e permanecem fora do Git.

O código operacional fica na raiz clonada. O Self-Development Engine usa um workspace configurável fora dela para índice, estado e worktrees isolados; o snapshot público sanitizado também é configurável e separado do runtime. Banco, logs, filas, métricas e relatórios mutáveis são gravados em `%LOCALAPPDATA%\NYRA` (ou em `NYRA_DATA_HOME`, quando explicitamente configurado), nunca dentro da árvore Git.

Parada e status:

```powershell
npm run status
npm run stop
```

## Linux

Os scripts `.sh` são uma base simples para distribuições com Python 3.11, Node e Ollama. Permissão e serviço de áudio variam por distribuição.

Para gerar o pacote Windows, execute `npm run build:release` na raiz. O mesmo
bootstrap garante primeiro o sidecar PyInstaller e então chama o build Tauri.
O executável fica em `desktop/src-tauri/target/release/nyra-desktop.exe` e pode
ser iniciado com `.\start-nyra.ps1`. Modelos TTS são lidos do runtime
`NYRA_DATA_HOME`/`%LOCALAPPDATA%\NYRA`; nenhum banco, segredo, log ou estado do
operador é incorporado ao executável.

## Testes manuais

- UI: `http://127.0.0.1:5173`
- Swagger: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/api/health`
- Segure “Falar com NYRA”, fale e solte. O navegador solicitará acesso ao microfone.
