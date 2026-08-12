from __future__ import annotations

import pytest

from application.ai.llm_control_service import LLMProfile
from domain.ai.services.llm_service import DEFAULT_MAX_OUTPUT_TOKENS, GenerationConfig
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.provider_factory import DynamicLLMService, _make_cache_key


class _FakeControlService:
    def __init__(self, profiles: list[LLMProfile]):
        self.profiles = profiles
        self.index = 0

    def resolve_active_profile(self):
        return self.profiles[self.index]


class _FakeProvider:
    def __init__(self, name: str):
        self.name = name
        self.closed = False
        self.close_count = 0

    async def aclose(self):
        self.closed = True
        self.close_count += 1


class _FakeSyncClient:
    def __init__(self):
        self.is_closed = False

    def close(self):
        self.is_closed = True


class _FakeAsyncClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class _ProviderWithoutAclose:
    def __init__(self):
        self._http_client_sync = _FakeSyncClient()
        self._http_client = _FakeAsyncClient()


class _ConfiguredProvider:
    def __init__(self):
        self.settings = Settings(
            default_model="profile-model",
            default_max_tokens=4096,
            default_temperature=0.3,
            timeout_seconds=75,
        )


class _FakeFactory:
    def __init__(self, profiles: list[LLMProfile]):
        self.control_service = _FakeControlService(profiles)
        self.providers = {profile.id: _FakeProvider(profile.id) for profile in profiles}

    def create_from_profile(self, profile):
        return self.providers[profile.id]


def _profile(profile_id: str, *, model: str) -> LLMProfile:
    return LLMProfile(
        id=profile_id,
        name=profile_id,
        api_key="key",
        model=model,
        protocol="openai",
    )


@pytest.mark.asyncio
async def test_dynamic_llm_service_closes_old_provider_when_cache_key_changes():
    profiles = [
        _profile("p1", model="model-a"),
        _profile("p2", model="model-b"),
    ]
    factory = _FakeFactory(profiles)
    service = DynamicLLMService(factory)

    first = await service._resolve_provider()
    factory.control_service.index = 1
    second = await service._resolve_provider()

    assert first is factory.providers["p1"]
    assert second is factory.providers["p2"]
    assert factory.providers["p1"].closed is True
    assert factory.providers["p1"].close_count == 1
    assert factory.providers["p2"].closed is False


@pytest.mark.asyncio
async def test_dynamic_llm_service_reuses_provider_when_cache_key_is_stable():
    profiles = [_profile("p1", model="model-a")]
    factory = _FakeFactory(profiles)
    service = DynamicLLMService(factory)

    first = await service._resolve_provider()
    second = await service._resolve_provider()

    assert first is second
    assert factory.providers["p1"].closed is False


@pytest.mark.asyncio
async def test_dynamic_llm_service_aclose_closes_cached_provider_and_clears_cache():
    profiles = [_profile("p1", model="model-a")]
    factory = _FakeFactory(profiles)
    service = DynamicLLMService(factory)

    provider = await service._resolve_provider()
    await service.aclose()

    assert provider.closed is True
    assert service._cached_provider is None
    assert service._cached_key is None


@pytest.mark.asyncio
async def test_dynamic_llm_service_close_fallback_handles_provider_clients_without_aclose():
    service = DynamicLLMService(_FakeFactory([_profile("p1", model="model-a")]))
    provider = _ProviderWithoutAclose()
    service._cached_provider = provider
    service._cached_key = "manual"

    await service.aclose()

    assert provider._http_client_sync.is_closed is True
    assert provider._http_client.closed is True
    assert service._cached_provider is None


def test_provider_cache_key_changes_for_full_secret_and_extra_request_fields():
    profile = _profile("p1", model="model-a").model_copy(
        update={
            "api_key": "abcdefgh-1",
            "extra_headers": {"X-Tenant": "one"},
            "extra_query": {"region": "cn"},
            "extra_body": {"thinking": {"type": "enabled"}},
        }
    )
    same_semantics = profile.model_copy(
        update={
            "extra_body": {"thinking": {"type": "enabled"}},
            "extra_query": {"region": "cn"},
            "extra_headers": {"X-Tenant": "one"},
        }
    )
    changed_secret = profile.model_copy(update={"api_key": "abcdefgh-2"})
    changed_body = profile.model_copy(update={"extra_body": {"thinking": {"type": "disabled"}}})

    assert _make_cache_key(profile) == _make_cache_key(same_semantics)
    assert _make_cache_key(profile) != _make_cache_key(changed_secret)
    assert _make_cache_key(profile) != _make_cache_key(changed_body)


def test_dynamic_merge_preserves_explicit_default_values():
    provider = _ConfiguredProvider()

    merged = DynamicLLMService._merge_config(
        GenerationConfig(max_tokens=DEFAULT_MAX_OUTPUT_TOKENS, temperature=1.0),
        provider,
    )

    assert merged.max_tokens == DEFAULT_MAX_OUTPUT_TOKENS
    assert merged.temperature == 1.0


def test_dynamic_merge_applies_profile_defaults_only_to_omitted_values():
    provider = _ConfiguredProvider()

    merged = DynamicLLMService._merge_config(GenerationConfig(), provider)

    assert merged.model == "profile-model"
    assert merged.max_tokens == 4096
    assert merged.temperature == 0.3
    assert merged.timeout_seconds == 75
