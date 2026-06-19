from __future__ import annotations

import httpx
import pytest

from nexus.connectors.jira import JiraClient, JiraConfig, JiraSource, render_issue


def _issue(key: str, *, summary: str = "Token work") -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Story"},
            "assignee": {"displayName": "Ava Owner"},
            "reporter": {"displayName": "Rae Reporter"},
            "labels": ["auth"],
            "components": [{"name": "api"}],
            "created": "2026-01-01T00:00:00.000+0000",
            "updated": "2026-01-02T00:00:00.000+0000",
            "parent": {"key": "AUTH-1"},
            "issuelinks": [{"outwardIssue": {"key": "AUTH-3"}}],
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Touch shared/auth.py"}],
                    }
                ],
            },
        },
    }


@pytest.mark.asyncio
async def test_jira_client_uses_enhanced_jql_pagination() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        token = request.url.params.get("nextPageToken")
        if token:
            return httpx.Response(200, json={"issues": [_issue("AUTH-2")]})
        return httpx.Response(
            200,
            json={"issues": [_issue("AUTH-1")], "nextPageToken": "next"},
        )

    cfg = JiraConfig(
        site_url="https://example.atlassian.net",
        email="me@example.com",
        api_token="tok",
        jql="project = AUTH",
        max_results=2,
        page_size=1,
    )
    client = JiraClient(cfg)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=cfg.site_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        issues = [issue async for issue in client.search_issues()]
    finally:
        await client.aclose()

    assert [issue["key"] for issue in issues] == ["AUTH-1", "AUTH-2"]
    assert requests[0].url.path == "/rest/api/3/search/jql"
    assert requests[0].url.params["jql"] == "project = AUTH"
    assert requests[1].url.params["nextPageToken"] == "next"


def test_render_issue_includes_metadata_and_adf_text() -> None:
    rendered = render_issue(_issue("AUTH-2"), site_url="https://example.atlassian.net")

    assert '"key": "AUTH-2"' in rendered
    assert "# AUTH-2 Token work" in rendered
    assert "Assignee: Ava Owner" in rendered
    assert "Linked issues: AUTH-1, AUTH-3" in rendered
    assert "Touch shared/auth.py" in rendered


@pytest.mark.asyncio
async def test_jira_source_lists_and_reads_cached_issue() -> None:
    class FakeClient:
        async def search_issues(self):
            yield _issue("AUTH-2")

        async def aclose(self) -> None:
            pass

    source = JiraSource(
        JiraConfig(
            site_url="https://example.atlassian.net",
            email="me@example.com",
            api_token="tok",
        ),
        client=FakeClient(),
    )

    refs = [ref async for ref in source.list_resources()]
    content = await source.read_resource(refs[0])

    assert refs[0].uri == "jira:example.atlassian.net:AUTH-2"
    assert refs[0].mime == "text/x-jira-issue"
    assert "AUTH-2" in content
