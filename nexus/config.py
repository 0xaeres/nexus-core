"""Nexus configuration — loads nexus.yaml + env vars per ENGINEERING.md §15."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    """Recursively substitute ${VAR} placeholders with environment values."""
    if isinstance(value, str):
        return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


# ----- nested models ---------------------------------------------------------


class ConnectorCfg(BaseModel):
    name: str
    type: str
    watch: bool = False
    # remaining fields are connector-specific; allow extras
    model_config = {"extra": "allow"}


class VectorCollectionsCfg(BaseModel):
    code: str = "nexus_code"
    text: str = "nexus_text"


class VectorQuantizationCfg(BaseModel):
    """Qdrant dense-vector quantization.

    TurboQuant is available in Qdrant v1.18+. It is applied when collections are
    created; changing it for an existing collection requires a recreate/reindex
    or an explicit Qdrant collection update outside Nexus.
    """

    enabled: bool = True
    type: str = "turboquant"
    bits: str = "bits4"
    always_ram: bool = True


class VectorStoreCfg(BaseModel):
    url: str = "http://localhost:6333"
    timeout_s: int = 120
    upsert_batch_size: int = 16
    collections: VectorCollectionsCfg = Field(default_factory=VectorCollectionsCfg)
    quantization: VectorQuantizationCfg = Field(default_factory=VectorQuantizationCfg)


class GraphStoreCfg(BaseModel):
    """Required derived product-system graph store."""

    host: str = Field("localhost", min_length=1)
    port: int = Field(6379, gt=0, le=65535)
    username: str | None = None
    password: str | None = None
    ssl: bool = False
    graph_prefix: str = Field("nexus", min_length=1)
    max_connections: int = Field(16, gt=0)
    timeout_ms: int = Field(5_000, gt=0)


class ModelCfg(BaseModel):
    """Single LLM role config. Provider-specific extras allowed."""

    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    url: str | None = None
    dim: int | None = None
    temperature: float = 0.0
    top_p: float | None = None
    instruction_profile: str | None = None
    model_config = {"extra": "allow"}


class ModelsCfg(BaseModel):
    council: ModelCfg          # drafter + critic + reviser
    drafter: ModelCfg | None = None
    critic: ModelCfg | None = None
    reviser: ModelCfg | None = None
    synthesizer: ModelCfg | None = None
    chat_agent: ModelCfg | None = None
    light: ModelCfg            # enricher (HQE + doc context)
    embedding: ModelCfg
    reranker: ModelCfg


class EnrichCfg(BaseModel):
    docs: bool = False  # Optional doc contextual retrieval; off by default for fast ingest
    code: bool = False  # HQE: optional, expensive code-question generation


class EnrichmentWorkerCfg(BaseModel):
    enabled: bool = False
    poll_interval_s: float = 5.0
    max_attempts: int = 3


class IngestionCfg(BaseModel):
    enrich_chunks: EnrichCfg = Field(default_factory=EnrichCfg)
    embed_batch_size: int = 32
    quality_gate_threshold: float = 0.0
    file_batch_size: int = 50
    read_concurrency: int = 10
    batch_concurrency: int = 2
    enricher_concurrency: int = 4       # cloud inference — rate-limited, not RAM-limited
    enrichment_worker: EnrichmentWorkerCfg = Field(default_factory=EnrichmentWorkerCfg)


class ServerCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class StorageCfg(BaseModel):
    """Local SQLite paths. Defaults work for dev; mount /var/lib/nexus in prod."""

    proposal_queue: Path = Path("./data/proposals.db")
    council_checkpoint: Path = Path("./data/council.sqlite")


# ----- root config -----------------------------------------------------------


class NexusConfig(BaseSettings):
    """Root config. `NexusConfig.load(path)` reads YAML + env, returns instance."""

    model_config = SettingsConfigDict(extra="forbid")

    skills_repo: str = ""  # set via nexus.yaml OR runtime via /setup/skills-repo
    hierarchy_root: Path = Path("./skills")

    connectors: list[ConnectorCfg] = Field(default_factory=list)
    vector_store: VectorStoreCfg = Field(default_factory=VectorStoreCfg)
    graph_store: GraphStoreCfg = Field(default_factory=GraphStoreCfg)
    models: ModelsCfg
    ingestion: IngestionCfg = Field(default_factory=IngestionCfg)
    server: ServerCfg = Field(default_factory=ServerCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)

    @classmethod
    def load(cls, path: str | Path = "nexus.yaml") -> NexusConfig:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"Config not found at {p}. Run `cp nexus.yaml.example nexus.yaml` and edit."
            )
        raw = yaml.safe_load(p.read_text())
        expanded = _expand_env(raw)
        return cls(**expanded)


@lru_cache(maxsize=1)
def get_config(path: str | Path = "nexus.yaml") -> NexusConfig:
    """Process-wide cached config accessor."""
    return NexusConfig.load(path)
