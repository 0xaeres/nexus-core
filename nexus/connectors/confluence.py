"""Confluence Cloud source connector.

Fetches pages from one or more Confluence Cloud spaces via the REST API v2
and presents them as ``ResourceRef`` entries for the ingest pipeline.

Authentication: Atlassian Basic Auth (email + API token).
API: https://<site>.atlassian.net/wiki/api/v2
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from nexus.ingest.models import ResourceRef


@dataclass(frozen=True)
class ConfluenceConfig:
    site_url: str  # e.g. https://myorg.atlassian.net
    email: str
    api_token: str
    # Restrict to specific space keys (e.g. ["DOCS", "ENG"]).
    # Empty list means all spaces the token can read.
    space_keys: tuple[str, ...] = ()
    max_pages: int = 1000
    page_size: int = 50


class ConfluenceAPIError(RuntimeError):
    pass


class ConfluenceClient:
    """Thin async wrapper around the Confluence Cloud v2 REST API."""

    def __init__(self, cfg: ConfluenceConfig) -> None:
        self.cfg = cfg
        base = cfg.site_url.rstrip("/") + "/wiki/api/v2"
        self._client = httpx.AsyncClient(
            base_url=base,
            auth=(cfg.email, cfg.api_token),
            headers={"Accept": "application/json"},
            timeout=60,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_spaces(self) -> AsyncIterator[dict[str, Any]]:
        """Yield each space dict the token can read."""
        url: str | None = "/spaces"
        while url:
            params = {"limit": 50} if "?" not in url else None
            data = await self._get_json(url, params=params)
            for space in data.get("results") or []:
                yield space
            url = _next_link(data, self.cfg.site_url)

    async def list_pages(self, space_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield page dicts (with body) for a given numeric space ID."""
        url: str | None = f"/spaces/{space_id}/pages"
        fetched = 0
        page_size = max(1, min(self.cfg.page_size, 50))
        while url and fetched < self.cfg.max_pages:
            remaining = self.cfg.max_pages - fetched
            params = (
                {
                    "body-format": "atlas_doc_format",
                    "limit": min(page_size, remaining),
                }
                if "?" not in url
                else None
            )
            data = await self._get_json(
                url,
                params=params,
            )
            for page in data.get("results") or []:
                yield page
                fetched += 1
                if fetched >= self.cfg.max_pages:
                    break
            url = _next_link(data, self.cfg.site_url)

    async def _get_json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        resp = await self._client.get(path, params=params)
        if resp.status_code >= 400:
            raise ConfluenceAPIError(
                f"Confluence API {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise ConfluenceAPIError("Confluence API returned non-object JSON")
        return data


class ConfluenceSource:
    """Ingest source that fetches Confluence Cloud pages."""

    def __init__(
        self,
        cfg: ConfluenceConfig,
        *,
        client: ConfluenceClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client or ConfluenceClient(cfg)
        self.source_id = f"confluence:{_site_key(cfg.site_url)}"
        self._pages: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def list_resources(self) -> AsyncIterator[ResourceRef]:
        site_key = _site_key(self.cfg.site_url)
        space_filter = {k.upper() for k in self.cfg.space_keys}

        async for space in self.client.list_spaces():
            key = str(space.get("key") or "")
            if space_filter and key.upper() not in space_filter:
                continue
            space_id = str(space.get("id") or "")
            if not space_id:
                continue
            async for page in self.client.list_pages(space_id):
                page_id = str(page.get("id") or "")
                if not page_id:
                    continue
                # Attach space metadata for rendering
                page["_space_key"] = key
                self._pages[page_id] = page
                version = (page.get("version") or {}).get("createdAt")
                yield ResourceRef(
                    source_id=self.source_id,
                    uri=f"confluence:{site_key}:{page_id}",
                    mime="text/x-confluence-page",
                    size_bytes=None,
                    last_modified=version,
                )

    async def read_resource(self, resource: ResourceRef) -> str:
        page_id = resource.uri.rsplit(":", 1)[-1]
        page = self._pages.get(page_id)
        if page is None:
            raise OSError(f"confluence page not cached: {page_id}")
        return render_page(page, site_url=self.cfg.site_url)


def confluence_config_from_source(source: dict) -> ConfluenceConfig:
    cfg = source.get("config") or {}
    site_url = str(cfg.get("site_url") or cfg.get("site") or "").strip()
    email = str(cfg.get("email") or "").strip()
    api_token = str(cfg.get("api_token") or cfg.get("token") or "").strip()
    if not site_url:
        raise ValueError("confluence source missing config.site_url")
    if not email:
        raise ValueError("confluence source missing config.email")
    if not api_token:
        raise ValueError("confluence source missing config.api_token")

    raw_keys = cfg.get("space_keys") or []
    if isinstance(raw_keys, str):
        raw_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    space_keys = tuple(str(k).strip().upper() for k in raw_keys if k)

    return ConfluenceConfig(
        site_url=site_url,
        email=email,
        api_token=api_token,
        space_keys=space_keys,
        max_pages=int(cfg.get("max_pages") or cfg.get("maxPages") or 1000),
        page_size=int(cfg.get("page_size") or cfg.get("pageSize") or 50),
    )


def render_page(page: dict[str, Any], *, site_url: str) -> str:
    """Render a Confluence page dict to an indexable plain-text document."""
    page_id = str(page.get("id") or "")
    title = str(page.get("title") or "").strip()
    space_key = str(page.get("_space_key") or "")
    version_num = (page.get("version") or {}).get("number") or ""
    created_at = (page.get("version") or {}).get("createdAt") or ""
    page_url = (
        f"{site_url.rstrip('/')}/wiki/spaces/{space_key}/pages/{page_id}"
    )

    body_obj = (page.get("body") or {}).get("atlas_doc_format") or {}
    body_value = body_obj.get("value") or ""
    # body_value is a JSON-encoded ADF document; decode and extract text.
    try:
        adf = json.loads(body_value) if isinstance(body_value, str) else body_value
    except (ValueError, TypeError):
        adf = {}
    body_text = _adf_to_text(adf).strip()

    metadata = {
        "id": page_id,
        "title": title,
        "url": page_url,
        "space": space_key,
        "version": version_num,
        "updated": created_at,
    }
    lines = [
        json.dumps({"confluence": metadata}, sort_keys=True),
        "",
        f"# {title}".strip(),
        "",
        f"Space: {space_key}",
        f"Version: {version_num}",
        f"URL: {page_url}",
        "",
        body_text,
    ]
    return "\n".join(lines).strip() + "\n"


# ------------------------------------------------------------------ helpers


def _adf_to_text(value: Any) -> str:
    """Recursively extract plain text from an Atlassian Document Format node."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts: list[str] = []
        text = value.get("text")
        if isinstance(text, str):
            parts.append(text)
        for child in value.get("content") or []:
            rendered = _adf_to_text(child)
            if rendered:
                parts.append(rendered)
        return " ".join(parts)
    if isinstance(value, list):
        return " ".join(
            part for item in value if (part := _adf_to_text(item))
        )
    return ""


def _site_key(site_url: str) -> str:
    parsed = urlparse(site_url)
    host = parsed.netloc or parsed.path
    return host.lower().strip("/")


def _next_link(data: dict[str, Any], site_url: str) -> str | None:
    """Extract the next-page URL from a v2 response ``_links`` block."""
    links = data.get("_links") or {}
    nxt = links.get("next")
    if not nxt:
        return None
    if nxt.startswith("http"):
        return nxt
    if nxt.startswith("/"):
        return nxt
    # Relative path — prepend the wiki base.
    base = site_url.rstrip("/") + "/wiki/api/v2"
    return f"{base}/{nxt.lstrip('/')}"
