"""Jira Cloud source connector."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator

from nexus.ingest.models import ResourceRef

_DEFAULT_FIELDS = (
    "summary",
    "description",
    "status",
    "issuetype",
    "assignee",
    "reporter",
    "labels",
    "components",
    "created",
    "updated",
    "resolution",
    "parent",
    "issuelinks",
)
_ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


class JiraConfig(BaseModel):
    site_url: str
    email: str
    api_token: str
    jql: str = "ORDER BY updated DESC"
    max_results: int = Field(default=500, ge=1, le=10_000)
    page_size: int = Field(default=50, ge=1, le=100)
    fields: tuple[str, ...] = _DEFAULT_FIELDS

    @field_validator("site_url")
    @classmethod
    def _valid_site_url(cls, value: str) -> str:
        raw = value.strip().rstrip("/")
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            raise ValueError("jira site_url must use https")
        if not host.endswith(".atlassian.net"):
            raise ValueError("jira site_url must be a Jira Cloud .atlassian.net host")
        if parsed.username or parsed.password:
            raise ValueError("jira site_url must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("jira site_url must not contain query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("jira site_url must not contain a path")
        return raw


class JiraAPIError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, cfg: JiraConfig):
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.site_url.rstrip("/"),
            auth=(cfg.email, cfg.api_token),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=60,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search_issues(self) -> AsyncIterator[dict[str, Any]]:
        fetched = 0
        next_page_token: str | None = None
        page_size = max(1, min(self.cfg.page_size, 100))
        while fetched < self.cfg.max_results:
            remaining = self.cfg.max_results - fetched
            params: dict[str, Any] = {
                "jql": self.cfg.jql,
                "maxResults": min(page_size, remaining),
                "fields": ",".join(self.cfg.fields),
                "expand": "renderedFields",
            }
            if next_page_token:
                params["nextPageToken"] = next_page_token
            payload = await self._get_json("/rest/api/3/search/jql", params=params)
            issues = payload.get("issues") or []
            if not isinstance(issues, list):
                raise JiraAPIError("Jira API returned non-list issues")
            for issue in issues:
                if not isinstance(issue, dict):
                    raise JiraAPIError("Jira API returned non-object issue")
                yield issue
                fetched += 1
                if fetched >= self.cfg.max_results:
                    break
            next_page_token = payload.get("nextPageToken")
            if not next_page_token or not issues:
                break

    async def _get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise JiraAPIError(f"Jira API request failed: {e}") from e
        if resp.status_code >= 400:
            raise JiraAPIError(f"Jira API {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise JiraAPIError("Jira API returned invalid JSON") from e
        if not isinstance(data, dict):
            raise JiraAPIError("Jira API returned non-object JSON")
        return data


class JiraSource:
    def __init__(self, cfg: JiraConfig, *, client: JiraClient | None = None):
        self.cfg = cfg
        self.client = client or JiraClient(cfg)
        self.source_id = f"jira:{_site_key(cfg.site_url)}"
        self._issues: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def list_resources(self) -> AsyncIterator[ResourceRef]:
        async for issue in self.client.search_issues():
            key = str(issue.get("key") or "")
            if not key:
                continue
            self._issues[key] = issue
            fields = issue.get("fields") or {}
            yield ResourceRef(
                source_id=self.source_id,
                uri=f"jira:{_site_key(self.cfg.site_url)}:{key}",
                mime="text/x-jira-issue",
                size_bytes=None,
                last_modified=_string_or_none(fields.get("updated")),
            )

    async def read_resource(self, resource: ResourceRef) -> str:
        key = resource.uri.rsplit(":", 1)[-1]
        issue = self._issues.get(key)
        if issue is None:
            raise OSError(f"jira issue not cached: {key}")
        return render_issue(issue, site_url=self.cfg.site_url)


def jira_config_from_source(source: dict) -> JiraConfig:
    cfg = source.get("config") or {}
    site_url = str(cfg.get("site_url") or cfg.get("site") or "").strip()
    email = str(cfg.get("email") or "").strip()
    api_token = str(cfg.get("api_token") or cfg.get("token") or "").strip()
    if not site_url:
        raise ValueError("jira source missing config.site_url")
    if not email:
        raise ValueError("jira source missing config.email")
    if not api_token:
        raise ValueError("jira source missing config.api_token")
    fields = cfg.get("fields") or _DEFAULT_FIELDS
    return JiraConfig(
        site_url=site_url,
        email=email,
        api_token=api_token,
        jql=str(cfg.get("jql") or "ORDER BY updated DESC"),
        max_results=int(cfg.get("max_results") or cfg.get("maxResults") or 500),
        page_size=int(cfg.get("page_size") or cfg.get("pageSize") or 50),
        fields=tuple(str(f) for f in fields),
    )


def render_issue(issue: dict[str, Any], *, site_url: str) -> str:
    key = str(issue.get("key") or "")
    fields = issue.get("fields") or {}
    summary = _text(fields.get("summary"))
    status = _name(fields.get("status"))
    issue_type = _name(fields.get("issuetype"))
    assignee = _display_name(fields.get("assignee"))
    reporter = _display_name(fields.get("reporter"))
    parent = (fields.get("parent") or {}).get("key") if isinstance(fields.get("parent"), dict) else ""
    labels = ", ".join(str(label) for label in fields.get("labels") or [])
    components = ", ".join(_name(c) for c in fields.get("components") or [] if _name(c))
    linked_keys = sorted(_linked_issue_keys(fields))
    description = _adf_to_text(fields.get("description"))
    rendered = issue.get("renderedFields") or {}
    rendered_description = _text(rendered.get("description"))
    body = rendered_description or description
    metadata = {
        "key": key,
        "url": f"{site_url.rstrip('/')} /browse/{key}".replace(" /", "/"),
        "summary": summary,
        "status": status,
        "issue_type": issue_type,
        "assignee": assignee,
        "reporter": reporter,
        "parent": parent or "",
        "labels": labels,
        "components": components,
        "created": _string_or_none(fields.get("created")) or "",
        "updated": _string_or_none(fields.get("updated")) or "",
        "linked_keys": linked_keys,
        "mentioned_keys": sorted(set(_ISSUE_KEY_RE.findall(body)) - {key}),
    }
    lines = [
        json.dumps({"jira": metadata}, sort_keys=True),
        "",
        f"# {key} {summary}".strip(),
        "",
        f"Status: {status}",
        f"Type: {issue_type}",
        f"Assignee: {assignee}",
        f"Reporter: {reporter}",
    ]
    if parent:
        lines.append(f"Parent: {parent}")
    if labels:
        lines.append(f"Labels: {labels}")
    if components:
        lines.append(f"Components: {components}")
    if linked_keys:
        lines.append(f"Linked issues: {', '.join(linked_keys)}")
    lines.extend(["", body.strip()])
    return "\n".join(lines).strip() + "\n"


def _linked_issue_keys(fields: dict[str, Any]) -> set[str]:
    keys = set()
    parent = fields.get("parent")
    if isinstance(parent, dict) and parent.get("key"):
        keys.add(str(parent["key"]))
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        for side in ("inwardIssue", "outwardIssue"):
            issue = link.get(side)
            if isinstance(issue, dict) and issue.get("key"):
                keys.add(str(issue["key"]))
    return keys


def _adf_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = []
        text = value.get("text")
        if isinstance(text, str):
            parts.append(text)
        for child in value.get("content") or []:
            rendered = _adf_to_text(child)
            if rendered:
                parts.append(rendered)
        return " ".join(parts)
    if isinstance(value, list):
        return " ".join(part for item in value if (part := _adf_to_text(item)))
    return ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _name(value: Any) -> str:
    return str((value or {}).get("name") or "") if isinstance(value, dict) else ""


def _display_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("displayName") or value.get("emailAddress") or "")


def _string_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _site_key(site_url: str) -> str:
    parsed = urlparse(site_url)
    host = parsed.netloc or parsed.path
    return host.lower().strip("/")
