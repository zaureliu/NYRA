# NYRA Desktop Application Control V1

Abertura e verificação REAL de aplicações GUI no desktop Windows.
Correção do incidente Notepad: a NYRA nunca mais afirma "aberto" sem janela
visível confirmada por enumeração Win32 — e quando confirma, a janela está
efetivamente no desktop (checagem cruzada com o SO nos smokes).

## Arquitetura

```text
desktop_launch(app_id)            ← LLM só passa o ID; comando vem do registry
        ↓
DesktopController.launch()
        ├─ registry config/desktop_apps.yaml (fonte única confiável)
        ├─ snapshot PRÉ-launch das janelas já visíveis (do operador)
        ├─ spawn detached (DEVNULL pipes, sem console da NYRA)
        └─ polling Win32 EnumWindows até janela NOVA/persistente OU timeout honesto
                ↓
Structured Result (grounding):
{ execution_success, effect_verified, verification_status, windows[] }
```

## Regras de verificação (anti-falso-positivo)

1. **Snapshot pré-launch**: janelas já visíveis pertencem ao operador e NUNCA
   contam como evidência desta abertura (mesmo título/processo idênticos).
2. **Console hosts ignorados**: janelas de classe `ConsoleWindowClass`,
   `CASCADIA_HOSTING_WINDOW_CLASS`, `PseudoConsoleWindow` ou com título
   caminho-de-executável (`C:\...\x.exe`) são hosts de console — nunca GUI.
3. **Persistência mínima**: a janela precisa permanecer visível ≥1,6s nas
   amostragens para descartar flash transitório do boot do processo.
4. **Match**: PID rastreado/descendentes OU process_names OU window_title_contains.
5. **Falha honesta**: timeout/exit-sem-janela ⇒ `WINDOW_NOT_CONFIRMED`,
   `effect_verified=false`, e o processo filho DA NYRA é encerrado (higiene).
6. `single_instance: true` + janela já aberta ⇒ `already_open` VERIFIED sem spawn.

## Registry (config/desktop_apps.yaml)

| id | executável | observações |
| --- | --- | --- |
| notepad | notepad.exe | títulos "Bloco de Notas/Notepad/Sem título" |
| calculadora | calc.exe | UWP: janela pode pertencer ao ApplicationFrameHost (coberto por título) |
| paint | mspaint.exe | |
| explorer | explorer.exe | abre janela nova; NUNCA encerra o explorer do usuário |

IDs só existem aqui; `desktop_launch("python evil.py")` ⇒ `UNKNOWN_APP`.
Sem command override; sem secrets no arquivo.

## Tools

| Tool | Risco | Função |
| --- | --- | --- |
| `desktop_launch {app}` | LOW_RISK | abre e SÓ retorna sucesso com janela visível verificada |
| `desktop_windows {app?}` | READ_ONLY | estado ATUAL ("continua aberto?") |
| `desktop_list_apps` | READ_ONLY | apps registrados + janelas ativas |

## API

```text
GET  /api/desktop/apps                  lista + janelas ativas por app
GET  /api/desktop/windows?app=id        janelas visíveis agora
POST /api/desktop/apps/{id}/launch      launch com grounding fields
```

Eventos: `DESKTOP_APP_LAUNCHED`, `DESKTOP_WINDOW_VERIFIED`.

## Testes & Smoke

```bash
.venv\Scripts\python.exe -m pytest backend/tests/test_desktop_control.py -q   # 12 testes
& .venv\Scripts\python.exe scripts\desktop-control-smoke.py                   # smoke real
```

Smoke real executa: launch notepad → janela visível confirmada (Win32) →
tasklist independente confirma o PID no SO → fecha somente o PID da NYRA.

## Limitações conhecidas

- Apps UWP cuja janela muda de processo são cobertos por `window_title_contains`;
  se o operador renomear/ocultar títulos, a verificação cai no timeout honesto.
- Não há (ainda) close/minimize/focus estruturados — fechamento hoje via
  shell específico por PID (nunca por nome de imagem).
