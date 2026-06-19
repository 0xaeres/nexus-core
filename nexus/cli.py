"""Nexus CLI — Typer entry point."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

app = typer.Typer(
    name="nexus",
    help="Sovereign, MCP-native context engine for codebases.",
    no_args_is_help=True,
    add_completion=False,
)

council_app = typer.Typer(help="LLM council commands.", no_args_is_help=True)
app.add_typer(council_app, name="council")

eval_app = typer.Typer(help="Evaluation harness commands.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")


def _echo_delete_report(report, *, dry_run: bool) -> None:
    prefix = "Would delete" if dry_run else "Deleted"
    typer.echo(f"{prefix} product '{report.product_id}':")
    typer.echo(
        "  registry   : "
        f"{report.registry.get('products', 0)} product, "
        f"{report.registry.get('sources', 0)} sources, "
        f"{report.registry.get('source_resources', 0)} manifests, "
        f"{report.registry.get('source_sync_runs', 0)} sync runs"
    )
    typer.echo(
        "  council    : "
        f"{report.queue.get('proposals', 0)} proposals, "
        f"{report.queue.get('sessions', 0)} sessions, "
        f"{report.checkpoints} checkpoints"
    )
    typer.echo(f"  skills     : {report.skills} files")
    if report.index:
        index_counts = ", ".join(
            f"{collection}={count}" for collection, count in report.index.items()
        )
        typer.echo(f"  index      : {index_counts}")
    else:
        typer.echo("  index      : skipped")
    typer.echo(f"  repomap    : {1 if report.repomap_deleted else 0} file")


# ---------------------------------------------------------------- init


@app.command()
def init(
    config_path: Path = typer.Option(
        Path("nexus.yaml"), "--config", "-c", help="Where to write the config."
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing config."),
) -> None:
    """Interactive setup — writes nexus.yaml."""
    if config_path.exists() and not force:
        typer.secho(
            f"{config_path} already exists. Pass --force to overwrite.", fg=typer.colors.RED
        )
        raise typer.Exit(code=1)
    typer.echo("nexus init — not yet implemented.")
    typer.echo(f"For now: `cp nexus.yaml.example {config_path}` and edit by hand.")


@app.command("delete-product")
def delete_product_cmd(
    product: str = typer.Option(..., "--product", "-p", help="Product ID to delete."),
    config_path: Path = typer.Option(Path("nexus.yaml"), "--config", "-c"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Actually delete. Default is dry-run."),
    skip_qdrant: bool = typer.Option(False, "--skip-qdrant", help="Skip index cleanup."),
) -> None:
    """Delete one product and all product-scoped local/index state."""
    from nexus.config import NexusConfig
    from nexus.tools.delete_product import delete_product

    config = NexusConfig.load(config_path)
    try:
        report = asyncio.run(
            delete_product(
                product_id=product,
                config=config,
                dry_run=not yes,
                skip_qdrant=skip_qdrant,
            )
        )
    except Exception as e:
        typer.secho(f"delete failed: {type(e).__name__}: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from e

    _echo_delete_report(report, dry_run=not yes)
    if not yes:
        typer.echo("")
        typer.secho("Dry run only. Re-run with --yes to delete.", fg=typer.colors.YELLOW)


# ---------------------------------------------------------------- ingest


@app.command()
def ingest(
    product: str = typer.Option(..., "--product", "-p", help="Product ID to ingest."),
    path: Path = typer.Option(..., "--path", help="Local directory to ingest."),
    config_path: Path = typer.Option(Path("nexus.yaml"), "--config", "-c"),
    no_enrich: bool = typer.Option(False, "--no-enrich", help="Skip contextual enrichment."),
) -> None:
    """Pull resources, chunk, embed, index from a local filesystem source."""
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
    """Run the hybrid retrieval pipeline."""
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

    if not result.hits:
        typer.secho("No relevant context found (quality gate).", fg=typer.colors.YELLOW)
        return
    if not result.reranked:
        typer.secho("(reranker unavailable; showing fused order)", fg=typer.colors.YELLOW)

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


# ---------------------------------------------------------------- eval


@eval_app.command("run")
def eval_run(
    suite: str = typer.Option(
        "all",
        "--suite",
        "-s",
        help="all, retrieval, rag, code, or a comma-separated list.",
    ),
    config_path: Path = typer.Option(Path("nexus.yaml"), "--config", "-c"),
    out_dir: Path = typer.Option(Path("artifacts/evals"), "--out-dir"),
    product: str | None = typer.Option(
        None,
        "--product",
        "-p",
        help="Override the suite default product id.",
    ),
    fixture: Path | None = typer.Option(
        None,
        "--fixture",
        help="Override the suite default fixture directory.",
    ),
    ingest_fixture: bool = typer.Option(
        True,
        "--ingest-fixture/--no-ingest-fixture",
        help="Ingest the suite fixture before scoring.",
    ),
    golden: Path = typer.Option(Path("evals/golden.jsonl"), "--golden"),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Limit RAG/code judge items for smoke runs.",
    ),
    top_k: int = typer.Option(10, "--top-k", help="Retrieval top-k."),
) -> None:
    """Run production eval suites and write JSON/Markdown artifacts."""
    from evals.harness import parse_suites, render_markdown_summary, run_suites
    from nexus.config import NexusConfig

    try:
        suites = parse_suites(suite)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(code=1) from e

    config = NexusConfig.load(config_path)
    try:
        artifact = asyncio.run(
            run_suites(
                suites=suites,
                config=config,
                config_path=config_path,
                out_dir=out_dir,
                product_id=product,
                fixture_path=fixture,
                ingest_fixture=ingest_fixture,
                golden_path=golden,
                limit=limit,
                top_k=top_k,
            )
        )
    except Exception as e:
        typer.secho(f"eval failed: {type(e).__name__}: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from e

    typer.echo(render_markdown_summary(artifact))
    if not artifact.passed:
        raise typer.Exit(code=1)


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
