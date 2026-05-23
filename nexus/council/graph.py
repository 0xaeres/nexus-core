"""LangGraph StateGraph for the council.

Topology:

      START
      /  \\
   Arch   Domain
      \\  /
   Synth
      |
     Adv ←─ conditional: routes back to Synth iff blocking + no revisions yet
      |
      END

Archaeologist and Domain Expert run in parallel. Synthesizer waits for both.
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
from nexus.council.agents import adversary, archaeologist, domain_expert, synthesizer
from nexus.council.state import CouncilState
from nexus.llm.client import ChatClient
from nexus.retrieval.pipeline import RetrievalContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- factory


@dataclass
class CouncilHandles:
    retrieval: RetrievalContext
    chat_arch: ChatClient
    chat_domain: ChatClient
    chat_synth: ChatClient
    chat_adv: ChatClient

    async def aclose(self) -> None:
        await self.retrieval.aclose()
        await self.chat_arch.aclose()
        await self.chat_domain.aclose()
        await self.chat_synth.aclose()
        await self.chat_adv.aclose()


@asynccontextmanager
async def council_handles(config: NexusConfig) -> AsyncIterator[CouncilHandles]:
    handles = CouncilHandles(
        retrieval=RetrievalContext.from_config(config),
        chat_arch=ChatClient.from_cfg(config.models.council, role="archaeologist"),
        chat_domain=ChatClient.from_cfg(config.models.council, role="domain_expert"),
        chat_synth=ChatClient.from_cfg(config.models.council, role="synthesizer"),
        chat_adv=ChatClient.from_cfg(config.models.council, role="adversary"),
    )
    try:
        yield handles
    finally:
        await handles.aclose()


# ---------------------------------------------------------------- graph


def build_graph(config: NexusConfig, handles: CouncilHandles):
    """StateGraph: START -> {Arch, Domain} -> Synth -> Adv -> route.

    Adv routes back to Synth iff (severity == blocking AND revision_count == 0).
    After one redraft + final Adv pass, END regardless.
    """

    async def arch_node(state: CouncilState) -> dict:
        return await archaeologist.run(
            state, config=config, retrieval=handles.retrieval, chat=handles.chat_arch
        )

    async def domain_node(state: CouncilState) -> dict:
        return await domain_expert.run(
            state, config=config, retrieval=handles.retrieval, chat=handles.chat_domain
        )

    async def synth_node(state: CouncilState) -> dict:
        return await synthesizer.run(state, config=config, chat=handles.chat_synth)

    async def adv_node(state: CouncilState) -> dict:
        proposal = state.get("proposal")
        if proposal is None:
            return {}
        crit, cost, msg = await adversary.critique(
            proposal=proposal, state=state, config=config, chat=handles.chat_adv
        )
        # Stamp the critique onto the proposal too so the queue row carries it.
        updated_proposal = proposal.model_copy(update={"adversary_critique": crit})
        return {
            "critique": crit,
            "proposal": updated_proposal,
            "deliberation": [msg],
            "costs": [cost],
        }

    def _route_after_adversary(state: CouncilState) -> str:
        crit = state.get("critique")
        rev = state.get("revision_count", 0) or 0
        if crit is not None and crit.severity == "blocking" and rev == 0:
            return "synthesizer"
        return END

    graph: StateGraph = StateGraph(CouncilState)
    graph.add_node("archaeologist", arch_node)
    graph.add_node("domain_expert", domain_node)
    graph.add_node("synthesizer", synth_node)
    graph.add_node("adversary", adv_node)

    graph.add_edge(START, "archaeologist")
    graph.add_edge(START, "domain_expert")
    graph.add_edge("archaeologist", "synthesizer")
    graph.add_edge("domain_expert", "synthesizer")
    graph.add_edge("synthesizer", "adversary")
    graph.add_conditional_edges(
        "adversary", _route_after_adversary, {"synthesizer": "synthesizer", END: END}
    )

    return graph


# ---------------------------------------------------------------- checkpointer


def open_checkpointer(db_path: Path) -> AsyncSqliteSaver:
    """Open an AsyncSqliteSaver against `db_path`. Caller owns lifecycle."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(db_path))


# ---------------------------------------------------------------- entry point


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
            "council: %s done — proposal=%s, msgs=%d, cost_entries=%d",
            session_id,
            getattr(proposal, "id", None),
            len(final_state.get("deliberation", [])),
            len(final_state.get("costs", [])),
        )
        return final_state, proposal
