from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping


_ARXIV_ENTRY = re.compile(
    r"Arxiv_ID:\s*(?P<id>[^\s]+)\s*\nTitle:\s*(?P<title>[^\n]+)(?:\nSummary:\s*(?P<summary>[^\n]+))?",
    re.IGNORECASE,
)


def _source(raw: Mapping[str, Any], query: str, provider: str) -> dict[str, Any] | None:
    source_id = str(raw.get("source_id") or raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not source_id or not title:
        return None
    actual_provider = str(raw.get("provider") or provider).strip()
    source_type = str(raw.get("source_type") or ("arxiv" if actual_provider in {"arxiv", "builtin_arxiv"} else "scholarly_metadata")).strip().lower()
    doi = str(raw.get("doi") or "").strip().lower()
    year = raw.get("published_year")
    return {
        "query": query,
        "provider": actual_provider,
        "providers": list(dict.fromkeys(raw.get("providers") or [actual_provider])),
        "source_ids": list(dict.fromkeys(raw.get("source_ids") or [source_id])),
        "source_type": source_type,
        "source_id": source_id,
        "title": title,
        "authors": [str(author) for author in raw.get("authors", []) or []],
        "url": str(raw.get("url") or (f"https://arxiv.org/abs/{source_id}" if source_type == "arxiv" else "")),
        "doi": doi,
        "published_year": int(year) if year not in (None, "") else None,
        "abstract": str(raw.get("abstract") or raw.get("snippet") or ""),
        "retrieved_at": str(raw.get("retrieved_at") or datetime.now(timezone.utc).isoformat()),
    }


def classify_literature_result(
    tool_name: str, tool_input: Mapping[str, Any], output: str, *, latency_ms: float = 0.0
) -> dict[str, Any] | None:
    """Normalize a real search attempt; a connection is never retrieval evidence."""
    if tool_name not in {"search_literature", "mcp__docs__search"}:
        return None
    query = str(tool_input.get("query") or "").strip()
    provider = "builtin_literature" if tool_name == "search_literature" else "external_mcp:docs"
    result = str(output).strip()
    sources: list[dict[str, Any]] = []
    error_type = ""
    message = ""
    status = "no_results"
    provider_statuses: dict[str, Any] = {}
    raw_result_count = 0
    deduplicated_result_count = 0
    if tool_name == "search_literature":
        try:
            data = json.loads(result)
            if not isinstance(data, dict):
                raise ValueError("provider result must be an object")
            provider = str(data.get("provider") or provider)
            status = str(data.get("status") or "no_results")
            if status not in {"success", "no_results", "failed"}:
                raise ValueError("invalid provider status")
            error_type = str(data.get("error_type") or "")
            message = str(data.get("message") or "")
            latency_ms = float(data.get("latency_ms") or latency_ms)
            provider_statuses = dict(data.get("provider_statuses") or {})
            raw_result_count = int(data.get("raw_result_count") or 0)
            deduplicated_result_count = int(data.get("deduplicated_result_count") or 0)
            for raw in data.get("sources", []) or []:
                if isinstance(raw, dict):
                    normalized = _source(raw, query, provider)
                    if normalized:
                        sources.append(normalized)
        except (ValueError, TypeError) as exc:
            status, error_type, message = "failed", "parse_error", str(exc)
    else:
        if result.startswith(("MCP error:", "MCP tool reported an error", "Search failed", "Notice:", "Failed")):
            status, error_type = "failed", "provider_error"
            message = "External MCP literature search failed"
        else:
            structured = result.split("[Structured Content]\n", 1)[-1] if "[Structured Content]\n" in result else result
            try:
                data = json.loads(structured)
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                raw_sources = data.get("sources", data.get("results", [])) or []
                if isinstance(raw_sources, list):
                    for raw in raw_sources:
                        if isinstance(raw, dict):
                            normalized = _source(raw, query, provider)
                            if normalized:
                                sources.append(normalized)
                if data.get("status") == "failed":
                    status = "failed"
                    error_type = str(data.get("error_type") or "provider_error")
                    message = str(data.get("message") or "External MCP literature search failed")[:200]
                else:
                    status = "success" if sources else "no_results"
            else:
                for match in _ARXIV_ENTRY.finditer(result):
                    normalized = _source({
                        "source_id": match.group("id"), "title": match.group("title"),
                        "abstract": match.group("summary") or "",
                    }, query, provider)
                    if normalized:
                        sources.append(normalized)
                status = "success" if sources else (
                    "no_results" if result.startswith("No papers found") else "failed"
                )
                if status == "failed":
                    error_type, message = "parse_error", "External MCP returned no recognized literature result"
    if not raw_result_count:
        raw_result_count = len(sources)
    if not deduplicated_result_count:
        deduplicated_result_count = len(sources)
    if status == "success" and not sources:
        status = "no_results"
    return {
        "query": query, "provider": provider, "status": status,
        "result_count": len(sources), "latency_ms": round(latency_ms, 2),
        "sources": sources, "error_type": error_type, "message": message,
        "provider_statuses": provider_statuses,
        "raw_result_count": raw_result_count,
        "deduplicated_result_count": deduplicated_result_count,
    }
