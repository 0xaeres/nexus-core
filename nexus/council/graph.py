"""LangGraph StateGraph for the single product-skill council.

Topology:

    START -> Planner -> (Architect, Domain Expert, Quality Expert)
          -> Synthesizer -> Repair -> Eval -> Finalizer -> END

The graph is bounded: three repair attempts per skill. All outputs remain
proposals until a human approves them.

State is checkpointed to SQLite so a process kill mid-session can resume.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from nexus.config import NexusConfig
from nexus.council.agents import pack
from nexus.council.errors import CouncilAgentError
from nexus.council.state import CouncilState
from nexus.llm.client import ChatClient
from nexus.retrieval.pipeline import RetrievalContext

log = logging.getLogger(__name__)
TokenSink = Callable[[dict[str, str]], Awaitable[None]]


@dataclass
class CouncilHandles:
    retrieval: RetrievalContext
    chat_drafter: ChatClient
    chat_critic: ChatClient
    chat_reviser: ChatClient
    chat_architect: ChatClient
    chat_domain_expert: ChatClient
    chat_quality_expert: ChatClient

    async def aclose(self) -> None:
        await self.retrieval.aclose()
        await self.chat_drafter.aclose()
        await self.chat_critic.aclose()
        await self.chat_reviser.aclose()
        await self.chat_architect.aclose()
        await self.chat_domain_expert.aclose()
        await self.chat_quality_expert.aclose()


@asynccontextmanager
async def council_handles(
    config: NexusConfig,
    *,
    token_sink: TokenSink | None = None,
    trace_context: dict[str, str] | None = None,
) -> AsyncIterator[CouncilHandles]:
    drafter_cfg = config.models.drafter or config.models.council
    critic_cfg = config.models.critic or config.models.council
    reviser_cfg = config.models.reviser or config.models.council
    log.info(
        "council models: drafter=%s/%s critic=%s/%s reviser=%s/%s",
        drafter_cfg.provider,
        drafter_cfg.model,
        critic_cfg.provider,
        critic_cfg.model,
        reviser_cfg.provider,
        reviser_cfg.model,
    )
    handles = CouncilHandles(
        retrieval=RetrievalContext.from_config(config),
        chat_drafter=_chat_from_cfg(
            drafter_cfg, role="drafter", token_sink=token_sink, trace_context=trace_context
        ),
        chat_critic=_chat_from_cfg(
            critic_cfg, role="critic", token_sink=token_sink, trace_context=trace_context
        ),
        chat_reviser=_chat_from_cfg(
            reviser_cfg, role="reviser", token_sink=token_sink, trace_context=trace_context
        ),
        chat_architect=_chat_from_cfg(
            critic_cfg, role="architect", token_sink=token_sink, trace_context=trace_context
        ),
        chat_domain_expert=_chat_from_cfg(
            critic_cfg,
            role="domain_expert",
            token_sink=token_sink,
            trace_context=trace_context,
        ),
        chat_quality_expert=_chat_from_cfg(
            critic_cfg,
            role="quality_expert",
            token_sink=token_sink,
            trace_context=trace_context,
        ),
    )
    try:
        yield handles
    finally:
        await handles.aclose()


def _chat_from_cfg(
    cfg,
    *,
    role: str,
    token_sink: TokenSink | None,
    trace_context: dict[str, str] | None,
) -> ChatClient:
    sig = inspect.signature(ChatClient.from_cfg)
    if "trace_context" in sig.parameters:
        return ChatClient.from_cfg(
            cfg, role=role, token_sink=token_sink, trace_context=trace_context
        )
    return ChatClient.from_cfg(cfg, role=role, token_sink=token_sink)


def build_graph(config: NexusConfig, handles: CouncilHandles):
    """StateGraph for the bounded product-skill council."""

    async def planner_node(state: CouncilState) -> dict:
        try:
            return await pack.planner(
                state, config=config, retrieval=handles.retrieval, chat=handles.chat_drafter
            )
        except Exception as e:
            raise CouncilAgentError("planner", e) from e

    async def architect_node(state: CouncilState) -> dict:
        try:
            return await pack.expert(
                state, name="architect", retrieval=handles.retrieval, chat=handles.chat_architect
            )
        except Exception as e:
            raise CouncilAgentError("architect", e) from e

    async def domain_expert_node(state: CouncilState) -> dict:
        try:
            return await pack.expert(
                state, name="domain_expert", retrieval=handles.retrieval, chat=handles.chat_domain_expert
            )
        except Exception as e:
            raise CouncilAgentError("domain_expert", e) from e

    async def quality_expert_node(state: CouncilState) -> dict:
        try:
            return await pack.expert(
                state, name="quality_expert", retrieval=handles.retrieval, chat=handles.chat_quality_expert
            )
        except Exception as e:
            raise CouncilAgentError("quality_expert", e) from e

    async def synthesizer_node(state: CouncilState) -> dict:
        try:
            return await pack.synthesizer(
                state, config=config, chat=handles.chat_drafter
            )
        except Exception as e:
            raise CouncilAgentError("synthesizer", e) from e

    async def repair_node(state: CouncilState) -> dict:
        try:
            return await pack.repair_loop(
                state, chat=handles.chat_reviser, retrieval=handles.retrieval
            )
        except Exception as e:
            raise CouncilAgentError("repair", e) from e

    async def evaluator_node(state: CouncilState) -> dict:
        try:
            return await pack.evaluator(state, chat=handles.chat_critic)
        except Exception as e:
            raise CouncilAgentError("skill_eval", e) from e

    async def finalizer_node(state: CouncilState) -> dict:
        try:
            return await pack.finalizer(state)
        except Exception as e:
            raise CouncilAgentError("finalizer", e) from e

    graph: StateGraph = StateGraph(CouncilState)
    graph.add_node("planner", planner_node)
    graph.add_node("architect", architect_node)
    graph.add_node("domain_expert", domain_expert_node)
    graph.add_node("quality_expert", quality_expert_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_node("repair", repair_node)
    graph.add_node("skill_eval", evaluator_node)
    graph.add_node("finalizer", finalizer_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "architect")
    graph.add_edge("planner", "domain_expert")
    graph.add_edge("planner", "quality_expert")
    graph.add_edge(["architect", "domain_expert", "quality_expert"], "synthesizer")
    graph.add_edge("synthesizer", "repair")
    graph.add_edge("repair", "skill_eval")
    graph.add_edge("skill_eval", "finalizer")
    graph.add_edge("finalizer", END)

    return graph


def open_checkpointer(db_path: Path) -> AsyncSqliteSaver:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(db_path))


async def run_council(
    *,
    config: NexusConfig,
    session_id: str,
    initial: CouncilState,
    checkpoint_db: Path,
):
    """Run one council session end-to-end. Returns (final_state, proposal-or-None)."""
    async with council_handles(config) as handles, open_checkpointer(checkpoint_db) as saver:
        graph = build_graph(config, handles)
        compiled = graph.compile(checkpointer=saver)
        log.info("council: %s starting (topic=%r)", session_id, initial.get("topic"))
        final_state = await compiled.ainvoke(
            initial,
            config={"configurable": {"thread_id": session_id}},
        )
        proposal = final_state.get("proposal")
        log.info(
            "council: %s done — proposal=%s, msgs=%d, cost_entries=%d, revisions=%d",
            session_id,
            getattr(proposal, "id", None),
            len(final_state.get("deliberation", [])),
            len(final_state.get("costs", [])),
            final_state.get("revision_count", 0),
        )
        return final_state, proposal
