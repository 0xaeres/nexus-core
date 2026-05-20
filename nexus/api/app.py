"""FastAPI application entry point."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nexus.api.routes import (
    activity,
    council,
    dashboard,
    org_library,
    products,
    proposals,
    skills,
    sources,
    webhooks,
)

app = FastAPI(
    title="Nexus API",
    description="Backend for the Nexus skill server. See ENGINEERING.md §11.",
    version="0.0.1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(products.router)
app.include_router(dashboard.router)
app.include_router(sources.router)
app.include_router(council.router)
app.include_router(skills.router)
app.include_router(proposals.router)
app.include_router(org_library.router)
app.include_router(activity.router)
app.include_router(webhooks.router)
