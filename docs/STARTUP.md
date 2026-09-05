# Inicialização da KAZUMI

## Desenvolvimento — entrypoint oficial

Na raiz canônica:

```powershell
cd .\KAZUMI
npm run dev
```

O bootstrap localiza a raiz pelo próprio script, valida `frontend/node_modules`
e `desktop/node_modules` contra seus `package-lock.json` e não executa `npm ci`
quando as árvores estão saudáveis. Se um reparo for realmente necessário, ele
encerra antes apenas processos Node/Cargo/Tauri vinculados a esta raiz, evitando
arquivos nativos bloqueados e sem tocar em outros projetos.

O sidecar `packaging/dist/kazumi-backend/kazumi-backend.exe` é reconstruído quando
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
.\start-kazumi.ps1
```

O executável regenerável fica em
`desktop/src-tauri/target/release/kazumi-desktop.exe`. O launcher de raiz requer
o desktop já gerado e inicia primeiro o backend Python da raiz canônica; o
desktop reutiliza esse serviço loopback saudável. O sidecar PyInstaller continua
incluído e validado para uso portátil, mas o atalho não depende de uma exceção
de antivírus caso proteção de endpoint o classifique heuristicamente.

O instalador `scripts/install-shortcut.ps1` cria `KAZUMI.lnk` na Área de Trabalho
e no Menu Iniciar. Ambos chamam `scripts/launch-kazumi.vbs`, que abre o launcher
de raiz sem console visível; o mutex do bootstrap e o plugin single-instance do
Tauri impedem inicializações duplicadas. O launcher nunca altera nem desativa a
proteção de endpoint.

## Separação de dados

- fonte versionada: raiz Git, sem binários ou dependências geradas;
- dependências locais: `frontend/node_modules` e `desktop/node_modules`;
- builds locais: `frontend/dist`, `packaging/dist` e `desktop/src-tauri/target`;
- dados, logs, estados e marcadores de execução: `%LOCALAPPDATA%\KAZUMI`.

`node_modules`, `dist`, `target`, credenciais e dados do operador nunca entram no
snapshot público do SelfDev.
