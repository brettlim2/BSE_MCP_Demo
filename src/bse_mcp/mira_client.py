"""Async client for Meltwater's MIRA API.

MIRA is Meltwater's AI assistant grounded on Meltwater content. We expose a thin,
stateless passthrough:
  * POST /v3/mira/responses  -> ask a question, get a grounded answer + citations
  * GET  /v3/mira/projects   -> list grounding scopes (projects)

Auth is the server-owned Meltwater token sent as the `apikey` header. We never
persist responses; `thread_id` for multi-turn is handed back to the caller.

MIRA is rate-limited to 60 requests/minute, so we gate outbound calls with a
simple in-memory token bucket.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .config import Settings
from .http_util import find_value
from .models import Citation, MiraAnswer, MiraProject


class MiraError(RuntimeError):
    """Raised when MIRA is misconfigured or returns an error."""


class _RateLimiter:
    """Token bucket: `rate` permits per `per` seconds, refilled continuously."""

    def __init__(self, rate: int, per: float) -> None:
        self._capacity = rate
        self._tokens = float(rate)
        self._per = per
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated
            self._updated = now
            self._tokens = min(self._capacity, self._tokens + elapsed * (self._capacity / self._per))
            if self._tokens < 1:
                wait = (1 - self._tokens) * (self._per / self._capacity)
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


class MiraClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._limiter = _RateLimiter(rate=60, per=60.0)
        self._client = httpx.AsyncClient(
            base_url=settings.meltwater_base_url,
            timeout=settings.mira_timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _require_key(self) -> str:
        if not self._s.meltwater_api_key:
            raise MiraError(
                "MELTWATER_API_KEY is not configured on the server. "
                "Set it as an environment secret to enable MIRA tools."
            )
        return self._s.meltwater_api_key

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"apikey": self._require_key(), "Accept": "application/json"}
        if extra:
            headers.update({k: v for k, v in extra.items() if v})
        return headers

    async def ask(
        self,
        question: str,
        project_id: str | None = None,
        thread_id: str | None = None,
    ) -> MiraAnswer:
        await self._limiter.acquire()
        body = {
            "input": [{"role": "user", "content": [{"type": "text", "text": question}]}],
            "stream": False,
        }
        headers = self._headers({"Thread-ID": thread_id, "Project-ID": project_id})
        try:
            resp = await self._client.post("/v3/mira/responses", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise MiraError(
                f"MIRA responded {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise MiraError(
                f"MIRA timed out after {self._s.mira_timeout:.0f}s "
                f"({type(exc).__name__}). Grounded queries can be slow — raise MIRA_TIMEOUT."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MiraError(f"MIRA request failed: {type(exc).__name__}: {exc}") from exc

        answer_text = data.get("output_text") if isinstance(data, dict) else None
        if not answer_text:
            answer_text = _extract_output_text(data)

        citations = _extract_citations(data)
        returned_thread = resp.headers.get("Thread-ID") or (
            find_value(data, "thread_id", "threadId", "Thread-ID") if isinstance(data, (dict, list)) else None
        )
        return MiraAnswer(
            answer=(answer_text or "").strip(),
            citations=citations,
            thread_id=str(returned_thread) if returned_thread else thread_id,
        )

    async def list_projects(self) -> list[MiraProject]:
        await self._limiter.acquire()
        try:
            resp = await self._client.get("/v3/mira/projects", headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise MiraError(
                f"MIRA responded {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise MiraError(
                f"MIRA timed out after {self._s.mira_timeout:.0f}s "
                f"({type(exc).__name__}). Raise MIRA_TIMEOUT if this persists."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MiraError(f"MIRA request failed: {type(exc).__name__}: {exc}") from exc

        rows: list = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("projects", "data", "items"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
        projects: list[MiraProject] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = row.get("id") or row.get("project_id") or row.get("projectId")
            if pid is None:
                continue
            projects.append(MiraProject(id=str(pid), name=row.get("name") or row.get("title")))
        return projects


def _extract_output_text(data: object) -> str | None:
    """Fall back to concatenating text segments out of the `output` array."""
    if not isinstance(data, dict):
        return None
    out = data.get("output")
    if not isinstance(out, list):
        return None
    chunks: list[str] = []
    for message in out:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in (None, "text", "output_text"):
                    text = part.get("text")
                    if text:
                        chunks.append(str(text))
    return "\n".join(chunks) if chunks else None


def _extract_citations(data: object) -> list[Citation]:
    """Collect annotation objects from anywhere in the response.

    MIRA wraps each annotation in a typed envelope, e.g.
    ``{"news_document_citation": {"title": ..., "url": ..., "citation_type": ...}}``
    (also ``social_``/``generic_`` variants). We unwrap one level when the fields
    aren't directly on the annotation item.
    """
    citations: list[Citation] = []
    seen: set[tuple[str | None, str | None]] = set()

    def normalize(ann: dict) -> Citation | None:
        title = ann.get("title") or ann.get("name")
        url = ann.get("url") or ann.get("link")
        ctype = ann.get("type") or ann.get("source") or ann.get("citation_type")
        # Unwrap the typed envelope (news_document_citation, etc.) if needed.
        if not (title or url):
            for value in ann.values():
                if isinstance(value, dict) and (value.get("title") or value.get("url")):
                    title = value.get("title") or value.get("name")
                    url = value.get("url") or value.get("link")
                    ctype = value.get("citation_type") or value.get("type") or ctype
                    break
        if not (title or url):
            return None
        # "news_document_citation" -> "news"
        type_label = _str(ctype)
        if type_label:
            type_label = type_label.replace("_document_citation", "").replace("_citation", "")
        return Citation(title=_str(title), url=_str(url), type=type_label)

    def walk(node: object) -> None:
        if isinstance(node, dict):
            annotations = node.get("annotations")
            if isinstance(annotations, list):
                for ann in annotations:
                    if not isinstance(ann, dict):
                        continue
                    citation = normalize(ann)
                    if citation is None:
                        continue
                    key = (citation.title, citation.url)
                    if key not in seen:
                        seen.add(key)
                        citations.append(citation)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return citations


def _str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
