from pathlib import Path

from scripts.utils import download_embedding_model


def test_model_directory_is_workspace_local():
    expected = Path(download_embedding_model.PROJECT_ROOT) / ".models" / "bge-small-zh-v1.5"

    assert download_embedding_model.MODEL_DIR == expected
