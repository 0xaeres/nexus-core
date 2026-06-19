"""Product-scoped conversational GraphRAG agent."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from nexus.api.authz import assert_product_access
from nexus.api.deps import get_config_dep, get_registry
from nexus.config import ModelCfg, NexusConfig
from nexus.graph.models import GraphRAGAnswer, GraphRAGMessage
from nexus.mcp_server.tools import ToolState, ask_product_graph
from nexus.registry import Registry
from nexus.retrieval.evidence import EvidenceMode

log = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])

CHAT_MODEL_OPTIONS = (
    "deepseek-ai/DeepSeek-V4-Flash",
    "deepseek-ai/DeepSeek-V4-Pro",
    "Qwen/Qwen3.6-35B-A3B",
    "google/gemma-4-26B-A4B-it",
)


class AgentMessageRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None
    history: list[GraphRAGMessage] = Field(default_factory=list)
    current_file: str | None = None
    mode: EvidenceMode = "auto"
    max_depth: int = Field(default=3, ge=1, le=5)
    top_k: int = Field(default=8, ge=5, le=12)
    model: str | None = None


class AgentRequestMeta(BaseModel):
    current_file: str | None = None
    max_depth: int
    top_k: int
    mode: EvidenceMode
    model: str | None = None


class AgentSessionMessage(BaseModel):
    role: str
    content: str
    created_at: str
    request: AgentRequestMeta | None = None
    answer: GraphRAGAnswer | None = None


class AgentSessionReplay(BaseModel):
    id: str
    product_id: str
    messages: list[AgentSessionMessage] = Field(default_factory=list)
    created_at: str
    updated_at: str


_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    id          TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL,
    messages_js TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_product
    ON agent_sessions(product_id, updated_at DESC);
"""


@router.post("/products/{product_id}/agent/messages", response_model=GraphRAGAnswer)
async def ask_product_agent(
    product_id: str,
    body: AgentMessageRequest,
    request: Request,
    config: NexusConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> GraphRAGAnswer:
    """Ask Nexus an arbitrary product question through graph-filtered RAG."""
    assert_product_access(request, registry, product_id)
    model_name = body.model or (config.models.chat_agent.model if config.models.chat_agent else None)
    if body.model and body.model not in CHAT_MODEL_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported chat model: {body.model}",
        )
    if _is_greeting(body.message):
        answer = GraphRAGAnswer(
            product_id=product_id,
            query=body.message,
            answer="Hey. Ask me about Nexus code, retrieval, ingestion, council flow, or UI behavior.",
            confidence=1.0,
            graph_available=True,
        )
        answer.session_id = await _agent_sessions(config).append(
            product_id=product_id,
            session_id=body.session_id,
            user_message=body.message,
            answer=answer,
            request_meta=_request_meta(body, model_name),
        )
        return answer
    tool_state = ToolState(
        product=product_id,
        config=_config_for_chat_model(config, model_name),
    )
    try:
        await tool_state.graph_store.ensure_schema()
        payload = await ask_product_graph(
            tool_state,
            query=body.message,
            history=[msg.model_dump(mode="json") for msg in body.history[-16:]],
            current_file=body.current_file,
            mode=body.mode,
            max_depth=body.max_depth,
            top_k=body.top_k,
            synthesize=True,
        )
        answer = GraphRAGAnswer.model_validate(payload)
        answer.session_id = await _agent_sessions(config).append(
            product_id=product_id,
            session_id=body.session_id,
            user_message=body.message,
            answer=answer,
            request_meta=_request_meta(body, model_name),
        )
        return answer
    except Exception as e:
        log.exception("product agent failed product=%s", product_id)
        raise HTTPException(status_code=503, detail="product agent unavailable") from e
    finally:
        await tool_state.aclose()


@router.get("/products/{product_id}/agent/models")
async def list_product_agent_models(
    product_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    config: NexusConfig = Depends(get_config_dep),
) -> dict:
    assert_product_access(request, registry, product_id)
    selected = config.models.chat_agent.model if config.models.chat_agent else CHAT_MODEL_OPTIONS[0]
    return {"models": list(CHAT_MODEL_OPTIONS), "default": selected}


@router.get(
    "/products/{product_id}/agent/sessions/{session_id}",
    response_model=AgentSessionReplay,
)
async def get_product_agent_session(
    product_id: str,
    session_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    config: NexusConfig = Depends(get_config_dep),
) -> AgentSessionReplay:
    assert_product_access(request, registry, product_id)
    replay = await _agent_sessions(config).get(product_id=product_id, session_id=session_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="agent session not found")
    return AgentSessionReplay.model_validate(replay)


def _config_for_chat_model(config: NexusConfig, model_name: str | None) -> NexusConfig:
    if not model_name:
        return config
    models = config.models.model_copy(deep=True)
    base = models.chat_agent or models.council
    models.council = _with_model(base, model_name)
    return config.model_copy(update={"models": models})


def _with_model(base: ModelCfg, model_name: str) -> ModelCfg:
    cfg = base.model_copy(deep=True)
    cfg.model = model_name
    if not cfg.provider:
        cfg.provider = "deepinfra"
    return cfg


def _is_greeting(message: str) -> bool:
    text = message.strip().lower()
    return text in {"hi", "hey", "hello", "yo", "sup", "thanks", "thank you"}


def _agent_sessions(config: NexusConfig) -> AgentSessionStore:
    return AgentSessionStore(config.storage.proposal_queue.parent / "agent_sessions.db")


def _request_meta(body: AgentMessageRequest, model_name: str | None) -> AgentRequestMeta:
    return AgentRequestMeta(
        current_file=body.current_file,
        max_depth=body.max_depth,
        top_k=body.top_k,
        mode=body.mode,
        model=model_name,
    )


class AgentSessionStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SESSION_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        try:
            yield conn
        finally:
            conn.close()

    async def append(
        self,
        *,
        product_id: str,
        session_id: str | None,
        user_message: str,
        answer: GraphRAGAnswer,
        request_meta: AgentRequestMeta,
    ) -> str:
        return await asyncio.to_thread(
            self._append_sync,
            product_id=product_id,
            session_id=session_id,
            user_message=user_message,
            answer=answer,
            request_meta=request_meta,
        )

    def _append_sync(
        self,
        *,
        product_id: str,
        session_id: str | None,
        user_message: str,
        answer: GraphRAGAnswer,
        request_meta: AgentRequestMeta,
    ) -> str:
        sid = session_id or uuid.uuid4().hex
        now = datetime.now(UTC).isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT messages_js, created_at FROM agent_sessions WHERE id = ? AND product_id = ?",
                    (sid, product_id),
                ).fetchone()
                messages = json.loads(row["messages_js"]) if row else []
                created_at = row["created_at"] if row else now
                messages.append(
                    {
                        "role": "user",
                        "content": user_message,
                        "request": request_meta.model_dump(mode="json"),
                        "created_at": now,
                    }
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": answer.answer,
                        "answer": answer.model_dump(mode="json"),
                        "created_at": now,
                    }
                )
                conn.execute(
                    """INSERT OR REPLACE INTO agent_sessions
                       (id, product_id, messages_js, created_at, updated_at)
                       VALUES (?,?,?,?,?)""",
                    (sid, product_id, json.dumps(messages), created_at, now),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return sid

    async def get(self, *, product_id: str, session_id: str) -> AgentSessionReplay | None:
        return await asyncio.to_thread(
            self._get_sync,
            product_id=product_id,
            session_id=session_id,
        )

    def _get_sync(self, *, product_id: str, session_id: str) -> AgentSessionReplay | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE id = ? AND product_id = ?",
                (session_id, product_id),
            ).fetchone()
        if row is None:
            return None
        return AgentSessionReplay.model_validate(
            {
                "id": row["id"],
                "product_id": row["product_id"],
                "messages": json.loads(row["messages_js"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
