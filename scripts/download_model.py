# -*- coding: utf-8 -*-
"""从 hf-mirror 下载模型（hf-xet 并行加速）"""
import os

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
from huggingface_hub import snapshot_download  # noqa: E402

p = snapshot_download(
    "Qwen/Qwen2.5-14B-Instruct-AWQ",
    local_dir="/root/autodl-tmp/models_hub/Qwen2.5-14B-Instruct-AWQ",
    allow_patterns=["*.json", "*.safetensors", "merges.txt", "vocab.json", "*.jinja"],
)
print("DONE", p)
