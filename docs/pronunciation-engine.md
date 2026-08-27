# Pronunciation Engine PT-BR

Pipeline V3.2:

`LLM response → display_text → Markdown/URL protection → technical parser → provider lexicon → numbers/units → prosody → TTS`

`display_text` nunca é alterado. `speech_text` é efêmero e contém apenas a forma destinada ao provider. A engine é determinística, local, sem LLM e sem execução de comandos. A ordem usa proteção de URLs/paths, longest-match-first, boundaries, regras lexicais e normalização técnica.

Prioridade: override salvo pelo usuário, provider/voice override, default lexicon, regra automática, forma nativa. Overrides ficam em `data/pronunciation/user_overrides.json` (não versionado); defaults ficam em `identity/pronunciation_ptbr.defaults.json`.

URLs, paths, hashes e código são resumidos no modo conversacional. `literal_required` preserva o conteúdo quando o usuário pede leitura exata. IP/CIDR/MAC, percentuais, versões, portas e unidades de bits/bytes recebem regras técnicas locais.

Uma regra inválida ou lexicon corrompido não derruba o TTS: o loader ignora a entrada e mantém a síntese com o texto disponível.
