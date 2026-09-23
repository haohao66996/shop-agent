# -*- coding: utf-8 -*-
"""Local BGE embedding helper for semantic memory recall."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import torch
from transformers import AutoModel, AutoTokenizer

from src.config import settings


class EmbeddingService:
    def __init__(self) -> None:
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self.available = Path(settings.embedding_model_path).exists()

    def _load(self) -> bool:
        if not self.available:
            return False
        if self._model is None:
            self._tokenizer = AutoTokenizer.from_pretrained(
                settings.embedding_model_path, local_files_only=True
            )
            self._model = AutoModel.from_pretrained(
                settings.embedding_model_path, local_files_only=True
            )
            self._model.eval()
        return True

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts or not self._load():
            return None
        with torch.no_grad():
            encoded = self._tokenizer(
                texts, padding=True, truncation=True, max_length=512,
                return_tensors="pt",
            )
            output = self._model(**encoded).last_hidden_state
            mask = encoded["attention_mask"].unsqueeze(-1).float()
            summed = (output * mask).sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1)
            vectors = summed / counts
            return vectors.cpu().numpy().tolist()


embedding = EmbeddingService()
