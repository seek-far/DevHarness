#!/usr/bin/env python3
"""Build background-knowledge web-search packets for SWE-bench instances.

The script loads the SWE-bench Verified test split by default, asks an LLM what
non-code background/domain knowledge would be useful for each problem
statement, executes those searches through a configured web-search provider,
and writes one JSON object per instance to a JSONL file.

Selection examples:

    python trial/swebench_background_search.py sympy__sympy-20590
    python trial/swebench_background_search.py --instances id1,id2
    python trial/swebench_background_search.py --slice 0:10
    python trial/swebench_background_search.py --all

Search provider examples:

    python trial/swebench_background_search.py --provider duckduckgo_html --slice 0:10
    TAVILY_API_KEY=... python trial/swebench_background_search.py --slice 0:10
    BRAVE_SEARCH_API_KEY=... python trial/swebench_background_search.py --provider brave --slice 0:10
    SERPER_API_KEY=... python trial/swebench_background_search.py --provider serper --slice 0:10

When no API key is available, --provider auto falls back to DuckDuckGo's HTML
search page. This is less stable than an official search API, but it gives real
web results without requiring a key. Use --fetch-pages to fetch text from the
top result pages and include it in the LLM-generated summary.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests


DEFAULT_ENV_FILE = Path("settings/worker_local_multi_process.env")
DEFAULT_OUTPUT_DIR = Path("trial")

# Keep this local so the trial tool does not import bf_worker.swebench_single,
# which imports the mini-swe-agent adapter and its heavy optional deps.
DATASET_MAPPING = {
    "full": "princeton-nlp/SWE-Bench",
    "verified": "princeton-nlp/SWE-Bench_Verified",
    "lite": "princeton-nlp/SWE-Bench_Lite",
    "multimodal": "princeton-nlp/SWE-Bench_Multimodal",
    "multilingual": "swe-bench/SWE-Bench_Multilingual",
    "_test": "klieret/swe-bench-dummy-test-dataset",
}

CODE_QUERY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(github|gitlab|bitbucket)\b", re.I), "repository host"),
    (re.compile(r"\b(source\s+code|code\s+search|implementation|diff|patch)\b", re.I), "code artifact"),
    (re.compile(r"\b(issue|pull\s+request|pr|commit|changeset)\s*#?\d+\b", re.I), "issue/pr/commit"),
    (re.compile(r"\bSWE-?bench\b", re.I), "SWE-bench leakage"),
    (re.compile(r"\bstack\s*overflow\b|\bstackoverflow\b", re.I), "code Q&A"),
    (re.compile(r"`[^`]+`"), "verbatim code token"),
    (re.compile(r"[\w./-]+\.(py|pyx|js|jsx|ts|tsx|java|go|rs|c|cc|cpp|h|hpp|rb|php)\b", re.I), "file path"),
    (re.compile(r"\b(test|tests|fixture|traceback|assertionerror|pytest)\b", re.I), "test/debug artifact"),
)

BLOCKED_RESULT_HOSTS = (
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "stackoverflow.com",
    "stackexchange.com",
)


_WRITE_LOCK = threading.Lock()


def parse_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("'").strip('"')
    return env


def normalize_openai_model_name(model: str) -> str:
    """Convert litellm-style provider/model names for direct OpenAI APIs."""
    if model.startswith("deepseek/"):
        return model.split("/", 1)[1]
    return model


def load_instances(subset: str, split: str) -> list[dict[str, Any]]:
    from datasets import load_dataset

    dataset_path = DATASET_MAPPING.get(subset, subset)
    return [dict(row) for row in load_dataset(dataset_path, split=split)]


def parse_slice_spec(slice_spec: str) -> slice:
    if not slice_spec:
        return slice(None)
    if ":" not in slice_spec:
        raise ValueError("--slice must look like START:STOP[:STEP]")
    parts = slice_spec.split(":")
    if len(parts) > 3:
        raise ValueError("--slice must look like START:STOP[:STEP]")
    values = [int(part) if part else None for part in parts]
    return slice(*values)


def read_ids_file(path: str | Path | None) -> list[str]:
    if not path:
        return []
    ids: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            ids.append(line)
    return ids


def select_instances(
    instances: list[dict[str, Any]],
    *,
    ids: list[str],
    slice_spec: str,
    all_instances: bool,
) -> list[dict[str, Any]]:
    ordered = sorted(instances, key=lambda item: item["instance_id"])
    by_id = {item["instance_id"]: item for item in ordered}
    has_ids = bool(ids)
    has_slice = bool(slice_spec)
    selectors = sum(bool(v) for v in (has_ids, has_slice, all_instances))
    if selectors != 1:
        raise ValueError("choose exactly one selector: instance id(s), --slice, or --all")

    if has_ids:
        selected: list[dict[str, Any]] = []
        for item_id in ids:
            key = item_id.strip()
            if not key:
                continue
            if key.isnumeric():
                idx = int(key)
                try:
                    selected.append(ordered[idx])
                except IndexError as exc:
                    raise ValueError(f"instance index out of range: {idx}") from exc
                continue
            if key not in by_id:
                raise ValueError(f"unknown instance id: {key}")
            selected.append(by_id[key])
        return selected

    if has_slice:
        return ordered[parse_slice_spec(slice_spec)]

    return ordered


def build_planner_messages(instance: dict[str, Any], max_queries: int) -> list[dict[str, str]]:
    problem = str(instance.get("problem_statement") or "")
    system = (
        "You plan web searches for software-debugging background knowledge. "
        "Return only JSON. The searches must be for general background, domain "
        "concepts, standards, public API documentation, math/statistics concepts, "
        "or user-facing behavior. Never search for source code, repository issues, "
        "pull requests, commits, patches, exact file paths, exact tests, stack "
        "traces, or the SWE-bench instance itself."
    )
    user = (
        f"Instance id: {instance.get('instance_id')}\n"
        f"Repository: {instance.get('repo')}\n\n"
        "Problem statement:\n"
        f"{problem}\n\n"
        f"Produce at most {max_queries} web search queries. Prefer zero queries if "
        "the problem is self-contained. Each query should be broad enough to avoid "
        "finding this specific bug or its fix.\n\n"
        "Return JSON exactly like:\n"
        '{"queries":[{"query":"...","rationale":"...","knowledge_type":"domain|api_docs|standard|concept"}],'
        '"no_search_reason":""}'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def call_llm(
    messages: list[dict[str, str]],
    *,
    env: dict[str, str],
    model: str,
    timeout: float,
) -> str:
    from openai import OpenAI

    base_url = env.get("LLM_API_BASE_URL") or os.environ.get("LLM_API_BASE_URL") or None
    api_key = env.get("LLM_API_KEY") or os.environ.get("LLM_API_KEY") or "EMPTY"
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    kwargs: dict[str, Any] = {"model": model, "messages": messages}
    profile = env.get("LLM_PARAM_PROFILE") or os.environ.get("LLM_PARAM_PROFILE") or "chat"
    if profile != "reasoning":
        kwargs["temperature"] = 0
    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


def extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM response JSON must be an object")
    return value


def coerce_search_plan(raw: str, max_queries: int) -> dict[str, Any]:
    data = extract_json_object(raw)
    queries: list[dict[str, str]] = []
    raw_queries = data.get("queries") or []
    if not isinstance(raw_queries, list):
        raw_queries = []
    for item in raw_queries[:max_queries]:
        if isinstance(item, str):
            query = item.strip()
            rationale = ""
            knowledge_type = ""
        elif isinstance(item, dict):
            query = str(item.get("query") or "").strip()
            rationale = str(item.get("rationale") or "").strip()
            knowledge_type = str(item.get("knowledge_type") or "").strip()
        else:
            continue
        if query:
            queries.append(
                {
                    "query": query,
                    "rationale": rationale,
                    "knowledge_type": knowledge_type,
                }
            )
    return {
        "queries": queries,
        "no_search_reason": str(data.get("no_search_reason") or "").strip(),
    }


def code_query_reason(query: str, instance: dict[str, Any]) -> str | None:
    lowered = query.lower()
    iid = str(instance.get("instance_id") or "")
    if iid and iid.lower() in lowered:
        return "exact instance id"
    match = re.search(r"-(\d+)$", iid)
    if match and re.search(rf"\b{re.escape(match.group(1))}\b", query):
        return "instance issue number"
    for pattern, reason in CODE_QUERY_PATTERNS:
        if pattern.search(query):
            return reason
    return None


def filter_search_plan(plan: dict[str, Any], instance: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    accepted: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in plan.get("queries") or []:
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        key = re.sub(r"\s+", " ", query).casefold()
        if key in seen:
            continue
        seen.add(key)
        reason = code_query_reason(query, instance)
        row = {
            "query": query,
            "rationale": str(item.get("rationale") or ""),
            "knowledge_type": str(item.get("knowledge_type") or ""),
        }
        if reason:
            row["rejected_reason"] = reason
            rejected.append(row)
        else:
            accepted.append(row)
    return accepted, rejected


def choose_provider(provider: str) -> str:
    if provider != "auto":
        return provider
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    if os.environ.get("BRAVE_SEARCH_API_KEY"):
        return "brave"
    if os.environ.get("SERPER_API_KEY"):
        return "serper"
    return "duckduckgo_html"


def _result(title: str, url: str, snippet: str, **extra: Any) -> dict[str, Any]:
    row = {"title": title or "", "url": url or "", "snippet": snippet or ""}
    row.update({k: v for k, v in extra.items() if v is not None})
    return row


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    for key, value in attrs:
        if key == "class" and value:
            return set(value.split())
    return set()


def _attr(attrs: list[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key == name and value:
            return value
    return ""


def _unwrap_duckduckgo_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "uddg" in qs and qs["uddg"]:
        return unquote(qs["uddg"][0])
    return url


class DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._in_title = False
        self._in_snippet = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []

    def _append_current(self) -> None:
        if not self._current:
            return
        self._current.setdefault("title", " ".join("".join(self._title_parts).split()))
        self._current.setdefault("snippet", " ".join("".join(self._snippet_parts).split()))
        if self._current.get("url"):
            self.results.append(
                _result(
                    str(self._current.get("title") or ""),
                    str(self._current.get("url") or ""),
                    str(self._current.get("snippet") or ""),
                )
            )
        self._current = None
        self._title_parts = []
        self._snippet_parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = _classes(attrs)
        if tag == "a" and "result__a" in classes:
            self._append_current()
            self._current = {"url": _unwrap_duckduckgo_url(html.unescape(_attr(attrs, "href")))}
            self._title_parts = []
            self._snippet_parts = []
            self._in_title = True
        elif self._current is not None and "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
            if self._current is not None:
                self._current["title"] = " ".join("".join(self._title_parts).split())
        if self._in_snippet and tag in {"a", "div"}:
            self._in_snippet = False
            if self._current is not None:
                self._current["snippet"] = " ".join("".join(self._snippet_parts).split())
                self._append_current()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)

    def close(self) -> None:
        super().close()
        self._append_current()


class VisibleTextParser(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


def result_url_block_reason(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for blocked in BLOCKED_RESULT_HOSTS:
        if host == blocked or host.endswith("." + blocked):
            return "code-related host"
    return None


def search_tavily(query: str, *, max_results: int, timeout: float) -> list[dict[str, Any]]:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY is required for provider=tavily")
    response = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": key,
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return [
        _result(
            str(item.get("title") or ""),
            str(item.get("url") or ""),
            str(item.get("content") or ""),
            score=item.get("score"),
        )
        for item in data.get("results") or []
    ][:max_results]


def search_brave(query: str, *, max_results: int, timeout: float) -> list[dict[str, Any]]:
    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        raise RuntimeError("BRAVE_SEARCH_API_KEY is required for provider=brave")
    response = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        params={"q": query, "count": max_results},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return [
        _result(
            str(item.get("title") or ""),
            str(item.get("url") or ""),
            str(item.get("description") or ""),
        )
        for item in ((data.get("web") or {}).get("results") or [])
    ][:max_results]


def search_serper(query: str, *, max_results: int, timeout: float) -> list[dict[str, Any]]:
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY is required for provider=serper")
    response = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        json={"q": query, "num": max_results},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return [
        _result(
            str(item.get("title") or ""),
            str(item.get("link") or ""),
            str(item.get("snippet") or ""),
        )
        for item in data.get("organic") or []
    ][:max_results]


def search_duckduckgo(query: str, *, max_results: int, timeout: float) -> list[dict[str, Any]]:
    response = requests.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_redirect": "1", "no_html": "1"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    rows: list[dict[str, Any]] = []
    if data.get("AbstractURL") or data.get("AbstractText"):
        rows.append(
            _result(
                str(data.get("Heading") or ""),
                str(data.get("AbstractURL") or ""),
                str(data.get("AbstractText") or ""),
            )
        )
    for item in data.get("RelatedTopics") or []:
        if "Topics" in item:
            for sub in item.get("Topics") or []:
                rows.append(_result(str(sub.get("Text") or ""), str(sub.get("FirstURL") or ""), ""))
        else:
            rows.append(_result(str(item.get("Text") or ""), str(item.get("FirstURL") or ""), ""))
        if len(rows) >= max_results:
            break
    return rows[:max_results]


def search_duckduckgo_html(query: str, *, max_results: int, timeout: float) -> list[dict[str, Any]]:
    response = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; sdlcma-background-search/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    parser = DuckDuckGoHTMLParser()
    parser.feed(response.text)
    parser.close()

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parser.results:
        url = str(item.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        if result_url_block_reason(url):
            continue
        rows.append(dict(item))
        if len(rows) >= max_results:
            break
    return rows


def search_web(provider: str, query: str, *, max_results: int, timeout: float) -> list[dict[str, Any]]:
    if provider == "none":
        return []
    if provider == "tavily":
        return search_tavily(query, max_results=max_results, timeout=timeout)
    if provider == "brave":
        return search_brave(query, max_results=max_results, timeout=timeout)
    if provider == "serper":
        return search_serper(query, max_results=max_results, timeout=timeout)
    if provider == "duckduckgo":
        return search_duckduckgo(query, max_results=max_results, timeout=timeout)
    if provider == "duckduckgo_html":
        return search_duckduckgo_html(query, max_results=max_results, timeout=timeout)
    raise ValueError(f"unknown search provider: {provider}")


def fetch_page_text(url: str, *, timeout: float, max_chars: int) -> dict[str, Any]:
    blocked = result_url_block_reason(url)
    if blocked:
        return {"fetched": False, "skipped_reason": blocked}
    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; sdlcma-background-search/1.0)",
            "Accept": "text/html,application/xhtml+xml,text/plain",
        },
        timeout=timeout,
        allow_redirects=True,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text/plain" not in content_type:
        return {"fetched": False, "skipped_reason": f"unsupported content type: {content_type}"}
    text = response.text
    if "html" in content_type:
        parser = VisibleTextParser()
        parser.feed(text)
        text = parser.text()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text[:max_chars]
    return {
        "fetched": True,
        "final_url": response.url,
        "content_type": content_type,
        "text_chars": len(text),
        "text": text,
    }


def fetch_result_pages(
    search_results: list[dict[str, Any]],
    *,
    max_pages: int,
    page_max_chars: int,
    timeout: float,
) -> None:
    remaining = max(0, max_pages)
    for row in search_results:
        for result in row.get("results") or []:
            if remaining <= 0:
                result["page_fetch"] = {"fetched": False, "skipped_reason": "page fetch budget exhausted"}
                continue
            url = str(result.get("url") or "")
            if not url:
                result["page_fetch"] = {"fetched": False, "skipped_reason": "missing url"}
                continue
            try:
                result["page_fetch"] = fetch_page_text(url, timeout=timeout, max_chars=page_max_chars)
            except Exception as exc:
                result["page_fetch"] = {"fetched": False, "error": f"{type(exc).__name__}: {exc}"}
            if result["page_fetch"].get("fetched"):
                remaining -= 1


def build_summary_messages(instance: dict[str, Any], search_results: list[dict[str, Any]], max_chars: int) -> list[dict[str, str]]:
    compact: list[dict[str, Any]] = []
    for row in search_results:
        compact_row = {
            "query": row.get("query"),
            "rationale": row.get("rationale"),
            "knowledge_type": row.get("knowledge_type"),
            "results": [],
        }
        for result in row.get("results") or []:
            page_fetch = result.get("page_fetch") if isinstance(result.get("page_fetch"), dict) else {}
            compact_row["results"].append(
                {
                    "title": result.get("title"),
                    "url": result.get("url"),
                    "snippet": result.get("snippet"),
                    "page_text": (page_fetch.get("text") or "")[:max_chars],
                    "page_fetch_status": {
                        key: value
                        for key, value in page_fetch.items()
                        if key not in {"text"}
                    },
                    "blocked_reason": result.get("blocked_reason"),
                }
            )
        compact.append(compact_row)

    system = (
        "You summarize web-search results for a SWE-bench background-knowledge packet. "
        "Only summarize domain concepts, standards, user-facing behavior, and public API documentation. "
        "Do not discuss source code, patches, commits, pull requests, repository issues, or exact fixes."
    )
    user = (
        f"Instance id: {instance.get('instance_id')}\n"
        f"Repository: {instance.get('repo')}\n\n"
        "Problem statement:\n"
        f"{instance.get('problem_statement')}\n\n"
        "Search evidence JSON:\n"
        f"{json.dumps(compact, ensure_ascii=False)[:max_chars]}\n\n"
        "Return concise Markdown with: useful background facts, source URLs, and any gaps. "
        "Do not invent facts that are not supported by the search evidence."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def existing_output_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        iid = data.get("instance_id")
        if isinstance(iid, str):
            ids.add(iid)
    return ids


def process_instance(
    instance: dict[str, Any],
    *,
    env: dict[str, str],
    model: str,
    provider: str,
    max_queries: int,
    max_results: int,
    fetch_pages: bool,
    max_pages: int,
    page_max_chars: int,
    summarize: bool,
    summary_max_chars: int,
    llm_timeout: float,
    search_timeout: float,
) -> dict[str, Any]:
    iid = str(instance.get("instance_id") or "")
    record: dict[str, Any] = {
        "instance_id": iid,
        "repo": instance.get("repo"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "llm_model": model,
        "search_provider": provider,
        "problem_statement": instance.get("problem_statement"),
    }
    try:
        raw = call_llm(
            build_planner_messages(instance, max_queries),
            env=env,
            model=model,
            timeout=llm_timeout,
        )
        plan = coerce_search_plan(raw, max_queries)
        accepted, rejected = filter_search_plan(plan, instance)
        record.update(
            {
                "llm_raw_response": raw,
                "search_plan": plan,
                "accepted_queries": accepted,
                "rejected_queries": rejected,
            }
        )
    except Exception as exc:
        record["error"] = f"planner {type(exc).__name__}: {exc}"
        return record

    search_results: list[dict[str, Any]] = []
    for item in record["accepted_queries"]:
        query = item["query"]
        row: dict[str, Any] = {
            "query": query,
            "rationale": item.get("rationale", ""),
            "knowledge_type": item.get("knowledge_type", ""),
        }
        try:
            row["results"] = search_web(provider, query, max_results=max_results, timeout=search_timeout)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["results"] = []
        search_results.append(row)
    if fetch_pages:
        fetch_result_pages(
            search_results,
            max_pages=max_pages,
            page_max_chars=page_max_chars,
            timeout=search_timeout,
        )
    record["search_results"] = search_results
    if summarize:
        try:
            record["search_summary"] = call_llm(
                build_summary_messages(instance, search_results, summary_max_chars),
                env=env,
                model=model,
                timeout=llm_timeout,
            ).strip()
        except Exception as exc:
            record["summary_error"] = f"{type(exc).__name__}: {exc}"
    return record


def write_record(path: Path, record: dict[str, Any]) -> None:
    with _WRITE_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ask an LLM for non-code background web searches for SWE-bench instances, then write JSONL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("instance_ids", nargs="*", help="One or more instance ids, or numeric indexes in sorted split.")
    parser.add_argument("--instances", default="", help="Comma-separated instance ids or numeric indexes.")
    parser.add_argument("--instances-file", default="", help="File with one instance id per line; # comments ok.")
    parser.add_argument("--slice", dest="slice_spec", default="", help="Slice over sorted instance ids, e.g. 0:10.")
    parser.add_argument("--all", action="store_true", help="Process the full selected split.")
    parser.add_argument("--subset", default="verified", help="SWE-bench subset or dataset path (default: verified).")
    parser.add_argument("--split", default="test", help="Dataset split (default: test).")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="Env file for LLM_API_* settings.")
    parser.add_argument("--model", default="", help="Planner model. Defaults to LLM_MODEL from env file/env.")
    parser.add_argument(
        "--provider",
        choices=("auto", "tavily", "brave", "serper", "duckduckgo_html", "duckduckgo", "none"),
        default="auto",
        help="Web-search provider. auto prefers Tavily, Brave, Serper, then DuckDuckGo HTML.",
    )
    parser.add_argument("--max-queries", type=int, default=3, help="Max LLM-planned queries per instance.")
    parser.add_argument("--max-results", type=int, default=5, help="Max search results per accepted query.")
    parser.add_argument("--fetch-pages", action="store_true", help="Fetch text from top search result pages.")
    parser.add_argument("--max-pages", type=int, default=5, help="Max pages to fetch per instance with --fetch-pages.")
    parser.add_argument("--page-max-chars", type=int, default=8000, help="Max extracted text chars per fetched page.")
    parser.add_argument("--no-summary", action="store_true", help="Skip LLM summary over search snippets/page text.")
    parser.add_argument("--summary-max-chars", type=int, default=30000, help="Max search evidence chars sent to summary LLM.")
    parser.add_argument("--workers", type=int, default=4, help="Parallel instance workers.")
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--search-timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=None, help="JSONL output path.")
    parser.add_argument("--redo-existing", action="store_true", help="Do not skip instance ids already in output.")
    parser.add_argument("--dry-run", action="store_true", help="List selected instances without LLM or web calls.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    comma_ids = [item.strip() for item in args.instances.split(",") if item.strip()]
    ids = [*args.instance_ids, *comma_ids, *read_ids_file(args.instances_file)]

    env = parse_dotenv(args.env_file)
    for key, value in env.items():
        os.environ.setdefault(key, value)
    raw_model = args.model or env.get("LLM_MODEL") or os.environ.get("LLM_MODEL") or ""
    model = args.model or normalize_openai_model_name(raw_model)
    if not model and not args.dry_run:
        raise SystemExit("LLM model is required via --model, env file LLM_MODEL, or environment LLM_MODEL")

    instances = select_instances(
        load_instances(args.subset, args.split),
        ids=ids,
        slice_spec=args.slice_spec,
        all_instances=args.all,
    )
    if not instances:
        raise SystemExit("no instances selected")

    provider = choose_provider(args.provider)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or DEFAULT_OUTPUT_DIR / f"swebench_background_search_{stamp}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output.with_suffix(".summary.json")

    if output.exists() and not args.redo_existing:
        done = existing_output_ids(output)
        instances = [item for item in instances if item["instance_id"] not in done]

    print(f"dataset: {DATASET_MAPPING.get(args.subset, args.subset)} split={args.split}")
    print(f"selected todo: {len(instances)}")
    print(f"model: {model or '<not needed for dry-run>'}")
    print(f"search provider: {provider}")
    print(f"output: {output}")
    if args.dry_run:
        for item in instances:
            print(item["instance_id"])
        return 0

    attempted = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                process_instance,
                instance,
                env=env,
                model=model,
                provider=provider,
                max_queries=max(0, args.max_queries),
                max_results=max(0, args.max_results),
                fetch_pages=args.fetch_pages,
                max_pages=max(0, args.max_pages),
                page_max_chars=max(0, args.page_max_chars),
                summarize=not args.no_summary,
                summary_max_chars=max(1000, args.summary_max_chars),
                llm_timeout=args.llm_timeout,
                search_timeout=args.search_timeout,
            ): instance["instance_id"]
            for instance in instances
        }
        for future in as_completed(futures):
            iid = futures[future]
            attempted += 1
            try:
                record = future.result()
            except Exception as exc:
                record = {"instance_id": iid, "error": f"{type(exc).__name__}: {exc}"}
            if (
                record.get("error")
                or record.get("summary_error")
                or any(row.get("error") for row in record.get("search_results") or [])
            ):
                errors += 1
                print(f"{iid}: ERROR", file=sys.stderr)
            else:
                accepted = len(record.get("accepted_queries") or [])
                rejected = len(record.get("rejected_queries") or [])
                result_count = sum(len(row.get("results") or []) for row in record.get("search_results") or [])
                fetched = sum(
                    1
                    for row in record.get("search_results") or []
                    for result in row.get("results") or []
                    if (result.get("page_fetch") or {}).get("fetched")
                )
                print(f"{iid}: queries={accepted} rejected={rejected} results={result_count} pages={fetched}")
            write_record(output, record)

    summary = {
        "output": str(output),
        "dataset": DATASET_MAPPING.get(args.subset, args.subset),
        "split": args.split,
        "attempted": attempted,
        "errors": errors,
        "model": model,
        "search_provider": provider,
        "max_queries": args.max_queries,
        "max_results": args.max_results,
        "fetch_pages": args.fetch_pages,
        "max_pages": args.max_pages,
        "summarize": not args.no_summary,
    }
    write_summary(summary_path, summary)
    print(f"Wrote {output}")
    print(f"Wrote {summary_path}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
