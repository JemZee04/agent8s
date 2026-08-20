from __future__ import annotations

import html
import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config

TIMEOUT_SECONDS = 15
EXCERPT_LIMIT = 800


class AtlassianError(RuntimeError):
    pass


@dataclass
class ConfluencePage:
    id: str
    title: str
    url: str
    excerpt: str


@dataclass
class JiraIssue:
    key: str
    summary: str
    description: str
    status: str
    issue_type: str
    url: str
    confluence_pages: list[ConfluencePage] = field(default_factory=list)

    def as_context_text(self) -> str:
        lines = [
            f"Jira {self.key} [{self.status}] ({self.issue_type}): {self.summary}",
            self.url,
            "",
            self.description or "(no description)",
        ]
        if self.confluence_pages:
            lines.append("")
            lines.append("Related Confluence pages:")
            for page in self.confluence_pages:
                lines.append(f"- {page.title} ({page.url})")
                if page.excerpt:
                    lines.append(f"  {page.excerpt}")
        return "\n".join(lines)


def _request_json(url: str, token: str, verify_ssl: bool) -> Any:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise AtlassianError(f"{url} -> HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise AtlassianError(f"{url} -> {e.reason}") from e


def _strip_markup(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _confluence_page_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"pageId=(\d+)", url) or re.search(r"/pages/(\d+)", url)
    return m.group(1) if m else None


def _fetch_confluence_page(config: Config, page_id: str) -> Optional[ConfluencePage]:
    if not (config.confluence_url and config.confluence_token):
        return None
    url = f"{config.confluence_url.rstrip('/')}/rest/api/content/{page_id}?expand=body.storage"
    try:
        data = _request_json(url, config.confluence_token, config.confluence_verify_ssl)
    except AtlassianError:
        return None
    storage = data.get("body", {}).get("storage", {}).get("value", "")
    return ConfluencePage(
        id=page_id,
        title=data.get("title", page_id),
        url=f"{config.confluence_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}",
        excerpt=_strip_markup(storage)[:EXCERPT_LIMIT],
    )


def _fetch_related_confluence_pages(config: Config, key: str) -> list[ConfluencePage]:
    if not (config.confluence_url and config.confluence_token):
        return []
    url = f"{config.jira_url.rstrip('/')}/rest/api/2/issue/{key}/remotelink"
    try:
        links = _request_json(url, config.jira_token, config.jira_verify_ssl)
    except AtlassianError:
        return []

    pages: list[ConfluencePage] = []
    for link in links or []:
        link_url = link.get("object", {}).get("url", "")
        if config.confluence_url.rstrip("/") not in link_url:
            continue
        page_id = _confluence_page_id_from_url(link_url)
        if not page_id:
            continue
        page = _fetch_confluence_page(config, page_id)
        if page:
            pages.append(page)
    return pages


def fetch_issue(config: Config, key: str) -> JiraIssue:
    if not config.jira_configured:
        raise AtlassianError("Jira is not configured (JIRA_URL / JIRA_PERSONAL_TOKEN)")

    url = f"{config.jira_url.rstrip('/')}/rest/api/2/issue/{key}"
    data = _request_json(url, config.jira_token, config.jira_verify_ssl)
    fields = data.get("fields", {})

    return JiraIssue(
        key=data.get("key", key),
        summary=fields.get("summary", ""),
        description=fields.get("description", "") or "",
        status=(fields.get("status") or {}).get("name", "?"),
        issue_type=(fields.get("issuetype") or {}).get("name", "?"),
        url=f"{config.jira_url.rstrip('/')}/browse/{data.get('key', key)}",
        confluence_pages=_fetch_related_confluence_pages(config, key),
    )
