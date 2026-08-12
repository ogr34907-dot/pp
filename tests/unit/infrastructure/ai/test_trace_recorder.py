from domain.ai.value_objects.token_usage import TokenUsage
from infrastructure.ai.provider_factory import DynamicLLMService
from infrastructure.ai.trace_recorder import TraceRecorder


def test_trace_recorder_uses_environment_enabled_flag(monkeypatch):
    monkeypatch.setenv("AI_TRACE_ENABLED", "no")

    recorder = TraceRecorder()

    assert recorder.enabled is False


def test_trace_recorder_explicit_enabled_takes_precedence(monkeypatch):
    monkeypatch.setenv("AI_TRACE_ENABLED", "no")

    recorder = TraceRecorder(enabled=True)

    assert recorder.enabled is True


def test_trace_recorder_disabled_returns_none_without_store():
    recorder = TraceRecorder(enabled=False)

    assert recorder.record_span(phase="prompt_render") is None


def test_trace_usage_metadata_keeps_cache_and_reasoning_tokens():
    usage = TokenUsage(
        input_tokens=10,
        output_tokens=5,
        cache_hit_tokens=4,
        cache_miss_tokens=6,
        reasoning_tokens=3,
    )

    assert DynamicLLMService._usage_metadata(usage) == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_hit_tokens": 4,
        "cache_miss_tokens": 6,
        "reasoning_tokens": 3,
    }
