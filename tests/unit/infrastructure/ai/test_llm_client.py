from infrastructure.ai.llm_client import LLMClient


def test_llm_client_keeps_omitted_values_available_for_dynamic_profile_merge():
    config = LLMClient(provider=object())._build_config()

    assert config.is_explicit("model") is False
    assert config.is_explicit("max_tokens") is False
    assert config.is_explicit("temperature") is False
