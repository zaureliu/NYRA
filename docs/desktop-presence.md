# Desktop Presence

NYRA Desktop é uma aplicação Tauri 2 que reutiliza React/Vite, SVG, WebSocket e o backend FastAPI do dashboard.

```powershell
.\scripts\start.ps1
.\scripts\start-desktop.ps1
```

A janela é transparente, sem moldura/sombra, fora da taskbar e always-on-top por padrão. Na primeira execução fica no canto inferior direito da área útil; o plugin window-state preserva posição e tamanho. A região de drag sobre o torso é invisível: não existe alça/card permanente.

- `Ctrl+Shift+Space`: push-to-talk enquanto pressionado;
- `Ctrl+Shift+N`: mostrar/ocultar;
- `Ctrl+Shift+I`: alternar interativo/click-through.

Se `Ctrl+Shift+I` estiver ocupado, o app tenta `Ctrl+Alt+I` automaticamente.

Conflitos de atalho vão para `desktop.log`; o tray continua sendo a recuperação segura. O tray oferece mostrar, ocultar, interativo, click-through, always-on-top, painel, falar, configurações, reconectar e encerrar.

O balão limita respostas a 220 caracteres; o dashboard conserva o texto completo. O WebSocket reconecta com backoff de 1 a 30 segundos. O Desktop Presence usa o Avatar V2 oficial com master e camadas SVG derivadas. “Iniciar com o Windows” começa desativado.

Escala persistida: 50%, 75%, 100%, 125% e 150%. `html`, `body`, `#root` e containers desktop permanecem transparentes.

## Cursor global

No Windows, uma thread nativa Tauri consulta `GetCursorPos` a aproximadamente 30 Hz e só emite uma atualização quando o cursor muda. O payload inclui bounds físicos da janela, do monitor do avatar e do monitor do cursor. A direção é calculada a partir do centro visual da personagem e normalizada pelo monitor em que o overlay está, portanto coordenadas negativas e monitores de tamanhos diferentes são suportados.

A WebView reutiliza o mesmo smoothing, dead zone, clamp, classificação de direções e retorno ao neutro do mouse follow web. O rastreamento não depende de `mousemove` do navegador e continua quando outro programa está em primeiro plano. Se Win32 não fornecer a posição, o evento entra em fallback neutro e o idle continua.

Quando o VTube Studio está carregado e `cursor_attention` está ativo, a ponte envia coordenadas contínuas ao mapeamento real descoberto no modelo. Os candidatos incluem `ParamEyeBallX`, `ParamEyeBallY`, `ParamAngleX` e `ParamAngleY`; os olhos recebem amplitude maior que a cabeça. O provider mantém o limite de FPS configurado e ignora IDs ausentes.

## Controle de aplicativos

Aplicativos cadastrados continuam passando pelo Desktop Apps Registry. Quando a descoberta dinâmica está habilitada, a NYRA também consulta, somente para leitura, App Paths, `PATH`, atalhos do menu Iniciar e `Get-StartApps`. Alvos do App Paths podem usar variáveis locais como `%SystemRoot%`; elas são expandidas diretamente pelo processo, sem shell, antes da revalidação e do launch. Um launch só termina como verificado depois que uma janela visível nova e compatível é confirmada via Win32.
