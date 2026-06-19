from __future__ import annotations

import json

import httpx
import pytest

from nexus.connectors.confluence import (
    ConfluenceClient,
    ConfluenceConfig,
    ConfluenceSource,
    render_page,
)

# ------------------------------------------------------------------ fixtures


def _space(space_id: str, key: str) -> dict:
    return {"id": space_id, "key": key, "name": f"Space {key}"}


def _page(page_id: str, title: str = "Getting Started") -> dict:
    body_adf = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello world content."}]}]}
    return {
        "id": page_id,
        "title": title,
        "version": {"number": 3, "createdAt": "2026-01-02T00:00:00.000Z"},
        "body": {
            "atlas_doc_format": {
                "value": json.dumps(body_adf),
                "representation": "atlas_doc_format",
            }
        },
    }


# ------------------------------------------------------------------ client tests


@pytest.mark.asyncio
async def test_confluence_client_paginates_spaces_and_pages() -> None:
    """Client follows _links.next for both spaces and pages."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path

        # Spaces endpoint — two pages of results
        if path.endswith("/spaces"):
            cursor = request.url.params.get("cursor")
            if cursor == "page2":
                return httpx.Response(200, json={"results": [_space("S2", "ENG")], "_links": {}})
            return httpx.Response(
                200,
                json={
                    "results": [_space("S1", "DOCS")],
                    "_links": {"next": "/wiki/api/v2/spaces?cursor=page2"},
                },
            )

        # Pages endpoint for S1
        if "/spaces/S1/pages" in path:
            cursor = request.url.params.get("cursor")
            if cursor == "p2":
                return httpx.Response(200, json={"results": [_page("P2", "Page Two")], "_links": {}})
            return httpx.Response(
                200,
                json={
                    "results": [_page("P1", "Page One")],
                    "_links": {"next": "/wiki/api/v2/spaces/S1/pages?cursor=p2"},
                },
            )

        # Pages endpoint for S2 — single page, no cursor
        if "/spaces/S2/pages" in path:
            return httpx.Response(200, json={"results": [_page("P3", "Page Three")], "_links": {}})

        return httpx.Response(404, json={})

    cfg = ConfluenceConfig(
        site_url="https://example.atlassian.net",
        email="me@example.com",
        api_token="tok",
        max_pages=10,
        page_size=1,
    )
    client = ConfluenceClient(cfg)
    # Replace the inner client with the mock transport.
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://example.atlassian.net/wiki/api/v2",
        auth=("me@example.com", "tok"),
        headers={"Accept": "application/json"},
    )

    try:
        spaces = [s async for s in client.list_spaces()]
        pages_s1 = [p async for p in client.list_pages("S1")]
        pages_s2 = [p async for p in client.list_pages("S2")]
    finally:
        await client.aclose()

    assert [s["key"] for s in spaces] == ["DOCS", "ENG"]
    assert [p["id"] for p in pages_s1] == ["P1", "P2"]
    assert [p["id"] for p in pages_s2] == ["P3"]


# ------------------------------------------------------------------ render tests


def test_render_page_includes_metadata_and_body() -> None:
    page = _page("42", "Architecture Overview")
    page["_space_key"] = "ENG"
    rendered = render_page(page, site_url="https://example.atlassian.net")

    assert '"id": "42"' in rendered
    assert '"space": "ENG"' in rendered
    assert "# Architecture Overview" in rendered
    assert "Hello world content." in rendered
    assert "example.atlassian.net" in rendered


def test_render_page_handles_missing_body() -> None:
    page = {"id": "99", "title": "Empty Page", "version": {}, "_space_key": "DOCS", "body": {}}
    rendered = render_page(page, site_url="https://example.atlassian.net")
    assert "# Empty Page" in rendered


# ------------------------------------------------------------------ source tests


@pytest.mark.asyncio
async def test_confluence_source_lists_and_reads_cached_page() -> None:
    class FakeClient:
        async def list_spaces(self):
            yield _space("S1", "DOCS")

        async def list_pages(self, space_id: str):
            assert space_id == "S1"
            yield _page("101", "My Page")

        async def aclose(self) -> None:
            pass

    source = ConfluenceSource(
        ConfluenceConfig(
            site_url="https://example.atlassian.net",
            email="me@example.com",
            api_token="tok",
        ),
        client=FakeClient(),
    )

    refs = [ref async for ref in source.list_resources()]
    assert len(refs) == 1
    assert refs[0].uri == "confluence:example.atlassian.net:101"
    assert refs[0].mime == "text/x-confluence-page"

    content = await source.read_resource(refs[0])
    assert "My Page" in content
    assert "Hello world content." in content


@pytest.mark.asyncio
async def test_confluence_source_filters_by_space_key() -> None:
    class FakeClient:
        async def list_spaces(self):
            yield _space("S1", "DOCS")
            yield _space("S2", "ENG")

        async def list_pages(self, space_id: str):
            if space_id == "S2":
                yield _page("201", "Eng Page")
            # S1 is filtered out — should not be called at all
            else:
                raise AssertionError(f"list_pages called for unexpected space {space_id}")

        async def aclose(self) -> None:
            pass

    source = ConfluenceSource(
        ConfluenceConfig(
            site_url="https://example.atlassian.net",
            email="me@example.com",
            api_token="tok",
            space_keys=("ENG",),
        ),
        client=FakeClient(),
    )

    refs = [ref async for ref in source.list_resources()]
    assert len(refs) == 1
    assert refs[0].uri == "confluence:example.atlassian.net:201"
