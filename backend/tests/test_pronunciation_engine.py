from app.speech.pronunciation.engine import PronunciationEngine


def test_longest_match_and_display_separation():
    source = "O GitHub Actions fez deploy do backend FastAPI."
    result = PronunciationEngine().prepare_for_speech(source, "edge_tts")
    assert result.original_text == source
    assert "GitHub Actions" in result.speech_text
    assert any(item.get("canonical") == "GitHub Actions" for item in result.applied_rules)
    assert "GitHub" not in result.detected_terms


def test_technical_units_bits_bytes_and_ip():
    result = PronunciationEngine().prepare_for_speech("500 Mbps não é igual a 500 MB/s. Host 192.168.1.20 na porta 443.")
    assert "megabits por segundo" in result.speech_text
    assert "megabytes por segundo" in result.speech_text
    assert "cento e noventa e dois" in result.speech_text


def test_urls_are_summarized_and_literal_mode_preserves_them():
    normal = PronunciationEngine().prepare_for_speech("Veja https://github.com/openai/nyra")
    literal = PronunciationEngine().prepare_for_speech("Veja https://github.com/openai/nyra", literal_required=True)
    assert "endereço disponível na tela" in normal.speech_text
    assert "https://github.com/openai/nyra" in literal.speech_text
