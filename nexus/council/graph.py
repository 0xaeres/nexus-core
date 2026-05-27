"""LangGraph StateGraph for the product skill-pack council.

Topology:

    START -> Planner -> Experts -> Synthesizer -> Repair -> Judge
       -> (missing evidence?) -> Targeted Callback -> Synthesizer -> Repair -> Judge
       -> Finalizer -> END

The graph is bounded: one targeted callback, three repair attempts per skill,
and all outputs remain proposals until a human approves them.

State is checkpointed to SQLite so a process kill mid-session can resume.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
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


@dataclass
class CouncilHandles:
    retrieval: RetrievalContext
    chat_drafter: ChatClient
    chat_critic: ChatClient
    chat_reviser: ChatClient

    async def aclose(self) -> None:
        await self.retrieval.aclose()
        await self.chat_drafter.aclose()
        await self.chat_critic.aclose()
        await self.chat_reviser.aclose()


@asynccontextmanager
async def council_handles(config: NexusConfig) -> AsyncIterator[CouncilHandles]:
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
        chat_drafter=ChatClient.from_cfg(drafter_cfg, role="drafter"),
        chat_critic=ChatClient.from_cfg(critic_cfg, role="critic"),
        chat_reviser=ChatClient.from_cfg(reviser_cfg, role="reviser"),
    )
    try:
        yield handles
    finally:
        await handles.aclose()


def build_graph(config: NexusConfig, handles: CouncilHandles):
    """StateGraph for the bounded product skill-pack council."""

    async def planner_node(state: CouncilState) -> dict:
        try:
            return await pack.planner(
                state, config=config, retrieval=handles.retrieval, chat=handles.chat_drafter
            )
        except Exception as e:
            raise CouncilAgentError("planner", e) from e

    async def experts_node(state: CouncilState) -> dict:
        try:
            return await pack.experts(
                state, retrieval=handles.retrieval, chat=handles.chat_critic
            )
        except Exception as e:
            raise CouncilAgentError("experts", e) from e

    async def synthesizer_node(state: CouncilState) -> dict:
        try:
            return await pack.synthesizer(
                state, config=config, chat=handles.chat_drafter
            )
        except Exception as e:
            raise CouncilAgentError("synthesizer", e) from e

    async def repair_node(state: CouncilState) -> dict:
        try:
            return await pack.repair_loop(state, chat=handles.chat_reviser)
        except Exception as e:
            raise CouncilAgentError("repair", e) from e

    async def judge_node(state: CouncilState) -> dict:
        try:
            return await pack.judge(state, chat=handles.chat_critic)
        except Exception as e:
            raise CouncilAgentError("judge", e) from e

    async def callback_node(state: CouncilState) -> dict:
        try:
            return await pack.targeted_callback(
                state, retrieval=handles.retrieval, chat=handles.chat_critic
            )
        except Exception as e:
            raise CouncilAgentError("targeted_callback", e) from e

    async def finalizer_node(state: CouncilState) -> dict:
        try:
            return await pack.finalizer(state)
        except Exception as e:
            raise CouncilAgentError("finalizer", e) from e

    graph: StateGraph = StateGraph(CouncilState)
    graph.add_node("planner", planner_node)
    graph.add_node("experts", experts_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_node("repair", repair_node)
    graph.add_node("judge", judge_node)
    graph.add_node("targeted_callback", callback_node)
    graph.add_node("finalizer", finalizer_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "experts")
    graph.add_edge("experts", "synthesizer")
    graph.add_edge("synthesizer", "repair")
    graph.add_edge("repair", "judge")
    graph.add_conditional_edges(
        "judge",
        pack.should_callback,
        {"targeted_callback": "targeted_callback", "finalizer": "finalizer"},
    )
    graph.add_edge("targeted_callback", "synthesizer")
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
