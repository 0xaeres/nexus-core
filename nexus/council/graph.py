"""LangGraph StateGraph for the 3-node council.

Topology (Reflexion-style draft-critique-revise, capped at 1 revision):

    START -> Drafter -> Critic -> (blocking?) -> Reviser -> END
                                ↘ (not blocking) ----------> END

The Critic does its OWN fresh retrieval against the corpus — the proven
faithfulness lever per Reflexion (2023) and Anthropic's Constitutional AI.
Without re-retrieval the critic devolves into sycophantic agreement.

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
from nexus.council.agents import critic, drafter, reviser
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
    """StateGraph: START -> Drafter -> Critic -> route(blocking?) -> {Reviser, END}."""

    async def drafter_node(state: CouncilState) -> dict:
        try:
            return await drafter.run(
                state, config=config, retrieval=handles.retrieval, chat=handles.chat_drafter
            )
        except Exception as e:
            raise CouncilAgentError("drafter", e) from e

    async def critic_node(state: CouncilState) -> dict:
        try:
            return await critic.run(
                state, config=config, retrieval=handles.retrieval, chat=handles.chat_critic
            )
        except Exception as e:
            raise CouncilAgentError("critic", e) from e

    async def reviser_node(state: CouncilState) -> dict:
        try:
            return await reviser.run(state, config=config, chat=handles.chat_reviser)
        except Exception as e:
            raise CouncilAgentError("reviser", e) from e

    def _route_after_critic(state: CouncilState) -> str:
        crit = state.get("critique")
        rev = state.get("revision_count", 0) or 0
        if crit is not None and crit.severity == "blocking" and rev == 0:
            return "reviser"
        return END

    graph: StateGraph = StateGraph(CouncilState)
    graph.add_node("drafter", drafter_node)
    graph.add_node("critic", critic_node)
    graph.add_node("reviser", reviser_node)

    graph.add_edge(START, "drafter")
    graph.add_edge("drafter", "critic")
    graph.add_conditional_edges(
        "critic", _route_after_critic, {"reviser": "reviser", END: END}
    )
    graph.add_edge("reviser", END)

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
