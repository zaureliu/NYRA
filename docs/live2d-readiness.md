# VTube Studio readiness

A Presence está pronta quando a API local está autenticada, há modelo carregado e o receiver Spout2 recebe frames com alpha válido. Esses sinais permanecem separados no diagnóstico.

Parâmetros são opcionais e descobertos por modelo. Boca usa `MouthOpen`/equivalentes; mouse usa `EyeBallX/Y` e `FaceAngleX/Y` quando disponíveis. IDs ausentes são ignorados isoladamente.

Sem VTS, modelo ou sender, o estado é indisponível/degraded e nenhuma personagem alternativa aparece.
