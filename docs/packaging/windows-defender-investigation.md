# Windows Defender / PyInstaller — investigação direcionada

Data: 2026-09-04. Baseline: `3d9bd933bde451c0a0e6c4abf1c5ed09ee7632d2`.
Checkpoint local: `checkpoint/pre-defender-packaging-20260904-3d9bd93`.
Nenhum reset, push, tag, publicação, exclusão do Defender, mudança de proteção
ou restauração de quarentena foi realizada.

## Evidência e classificação

O histórico local contém `Trojan:Win32/Bearfoos.B!ml`, ThreatID `2147731849`,
nos dois caminhos oficiais de `kazumi-backend.exe`. Há ocorrências desde
2026-09-03 03:21:21 (horário local), anteriores ao Deepgram
(`7655624108aa5b6caec600ffa19c62382a3ca3cc`, 2026-09-04 21:07).
Os registros consultados apresentam ação bem-sucedida e ThreatStatusID 3.
A ocorrência anterior mais recente era de 2026-09-04 21:12:23.

Defender observado: proteção em tempo real e antivírus habilitados;
plataforma `4.18.26080.3`, engine `1.1.26080.3`, inteligência `1.459.51.0`.
Não houve nova detecção durante as comparações Python/pacote de controle.

Classificação: **LIKELY PACKAGING FALSE POSITIVE**, não confirmado.
O gatilho exato da heurística não foi identificado. Não há evidência de que
Deepgram, UPX ou uma DLL específica sejam a causa. O rebuild de controle
sem mudanças funcionou; portanto, tampouco se atribui a ausência posterior
de alerta aos ajustes de higiene do pacote.

Um controle especialmente relevante: o `PYZ-00.pyz` do build anteriormente
bloqueado e o do rebuild limpo inalterado são byte a byte idênticos:
SHA-256 `b68052c78e145024aaf92d2cf9ae2592f20ba10008d0e6afe2f47551dbd29957`.
Isso cobre o arquivo de módulos Python, não a totalidade do executável,
dos recursos externos ou de todos os comportamentos possíveis em runtime.

O próprio projeto PyInstaller documenta a possibilidade de falsos positivos
associados ao bootloader distribuído. Isso é contexto, não prova deste caso:
[bootloader](https://pyinstaller.org/en/stable/bootloader-building.html),
[orientação do projeto sobre antivírus](https://github.com/pyinstaller/pyinstaller/blob/develop/.github/ISSUE_TEMPLATE/antivirus.md).
Nenhuma alteração de bootloader, ofuscação ou tentativa de evasão foi feita.

## Build, hooks e recursos

Builder canônico: `packaging/build-backend.ps1`, chamado pelo bootstrap de
`npm run build`; spec: `packaging/kazumi-backend.spec`.
Python utilizado: `backend/.venv`, CPython 3.11.9 x64.
PyInstaller 6.22.2; pyinstaller-hooks-contrib 2026.7.
Modo `onedir`, console, `exclude_binaries=True`, `strip=False`, `optimize=0`.
UPX desabilitado em EXE e COLLECT; nenhum executável UPX encontrado no PATH.
O commit `501a296` já desligara UPX em 2026-09-03 03:26:37, e o histórico
registra a mesma ameaça depois disso. Um novo controle UPX-on/off não se aplica.

Bootloader original: `PyInstaller/bootloader/Windows-64bit-intel/run.exe`,
SHA-256 `9f93091d097c7bca65e18e233b68886a017da65c9681efb1fcbfcc254e4fa1fd`,
conferente com o RECORD da distribuição instalada. Isso não equivale a uma
auditoria independente da cadeia de fornecimento.

EXE x64: seções `.text`, `.rdata`, `.data`, `.pdata`, `.fptable`, `.rsrc`,
`.reloc`; imports de bootloader USER32, KERNEL32 e ADVAPI32.
Recursos PE: ícone console padrão, grupo de ícones e manifesto `asInvoker`,
sem recurso VERSIONINFO customizado ou assinatura de código KAZUMI.
O overlay é o CArchive normal do PyInstaller, com o PYZ comprimido em zlib.
DLLs/modelos ficam em `_internal`, sem extração onefile `_MEI*` por execução.

Hidden imports explícitos preservados: `kokoro_onnx`, `espeakng_loader`,
`phonemizer`, `win32timezone`. Não há hook de projeto nem runtime hook customizado.
Hooks de análise são os do PyInstaller, contrib e NumPy instalados.
Incluem famílias de uvicorn/websockets, pydantic, cryptography, pywin32/comtypes,
PyAV, NumPy, fsspec, ONNX Runtime, soundfile e pyttsx3; um hook preventivo para
TensorFlow não significa que TensorFlow esteja incluído. Torch e TensorFlow
não constam dos módulos coletados.

Runtime hooks finais, todos das distribuições instaladas:

- `pyi_rth_pkgutil`: descoberta de módulos no PYZ;
- `pyi_rth_multiprocessing`: dispatch dos helpers multiprocessing;
- `pyi_rth_inspect`: resolução de nomes de arquivos frozen;
- `pyi_rth_pywintypes` e `pyi_rth_pythoncom`: caminho local pywin32;
- `pyi_rth_cryptography_openssl`: caminho de módulos OpenSSL, quando existente.

Antes também entrava `pyi_rth_pkgres`, via dependências de teste. A exclusão
comprovadamente direcionada de `fsspec.conftest`, `pytest`, `_pytest` eliminou
183 módulos Python e quatro extensões transitivas de teste; não há imports
dessas extensões no source da aplicação. Os testes de projetos feitos pelo
SelfDev continuam usando seu processo Python externo, não pytest frozen.

## Inventário binário

Build anterior ao Deepgram e build Deepgram: mesmos **159 arquivos nativos
únicos** (45 BINARY e 114 EXTENSION). O primeiro inventário recursivo encontrou
160 entradas porque `espeak-ng.dll` aparece antes e depois da reclassificação
DATA/BINARY do Analysis. O COLLECT remove a duplicação.

Pacote final: 45 BINARY + 110 EXTENSION = **155 DLLs/PYDs**, 464 DATA,
um único EXE (`kazumi-backend.exe`); 2556 módulos Python no Analysis.
Executáveis, DLLs ou runtime hooks inesperados: **0** dentro desse inventário.

Famílias presentes: CPython 3.11/OpenSSL/SQLite/libffi; runtime VC++;
NumPy/OpenBLAS; PyAV/FFmpeg e codecs; CTranslate2/OpenMP;
ONNX Runtime; eSpeak NG; libsndfile; pywin32; cryptography/cffi;
pydantic_core; tokenizers/hf-xet; psutil; HTTP/WebSocket e parsers YAML.
As dependências de codec são provenientes do wheel PyAV, não executáveis
baixados separadamente pelo backend. Não há `ffmpeg.exe` extra.

Todos os inputs nativos de wheels e runtime hooks conferem com seus RECORDs
locais, sem divergências. Os 28 inputs Python/Windows externos a wheels do
inventário anterior tinham assinatura Authenticode válida. Não se afirma que
um hash/assinatura isolado prove ausência de malware.

Os dois modelos TTS externos são os assets públicos já previstos pelo script
`scripts/download_tts_models.ps1`, com seus SHA-256 fixos conferidos:

- `kokoro-v1.0.int8.onnx`: `6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb`;
- `voices-v1.0.bin`: `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d`.

Silero `silero_vad_v6.onnx` vem do Faster-Whisper instalado. Nenhum modelo
Nova-3 é baixado/empacotado. Nenhum novo binário externo foi baixado nesta tarefa.

Inventários completos, caminhos de origem, hashes e comparações permanecem
locais em `.tmp/defender-inventory.json` e `.tmp/defender-final-inventory.json`.
Não foram publicados logs do operador, áudio, credenciais ou topologia.

## Checagem direcionada do source

Aplicado o checklist standalone de `finding-discovery`, limitado aos padrões
solicitados e aos caminhos que os explicam; não foi feita auditoria global.
Não foi encontrada evidência de comportamento malicioso nos trechos examinados.

Foram pesquisados injeção de processos/shellcode, credential dumping/LSASS,
autorun/RunOnce, mudanças no Defender, PowerShell codificado, download oculto
de executáveis, eval/exec/marshal e uso de subprocess/ctypes/registro.
Os pontos examinados correspondem a funções explícitas:

- Credential Broker usa `KAZUMI_CRED:`/CredReadW e DPAPI para seus próprios
  registros, sem enumeração ou dumping de credenciais de terceiros;
- descoberta de apps/USB e observação de janelas usam consultas locais;
  OpenProcess observado solicita apenas PROCESS_QUERY_LIMITED_INFORMATION;
- `Get-StartApps`, consultas de serviços/rotas e tarefas são diagnósticos;
  execução/mutação de tarefas e shell sensível passam por approval vinculado;
- elevação usa UAC `RunAs`, não bypass; o classificador marca comandos
  codificados e mudanças de segurança como perigosos;
- helpers TTS/SAPI, subprocessos de ferramentas e processos gerenciados têm
  finalidades explícitas; SSH usa host lógico e StrictHostKeyChecking;
- o launcher VBS esconde a janela de console; a opção de autostart Tauri é
  funcionalidade existente, não uma nova persistência introduzida nesta tarefa.

Defeito de packaging encontrado: coleta recursiva de `config` incorporava
arquivos ignorados `homelab_hosts.yaml`, `homelab_hosts.local.yaml` e
`network_aliases.local.json`. Não foi necessário imprimir seu conteúdo.
Corrigido com lista explícita de assets públicos e registries iniciais vazios
vindos dos templates `.example`. O runtime existente e suas configurações
nunca são apagados ou sobrescritos por essa mudança.

## Controles e mudanças mínimas

1. Backend Python canônico: startup/health PASS; 60 s de observação após health;
   nenhuma detecção; processo encerrado, porta livre.
2. Rebuild limpo sem alterar spec: cache/work/dist novos e privados do teste,
   UPX ainda off. SHA-256 antes de executar:
   `537bf9854e539aec4dd09b9e358f477b52bbdb8b750d3b0a87b42f169ff05ce9`.
   Startup PASS, 60 s após health, nenhuma detecção. Exame personalizado do
   Defender concluído em 21:34:23 sem alerta.
3. Higiene do pacote: assets públicos explícitos; excluir apenas dependências
   de teste identificadas; `--clean` com `PYINSTALLER_CONFIG_DIR` isolado por build.
   UPX, bootloader, versão do PyInstaller e funcionalidades KAZUMI preservados.

Cada build usa `%LOCALAPPDATA%/KAZUMI/tmp/pyinstaller-<id>/{cache,work,dist}`.
`--clean` só toca seu cache gerado, não cache global ou dados do operador.
O output anterior é movido pelo builder para `kazumi-backend-previous-<id>`;
nenhum arquivo da quarentena é recuperado. Não houve limpeza destrutiva.
Não foi necessário criar matriz de repros mínimos, porque o controle
inalterado já passou. Não se fez uma série de mudanças para procurar um hash
que escapasse do antivírus.

Os probes Python e frozen observaram conexão resetada na resposta ao endpoint
de shutdown, exit code 2, sem force-kill e sem processo/porta residual.
Isso já ocorre no caminho de shutdown existente; não é prova de teardown
graceful de cada componente interno.

## Release final

`npm run build`: backend PyInstaller + frontend de produção + Tauri release/NSIS
PASS, sem instalar dependências. Testes direcionados: 6 PASS;
`git diff --check`: PASS. Não houve testes globais nem mudanças Rust/frontend.

SHA-256 do backend final, conferido antes da execução e idêntico nos dois
caminhos oficiais:
`50d90b823cb99f0882cf56138eef946137795865ac09fe3cdbdf07c436a164d3`.
Ambas as pastas oficiais passaram por exames personalizados locais do Defender
concluídos em 21:37:23/24 sem nova ameaça.

O atalho real `C:/Users/<USER>/Desktop/KAZUMI.lnk` aponta para WScript +
`<REPO_ROOT>/scripts/launch-kazumi.vbs`. Lançamento normal (não dev/CDP) respondeu
health em 21:39:18 com um backend e um desktop; porta 8000 em 127.0.0.1.
Evento nativo WM_CLOSE nas duas janelas visíveis: ambas ocultas, backend
continuou online (X = HIDE_TO_TRAY).

O clique físico de Tray Exit não foi validado: **MANUAL_VALIDATION_REQUIRED**.
O helper visual foi declarado incompatível pelo operador e não deve ser usado
nesta sessão. A instância de release normal não expõe CDP; não foi criado um
endpoint de teste, não foi recompilado o desktop para esse fim e não se usou
kill de processo como substituto do coordenador.

Foi confirmado no source que `quit_kazumi` (UiExit), o item nativo `quit`
(TrayExit) e ExitRequested (OsShutdown) delegam à mesma função
`shutdown::request_app_shutdown`. Ela para cursor/STT bridge, chama
`backend_manager::shutdown_owned`, encerra Presence, remove tray/janelas e
finaliza Tauri. A parte backend desse fluxo, `/internal/owned-shutdown` →
`coordinate_full_shutdown`, foi exercitada realmente nos dois controles e
liberou seus processos/porta. Isso **não** conta como execução do coordenador
desktop completo na instância oficial atual.

Na retomada, a instância oficial continuava saudável: backend PID 4880,
desktop PID 8384 e porta 127.0.0.1:8000 LISTENING. Ela foi mantida disponível
para saída manual; não se reporta backend=0/desktop=0 como se Exit tivesse
ocorrido. Uma consulta de Get-NetTCPConnection dentro do sandbox não encontrou
o listener; netstat e a consulta fora do sandbox confirmaram a porta em escuta.

### Helper incompatível (somente inspeção após a proibição)

`.tmp/invoke-kazumi-tray-exit.ps1` não contém Stop-Process, taskkill, WM_CLOSE,
SendMessage/PostMessage ou comando que encerre PowerShell/terminal.
Contém EnumWindows/FindWindow por classe, acessibilidade e cliques por
coordenadas. Seleciona um ícone sem título e procura um menu visível genérico,
confirmando o texto `Encerrar KAZUMI` somente mais adiante.

Antes de qualquer validação do alvo, usa `keybd_event(27, ...)` para enviar
Esc globalmente, sem vincular o teclado à KAZUMI. Se Codex estiver em primeiro
plano, esse Esc pode cancelar a sessão ativa — explicação provável, não uma
prova instrumentada de qual janela recebeu o evento. O resultado recuperado
da execução interrompida mostrou retângulo do botão de overflow
`1665,1040,1665,1040` (área zero), `visible=False` e erro
`Notification overflow did not open`, sem acionar Exit. O helper não foi
corrigido nem será novamente executado como parte desta investigação.

Um cache/log gerado pelo hf-xet durante o build foi preservado em
`.tmp/defender-generated-hf-cache`, fora dos arquivos versionados.

Limitações: sem causa heurística exata, sem confirmação Microsoft de falso
positivo e sem garantia sobre futuras assinaturas/builds; Exit desktop completo
pendente de validação manual. Não foram removidos
recursos para contornar Defender. Deepgram permanece preservado, sem benchmark
ou transmissão de áudio nesta tarefa.
