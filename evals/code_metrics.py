"""Compatibility wrapper for shared eval metric helpers."""

from __future__ import annotations

from evals.metrics import dcg, mean, ndcg_at_k, precision_at_k, recall_at_k

__all__ = ["dcg", "mean", "ndcg_at_k", "precision_at_k", "recall_at_k"]
