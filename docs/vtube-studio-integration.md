# Integração VTube Studio

Configuração em `Settings > Visual > Live2D`: Enabled, Auto/Live2D/Current, host/porta loopback, auto-connect, lip sync, cursor attention, physics, FPS e debug.

O provider suporta estados `DISABLED`, `NOT_INSTALLED`, `API_DISABLED`, `CONNECTING`, `AUTH_REQUIRED`, `CONNECTED`, `MODEL_MISSING`, `MODEL_LOADED`, `RECONNECTING` e `ERROR`. Atualizações são limitadas a 30/60 FPS e IDs só são enviados após `InputParameterListRequest`.

Default `ws://127.0.0.1:8001`, configurável. A API oficial pode escolher portas seguintes quando 8001 estiver ocupada; discovery UDP futuro/auxiliar usa 47779.
