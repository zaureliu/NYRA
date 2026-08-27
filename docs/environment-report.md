# Relatório do ambiente

Coleta executada em **2026-08-19**, no início da implementação do MVP.

## Sistema

- Sistema operacional: Microsoft Windows 10 Pro 64-bit, versão 10.0.19044
- Shell: Windows PowerShell
- CPU: Intel Core i5-11400F, 6 núcleos / 12 threads, 2,60 GHz base
- RAM: 24.409 MiB visíveis (aproximadamente 24 GB); ~14,9 GB livres na coleta
- GPU principal: AMD Radeon RX 7600
- Adaptador adicional: Parsec Virtual Display Adapter
- CUDA/NVIDIA: não disponível

Decisão: Ollama gerencia sua própria aceleração. STT e TTS usam CPU no MVP para compatibilidade com a GPU AMD no Windows. A ausência de CUDA não bloqueia texto, memória, ferramentas, avatar nem voz.

## Ferramentas encontradas

| Componente | Resultado |
|---|---|
| Python | 3.11.9 instalado no escopo do usuário durante o setup |
| Node.js | 24.19.0 |
| npm | 11.17.0 (`npm.cmd`, pois a política bloqueia `npm.ps1`) |
| Git | 2.55.0.windows.3 |
| Ollama | 0.30.11 |
| winget | 1.29.280 |

## Ollama

- Modelo reutilizado: `qwen3:8b`
- Tamanho local: 5,2 GB
- Arquitetura/parâmetros: Qwen3, 8,2B, Q4_K_M
- Janela informada: 40.960 tokens
- Capacidades: completion, tools e thinking
- Nenhum modelo LLM novo foi baixado.

## Observações

- Antes da intervenção, `py.exe` e `python.exe` apontavam apenas para aliases vazios da Microsoft Store. Python 3.11.9 foi instalado com winget no escopo do usuário.
- A execução de scripts PowerShell está restrita; scripts do projeto podem ser chamados com `powershell -ExecutionPolicy Bypass -File ...` quando a política local exigir.
- Chatterbox/XTTS dependem de stacks pesadas de ML e não têm uma rota AMD/CUDA adequada neste host, além de exigirem uma referência de voz autorizada. O primeiro teste do SAPI do Windows falhou por acesso COM negado no processo gerenciado. O fallback funcional escolhido é Kokoro ONNX int8 em CPU, com a voz feminina brasileira licenciada `pf_dora`; SAPI permanece isolado como último recurso.
