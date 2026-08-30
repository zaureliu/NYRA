# USB Device Monitor & Registry V1

O monitor USB inicia com a NYRA e usa `CM_Register_Notification` (ConfigMgr)
para receber mudanças Plug and Play. Cada hint nativo entra em uma fila limitada,
passa por debounce e dispara uma reconciliação SetupAPI. Uma reconciliação leve a
cada 30 segundos funciona como fallback e heartbeat; não há PowerShell recorrente.

Fluxo lógico:

`Windows PnP -> UsbDeviceService -> fingerprint/registry -> EventBus -> Computer State -> notification/UI`

O baseline do startup é persistido como `PRESENT_AT_STARTUP`, sem emitir alertas
de conexão. O registry e o histórico SQLite (máximo de 1000 eventos) ficam em
`%LOCALAPPDATA%\NYRA\usb-devices\registry.db`, nunca no repositório.

A identidade usa, em ordem, serial USB, Container ID, Device Instance ID e um
composto de VID/PID/fabricante/produto. VID/PID sozinho nunca identifica uma
unidade. Colisões de identidade fraca são separadas pelo instance id. Um nome
parecido com fingerprint diferente vira desconhecido/`IDENTITY_CHANGED`.

`trusted` significa somente “dispositivo reconhecido pelo usuário”. Não é prova
de segurança, autenticação criptográfica, certificação, proteção contra malware
ou contra spoofing.

Privacidade: a capability observa apenas presença e metadados PnP/volume. Ela não
abre ou indexa drives, não lê arquivos/documentos/celulares, não captura áudio,
keystrokes nem relatórios HID brutos, e não altera portas, rede ou dispositivos
de áudio padrão.
