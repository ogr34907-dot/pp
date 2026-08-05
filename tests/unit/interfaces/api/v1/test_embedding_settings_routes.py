from application.ai import embedding_config_service
from application.ai.embedding_config_service import EmbeddingConfigModel
from interfaces.api.v1.core import settings


def test_update_embedding_config_returns_api_serialization(monkeypatch):
    updated = EmbeddingConfigModel(
        mode="openai",
        api_key="embedding-key",
        base_url="https://embeddings.example.test/v1",
        model="text-embedding-3-small",
        use_gpu=False,
        model_path="",
        created_at="2026-08-05T09:00:00",
        updated_at="2026-08-05T09:01:00",
    )

    class StubEmbeddingConfigService:
        def update_config(self, **kwargs):
            assert kwargs == {
                "mode": "openai",
                "api_key": "embedding-key",
                "base_url": "https://embeddings.example.test/v1",
                "model": "text-embedding-3-small",
                "use_gpu": False,
                "model_path": "",
            }
            return updated

        def to_api_dict(self):
            return {
                **updated.to_dict(),
                "created_at": updated.created_at,
                "updated_at": updated.updated_at,
            }

    monkeypatch.setattr(
        embedding_config_service,
        "get_embedding_config_service",
        lambda: StubEmbeddingConfigService(),
    )

    result = settings.update_embedding_config(
        settings.EmbeddingConfigUpdate(
            mode="openai",
            api_key="embedding-key",
            base_url="https://embeddings.example.test/v1",
            model="text-embedding-3-small",
            use_gpu=False,
            model_path="",
        )
    )

    assert result == {
        "mode": "openai",
        "api_key": "embedding-key",
        "base_url": "https://embeddings.example.test/v1",
        "model": "text-embedding-3-small",
        "use_gpu": False,
        "model_path": "",
        "created_at": "2026-08-05T09:00:00",
        "updated_at": "2026-08-05T09:01:00",
    }
