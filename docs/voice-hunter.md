# Voice Hunter V3.4

O Voice Hunter é uma busca manual, license-first, dentro de `Settings > Voice Lab > Voice Hunter`. Ele não é um crawler e não executa consultas periódicas. A voz oficial só muda depois de confirmação explícita em `Definir como voz oficial da KAZUMI`.

## Fluxo

1. `Pesquisar novas vozes` verifica as fontes primárias do catálogo e registra a decisão em `logs/voice-hunter-search.jsonl`.
2. Apenas amostras já aprovadas são normalizadas para WAV PCM16, mono, 24 kHz, com remoção de DC, trim leve e pico conservador.
3. SHA-256 impede duplicatas. Métricas de áudio e faster-whisper validam clareza e idioma sem inferir idade, identidade ou outros atributos sensíveis.
4. Os previews passam pelo Pronunciation Engine V3.2. Modelos pesados permanecem residentes durante o benchmark.
5. Cards exibem licença, status, score técnico, nota do usuário, sample, latência e ações A/B.

Estados da busca: `IDLE`, `SEARCHING`, `CHECKING_LICENSES`, `DOWNLOADING`, `ANALYZING`, `BENCHMARKING`, `READY` e `ERROR`. Cancelar preserva o cache anterior.

## Segurança de seleção

- `SAFE_FOR_KAZUMI_REFERENCE`: pode alimentar Chatterbox; exige sample local e seleção humana.
- `SAFE_FOR_DIRECT_TTS`: pode ser usado somente pelo provider correspondente.
- `AUDITION_ONLY`: pode ser ouvido e comparado; seleção oficial é bloqueada.
- `REJECTED`: teste e seleção são bloqueados.

Opções pagas permanecem desabilitadas até existir credencial e integração explícitas. Nenhuma compra ou assinatura é iniciada. Antes de qualquer seleção, o metadata do perfil atual é salvo em `data/voices/profile-backups/`; somente uma referência segura pode ser copiada para `data/voices/kazumi_reference.wav`.

## Dados e orçamento

O limite padrão é `MAX_VOICE_HUNTER_DOWNLOAD_GB=8`. Áudios ficam em `data/voices/candidates/<candidate_id>/` e não são versionados. Cada diretório contém `sample.wav`, `metadata.json`, `license.txt` e `source.json`. Resultados de benchmark ficam em `data/voice-benchmarks/voice-hunter/`.

`Remover candidatas descartadas` opera apenas dentro de `data/voices/candidates/`. Não remove a voz oficial, modelos, backups ou dados gerais da KAZUMI.

## API

- `GET /api/voice-hunter/status` e `/phrases`
- `POST /api/voice-hunter/search` e `/cancel`
- `GET /api/voice-hunter/candidates/{id}/sample`
- `POST /api/voice-hunter/candidates/{id}/preview`
- `POST /api/voice-hunter/compare`
- `PATCH /api/voice-hunter/candidates/{id}/preference`
- `POST /api/voice-hunter/candidates/{id}/select`
- `DELETE /api/voice-hunter/candidates/discarded`

Para repetir o benchmark real:

```powershell
.\.venv\Scripts\python.exe .\scripts\voice-hunter-benchmark.py --candidates kokoro-pf-dora,chatterbox-multilingual-default,omnivoice-brpt-calm-design --phrases all
```

O worker OmniVoice fica isolado em `.venv-omnivoice`; o Chatterbox permanece em `.venv-chatterbox`. Falha em qualquer um deles não impede Kokoro, Edge ou a inicialização da KAZUMI.
