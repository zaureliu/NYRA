# Live2D performance

Matriz preparada: VTS fechado, VTS aberto idle, modelo idle, speaking e physics. O provider oferece 30/60 FPS; 30 é o padrão por eficiência e updates param quando não há estado novo. VTube Studio aberto sem API/modelo mediu 739,7 MB de working set e aproximadamente 6,08% de CPU total normalizada em três segundos. Medições com modelo KAZUMI/physics continuam bloqueadas por `WAITING_FOR_LAYERED_ART`.

VTS fechado não adiciona loop agressivo: conexão usa backoff e a camada de personagem permanece vazia. Métricas reais de GPU/VRAM exigem modelo carregado.
