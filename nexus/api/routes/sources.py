"""Source connectors — see ENGINEERING.md §11.

Sources come from two places:
1. `nexus.yaml` `connectors:` block (declarative, baked in at deploy time).
2. The runtime registry (added via the UI; persists across restarts).

The list endpoint merges both; the registry wins on name conflicts so user-added
config can override the declarative defaults.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from nexus.api.deps import get_config_dep, get_registry
from nexus.config import NexusConfig
from nexus.registry import Registry

router = APIRouter(prefix="/products/{product_id}/sources", tags=["sources"])


_SECRET_KEY_HINTS = ("token", "api_key", "password", "secret")


def _redact(d: dict) -> dict:
    out: dict = {}
    for k, v in d.items():
        if any(s in k.lower() for s in _SECRET_KEY_HINTS):
            out[k] = "***" if v else ""
        else:
            out[k] = v
    return out


def _config_sources(config: NexusConfig, product_id: str) -> list[dict]:
    out: list[dict] = []
    for c in config.connectors:
        extras = c.model_dump(exclude={"name", "type", "watch"})
        out.append({
            "id": c.name,
            "product": product_id,
            "name": c.name,
            "type": c.type,
            "status": "watching" if c.watch else "connected",
            "lastSync": None,
            "resourceCount": 0,
            "config": _redact(extras),
        })
    return out


@router.get("")
async def list_sources(
    product_id: str,
    config: NexusConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> dict:
    by_name = {s["name"]: s for s in _config_sources(config, product_id)}
    for s in registry.list_sources(product_id):
        s["config"] = _redact(s.get("config") or {})
        by_name[s["name"]] = s
    return {"sources": list(by_name.values())}


@router.get("/{source_id}")
async def get_source(
    source_id: str,
    product_id: str,
    config: NexusConfig = Depends(get_config_dep),
    registry: Registry = Depends(get_registry),
) -> dict:
    runtime = registry.get_source(product_id, source_id)
    if runtime:
        runtime["config"] = _redact(runtime.get("config") or {})
        return runtime
    for s in _config_sources(config, product_id):
        if s["name"] == source_id:
            return s
    raise HTTPException(status_code=404, detail="source not found")


@router.post("")
async def add_source(
    product_id: str,
    name: str = Body(..., embed=True),
    type: str = Body(..., embed=True),
    config_block: dict = Body(default_factory=dict, embed=True, alias="config"),
    registry: Registry = Depends(get_registry),
) -> dict:
    if registry.get_source(product_id, name):
        raise HTTPException(status_code=409, detail=f"source {name!r} already exists")
    registry.upsert_source(
        {
            "product": product_id,
            "name": name,
            "type": type,
            "status": "connected",
            "config": config_block,
            "resourceCount": 0,
        }
    )
    out = registry.get_source(product_id, name) or {}
    out["config"] = _redact(out.get("config") or {})
    return out


@router.delete("/{source_id}")
async def delete_source(
    product_id: str, source_id: str, registry: Registry = Depends(get_registry)
) -> dict:
    if not registry.delete_source(product_id, source_id):
        raise HTTPException(status_code=404, detail="source not found in registry")
    return {"ok": True}


@router.post("/{source_id}/sync")
async def sync_source(product_id: str, source_id: str) -> dict:
    # Hooking into the daemon's manual sync path is Slice 5+ daemon work.
    # For now we acknowledge the request so the UI can show a spin state.
    return {"ok": True, "queued": True, "product": product_id, "source": source_id}
