# Inicialização da NYRA

## Desenvolvimento — entrypoint oficial

Na raiz canônica:

```powershell
cd .\NYRA
npm run dev
```

O bootstrap localiza a raiz pelo próprio script, valida `frontend/node_modules`
e `desktop/node_modules` contra seus `package-lock.json` e não executa `npm ci`
quando as árvores estão saudáveis. Se um reparo for realmente necessário, ele
encerra antes apenas processos Node/Cargo/Tauri vinculados a esta raiz, evitando
arquivos nativos bloqueados e sem tocar em outros projetos.

O sidecar `packaging/dist/nyra-backend/nyra-backend.exe` é reconstruído quando
ausente ou stale, antes do Tauri. Em seguida o bootstrap inicia Ollama, backend,
Vite e Tauri em ordem, espera `http://127.0.0.1:8000/api/health` ficar online e
confirma a UI em `http://127.0.0.1:5173`.

```powershell
npm run status
npm run stop
```

## Release local

```powershell
npm run build:release
.\start-nyra.ps1
```

O executável regenerável fica em
`desktop/src-tauri/target/release/nyra-desktop.exe`. O launcher de raiz reconstrói
automaticamente o sidecar ou o release apenas se estiverem ausentes/stale.

O instalador `scripts/install-shortcut.ps1` cria `NYRA.lnk` na Área de Trabalho
e no Menu Iniciar. Ambos chamam `scripts/launch-nyra.vbs`, que abre o launcher
de raiz sem console visível; o mutex do bootstrap e o plugin single-instance do
Tauri impedem inicializações duplicadas.

## Separação de dados

- fonte versionada: raiz Git, sem binários ou dependências geradas;
- dependências locais: `frontend/node_modules` e `desktop/node_modules`;
- builds locais: `frontend/dist`, `packaging/dist` e `desktop/src-tauri/target`;
- dados, logs, estados e marcadores de execução: `%LOCALAPPDATA%\NYRA`.

`node_modules`, `dist`, `target`, credenciais e dados do operador nunca entram no
snapshot público do SelfDev.
