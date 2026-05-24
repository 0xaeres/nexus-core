"""Nexus CLI — Typer entry point."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

app = typer.Typer(
    name="nexus",
    help="Sovereign, MCP-native skill server for codebases.",
    no_args_is_help=True,
    add_completion=False,
)

council_app = typer.Typer(help="LLM council commands.", no_args_is_help=True)
app.add_typer(council_app, name="council")


# ---------------------------------------------------------------- init


@app.command()
def init(
    config_path: Path = typer.Option(
        Path("nexus.yaml"), "--config", "-c", help="Where to write the config."
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing config."),
) -> None:
    """Interactive setup — writes nexus.yaml. (Slice 1)"""
    if config_path.exists() and not force:
        typer.secho(
            f"{config_path} already exists. Pass --force to overwrite.", fg=typer.colors.RED
        )
        raise typer.Exit(code=1)
    typer.echo("nexus init — not yet implemented (Slice 1 follow-on).")
    typer.echo(f"For now: `cp nexus.yaml.example {config_path}` and edit by hand.")


# ---------------------------------------------------------------- ingest


@app.command()
def ingest(
    product: str = typer.Option(..., "--product", "-p", help="Product ID to ingest."),
    path: Path = typer.Option(..., "--path", help="Local directory to ingest (Slice 1)."),
    config_path: Path = typer.Option(Path("nexus.yaml"), "--config", "-c"),
    no_enrich: bool = typer.Option(False, "--no-enrich", help="Skip contextual enrichment."),
) -> None:
    """Pull resources, chunk, embed, index. (Slice 1: local-fs source)"""
    from nexus.config import NexusConfig
    from nexus.connectors.local_fs import LocalFsConfig, LocalFsSource
    from nexus.ingest.pipeline import run_ingest

    config = NexusConfig.load(config_path)
    if not path.exists() or not path.is_dir():
        typer.secho(f"{path} is not a directory.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    source = LocalFsSource(LocalFsConfig(root=path))
    typer.echo(f"Ingesting from {path.resolve()} into product '{product}'…")
    stats = asyncio.run(
        run_ingest(product_id=product, source=source, config=config, enrich=not no_enrich)
    )
    typer.echo(
        f"resources: seen={stats.resources_seen} "
        f"indexed={stats.resources_indexed} "
        f"skipped={stats.resources_skipped}"
    )
    typer.echo(
        f"chunks:    produced={stats.chunks_produced} indexed={stats.chunks_indexed}"
    )


# ---------------------------------------------------------------- query


@app.command()
def query(
    text: str = typer.Argument(..., help="Query string."),
    product: str = typer.Option(..., "--product", "-p"),
    top_k: int = typer.Option(10, "--top-k", "-k"),
    mode: str = typer.Option(
        "auto", "--mode", help="auto | code | text — which named vector(s) to search."
    ),
    config_path: Path = typer.Option(Path("nexus.yaml"), "--config", "-c"),
) -> None:
    """Run the 5-stage GraphRAG retrieval pipeline."""
    from nexus.config import NexusConfig
    from nexus.retrieval.pipeline import RetrievalContext, retrieve

    config = NexusConfig.load(config_path)

    async def _go():
        ctx = RetrievalContext.from_config(config)
        try:
            return await retrieve(
                ctx=ctx,
                product_id=product,
                query=text,
                top_k=top_k,
                mode=mode,  # type: ignore[arg-type]
            )
        finally:
            await ctx.aclose()

    result = asyncio.run(_go())

    if result.mode == "no_context":
        typer.secho("No relevant context found (quality gate).", fg=typer.colors.YELLOW)
        return
    if result.cache_hit:
        typer.secho("(cache hit)", fg=typer.colors.GREEN)
    if result.degraded_components:
        typer.secho(
            f"(degraded: {','.join(result.degraded_components)})", fg=typer.colors.YELLOW
        )

    for i, hit in enumerate(result.hits, start=1):
        payload = hit.payload or {}
        anchor = f'{payload.get("resource_uri","?")}:{payload.get("start_line","?")}'
        ctx_path = payload.get("context_path") or ""
        typer.echo(
            f"{i:>2}. [{hit.score:.3f}] {hit.source:<10} {anchor}"
            + (f"  ({ctx_path})" if ctx_path else "")
        )
        body = (payload.get("content") or "").strip().splitlines()
        for line in body[:3]:
            typer.echo(f"      {line[:120]}")
        if len(body) > 3:
            typer.echo(f"      … (+{len(body)-3} lines)")


# ---------------------------------------------------------------- council


@council_app.command("draft")
def council_draft(
    topic: str = typer.Option(..., "--topic", "-t"),
    product: str = typer.Option(..., "--product", "-p"),
    config_path: Path = typer.Option(Path("nexus.yaml"), "--config", "-c"),
) -> None:
    """Run the LLM Council to draft a skill proposal."""
    import uuid as _uuid
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from nexus.config import NexusConfig
    from nexus.council.graph import run_council
    from nexus.council.queue import ProposalQueue
    from nexus.council.state import initial_state

    config = NexusConfig.load(config_path)
    queue = ProposalQueue(config.storage.proposal_queue)

    session_id = f"cs_{_dt.now(_UTC).strftime('%Y%m%d_%H%M%S')}_{_uuid.uuid4().hex[:6]}"
    started_at = _dt.now(_UTC).isoformat()
    typer.echo(f"Council session {session_id} starting…")
    typer.echo(f"  topic   : {topic}")
    typer.echo(f"  product : {product}")

    initial = initial_state(
        session_id=session_id,
        product_id=product,
        topic=topic,
        config_path=str(config_path),
    )

    async def _go():
        return await run_council(
            config=config,
            session_id=session_id,
            initial=initial,
            checkpoint_db=config.storage.council_checkpoint,
        )

    final_state, proposal = asyncio.run(_go())

    deliberation = [m.model_dump() if hasattr(m, "model_dump") else m for m in final_state.get("deliberation", [])]
    costs = [c.model_dump() if hasattr(c, "model_dump") else c for c in final_state.get("costs", [])]

    if proposal is None:
        typer.secho("Council produced no proposal.", fg=typer.colors.YELLOW)
        return

    queue.enqueue(
        proposal,
        session_id=session_id,
        product_id=product,
        deliberation=deliberation,
        costs=costs,
    )
    queue.record_session(
        session_id=session_id,
        product_id=product,
        topic=topic,
        proposal_id=proposal.id,
        deliberation=deliberation,
        costs=costs,
        started_at=started_at,
        completed_at=_dt.now(_UTC).isoformat(),
    )

    total_prompt = sum(c.get("prompt_tokens", 0) for c in costs)
    total_completion = sum(c.get("completion_tokens", 0) for c in costs)
    typer.echo("")
    typer.secho(
        f"✓ proposal {proposal.id} pending at http://localhost:3000",
        fg=typer.colors.GREEN,
    )
    typer.echo(
        f"  name        : {proposal.name}\n"
        f"  confidence  : {proposal.confidence:.2f}\n"
        f"  citations   : {len(proposal.citations)}\n"
        f"  tokens      : prompt={total_prompt}, completion={total_completion}"
    )


# ---------------------------------------------------------------- daemon


@app.command()
def daemon(
    product: str = typer.Option(..., "--product", "-p", help="Product ID to ingest into."),
    config_path: Path = typer.Option(Path("nexus.yaml"), "--config", "-c"),
    bootstrap: bool = typer.Option(
        True, "--bootstrap/--no-bootstrap", help="Run a full sync on startup."
    ),
) -> None:
    """Continuous index daemon: subscribes to all `watch: true` connectors."""
    import logging as _logging

    from nexus.config import NexusConfig
    from nexus.daemon import run_daemon

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = NexusConfig.load(config_path)
    typer.echo(f"nexus daemon — product={product} bootstrap={bootstrap}")
    try:
        asyncio.run(run_daemon(config=cfg, product_id=product, bootstrap=bootstrap))
    except KeyboardInterrupt:
        typer.echo("\ndaemon stopped.")


# ---------------------------------------------------------------- version


@app.command()
def version() -> None:
    """Print the installed version."""
    try:
        from importlib.metadata import version as _v

        typer.echo(_v("nexus"))
    except Exception:
        typer.echo("0.0.1-dev")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
