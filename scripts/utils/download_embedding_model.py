"""Download and verify the bundled local embedding model.

The weights are stored under the repository's ``.models`` directory so the
runtime's ``EMBEDDING_MODEL_PATH`` and this utility always refer to the same
location.  Heavy ML imports stay lazy so the script can be inspected without
installing ``requirements-local.txt`` first.
"""

from __future__ import annotations

import os
from pathlib import Path

MODEL_ID = "BAAI/bge-small-zh-v1.5"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = PROJECT_ROOT / ".models" / "bge-small-zh-v1.5"


def download_model() -> Path:
    """Download the model snapshot into the repository workspace."""
    from huggingface_hub import snapshot_download

    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    endpoint = os.getenv("HF_ENDPOINT") or None
    snapshot_download(
        MODEL_ID,
        local_dir=MODEL_DIR,
        endpoint=endpoint,
        # The safetensors file is sufficient for sentence-transformers; avoid
        # storing the duplicate PyTorch checkpoint in the workspace.
        ignore_patterns=["pytorch_model.bin"],
        max_workers=4,
    )
    return MODEL_DIR


def verify_model(model_dir: Path = MODEL_DIR):
    """Load the downloaded snapshot without any network access."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(
        str(model_dir),
        device="cpu",
        trust_remote_code=False,
        local_files_only=True,
    )
    samples = [
        "林雪站在雪山之巅，寒风刺骨。",
        "李明在图书馆里翻阅古籍，寻找线索。",
        "雪山上的风越来越大，林雪感到一丝不安。",
    ]
    embeddings = model.encode(samples, convert_to_numpy=True)
    return model, embeddings


def main() -> int:
    print("=" * 60)
    print("下载本地向量模型")
    print("=" * 60)
    print(f"\n[1] 下载模型: {MODEL_ID}")
    print(f"    目标路径: {MODEL_DIR}")
    print("    首次运行会下载约 100MB，请稍候。")

    try:
        model_dir = download_model()
        print(f"✓ 模型下载成功: {model_dir}")

        print("\n[2] 离线加载并测试模型...")
        model, embeddings = verify_model(model_dir)
        print(f"✓ 生成向量维度: {embeddings.shape}")

        print("\n" + "=" * 60)
        print("✓ 本地模型下载并测试成功！")
        print("=" * 60)
        print(f"  EMBEDDING_SERVICE=local")
        print(f"  EMBEDDING_MODEL_PATH={model_dir}")
        print(f"  向量维度: {model.get_sentence_embedding_dimension()}")
        return 0
    except Exception as exc:
        print(f"\n✗ 错误: {exc}")
        print("\n可能的解决方案:")
        print("  1. 检查网络连接（需要访问 HuggingFace）")
        print("  2. 设置 HF_ENDPOINT=https://hf-mirror.com 后重试")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
