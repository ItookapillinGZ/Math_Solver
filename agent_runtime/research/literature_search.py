from __future__ import annotations

"""Small, deterministic retrieval layer; source relevance remains the Lead's call."""

import html
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence


BUILTIN_PROVIDERS = ("arxiv", "crossref")
ARXIV_RESULTS = 10
CROSSREF_RESULTS = 10
_WORDS = re.compile(r"[\w]+(?:[-'][\w]+)*", re.UNICODE)
_STOP = frozenset({"a", "an", "and", "for", "from", "in", "of", "on", "the", "to", "with"})


def _tokens(value: str) -> list[str]:
    return [word.casefold() for word in _WORDS.findall(value) if word.casefold() not in _STOP]


def build_arxiv_search_query(query: str) -> str:
    """Use an explicit phrase when supplied, then a few informative AND terms.

    An initial pair of capitalized words is treated as a possible name/phrase;
    the rule is syntactic and has no mathematics or author-name dictionary.
    """
    query = " ".join(str(query).split())
    if not query:
        raise ValueError("query cannot be empty")
    quoted = re.findall(r'"([^"\n]+)"', query)
    plain = re.sub(r'"[^"\n]*"', " ", query)
    words = _WORDS.findall(plain)
    phrases = [" ".join(_WORDS.findall(part)) for part in quoted]
    if not phrases and len(words) >= 2 and all(word[:1].isupper() for word in words[:2]):
        phrases.append(" ".join(words[:2]))
        words = words[2:]
    terms = [word for word in words if word.casefold() not in _STOP]
    parts = [f'all:"{phrase}"' for phrase in phrases if phrase]
    # A small conjunction prevents one generic word from dominating the feed.
    parts.extend(f"all:{word}" for word in terms[:3])
    if not parts:
        parts = [f"all:{words[0]}"] if words else [f'all:"{phrases[0]}"']
    return " AND ".join(parts)


def build_arxiv_url(query: str) -> str:
    params = {
        "search_query": build_arxiv_search_query(query),
        "start": 0,
        "max_results": ARXIV_RESULTS,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    return "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def build_crossref_url(query: str) -> str:
    params = {"query.bibliographic": " ".join(str(query).split()), "rows": CROSSREF_RESULTS}
    return "https://api.crossref.org/works?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _year(record: Mapping[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        date = record.get(key)
        if isinstance(date, dict):
            parts = date.get("date-parts") or []
            if parts and parts[0]:
                try:
                    return int(parts[0][0])
                except (TypeError, ValueError):
                    pass
    return None


def _clean_abstract(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def normalize_crossref_item(item: Mapping[str, Any], query: str, retrieved_at: str | None = None) -> dict[str, Any] | None:
    doi = str(item.get("DOI") or "").strip().lower()
    titles = item.get("title") or []
    title = str(titles[0] if isinstance(titles, list) and titles else titles if isinstance(titles, str) else "").strip()
    if not doi or not title:
        return None
    authors = []
    for author in item.get("author") or []:
        if isinstance(author, dict):
            name = " ".join(str(author.get(field) or "").strip() for field in ("given", "family")).strip()
            if name:
                authors.append(name)
    source = {
        "query": query, "provider": "crossref", "providers": ["crossref"],
        "source_type": str(item.get("type") or "journal-article"),
        "source_id": doi, "title": " ".join(title.split()), "authors": authors,
        "url": str(item.get("URL") or f"https://doi.org/{doi}"), "doi": doi,
        "published_year": _year(item), "abstract": _clean_abstract(item.get("abstract")),
        "retrieved_at": retrieved_at or _now(),
    }
    return source


def normalize_arxiv_feed(content: bytes, query: str, retrieved_at: str | None = None) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    atom = "{http://www.w3.org/2005/Atom}"
    arxiv = "{http://arxiv.org/schemas/atom}"
    sources = []
    for entry in root.findall(f"{atom}entry"):
        id_node, title_node = entry.find(f"{atom}id"), entry.find(f"{atom}title")
        if id_node is None or not id_node.text or title_node is None or not title_node.text:
            continue
        source_id = id_node.text.strip().split("/abs/")[-1]
        if not source_id or source_id == "http://arxiv.org/api/errors":
            continue
        summary = entry.find(f"{atom}summary")
        published = entry.find(f"{atom}published")
        doi_node = entry.find(f"{arxiv}doi")
        doi = (doi_node.text or "").strip().lower() if doi_node is not None else ""
        authors = []
        for author in entry.findall(f"{atom}author"):
            name = author.find(f"{atom}name")
            if name is not None and name.text:
                authors.append(" ".join(name.text.split()))
        year = None
        if published is not None and published.text:
            try:
                year = int(published.text[:4])
            except ValueError:
                pass
        sources.append({
            "query": query, "provider": "arxiv", "providers": ["arxiv"],
            "source_type": "arxiv", "source_id": source_id,
            "title": " ".join(title_node.text.split()), "authors": authors,
            "url": f"https://arxiv.org/abs/{source_id}", "doi": doi,
            "published_year": year,
            "abstract": " ".join(summary.text.split()) if summary is not None and summary.text else "",
            "retrieved_at": retrieved_at or _now(),
        })
    return sources


def _normalized_title(source: Mapping[str, Any]) -> str:
    return " ".join(re.findall(r"\w+", str(source.get("title") or "").casefold()))


def deduplicate_sources(sources: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for raw in sources:
        source = dict(raw)
        doi = str(source.get("doi") or "").strip().lower()
        title = _normalized_title(source)
        match = next((i for i, old in enumerate(merged) if
                      (doi and doi == str(old.get("doi") or "").strip().lower()) or
                      (source.get("source_type") == old.get("source_type") and source.get("source_id") == old.get("source_id")) or
                      (title and title == _normalized_title(old))), None)
        if match is None:
            source["providers"] = list(dict.fromkeys(source.get("providers") or [source.get("provider")]))
            source["source_ids"] = list(dict.fromkeys(source.get("source_ids") or [source.get("source_id")]))
            merged.append(source)
            continue
        old = merged[match]
        providers = list(dict.fromkeys([*(old.get("providers") or [old.get("provider")]),
                                        *(source.get("providers") or [source.get("provider")])]))
        source_ids = list(dict.fromkeys([*(old.get("source_ids") or [old.get("source_id")]),
                                         *(source.get("source_ids") or [source.get("source_id")])]))
        # Prefer DOI metadata as the canonical selectable record.
        if source.get("provider") == "crossref" and old.get("provider") != "crossref":
            source["providers"] = providers
            source["source_ids"] = source_ids
            if not source.get("abstract") and old.get("abstract"):
                source["abstract"] = old["abstract"]
            merged[match] = source
        else:
            old["providers"] = providers
            old["source_ids"] = source_ids
            if not old.get("doi") and doi:
                old["doi"] = doi
            if not old.get("abstract") and source.get("abstract"):
                old["abstract"] = source["abstract"]
    return merged


def rank_sources(sources: Sequence[Mapping[str, Any]], query: str) -> list[dict[str, Any]]:
    terms = list(dict.fromkeys(_tokens(query)))
    normalized_query = " ".join(_WORDS.findall(query.casefold()))

    def key(source: Mapping[str, Any]) -> tuple[Any, ...]:
        title = str(source.get("title") or "")
        title_terms = set(_tokens(title))
        abstract_terms = set(_tokens(str(source.get("abstract") or "")))
        author_terms = set(_tokens(" ".join(source.get("authors") or [])))
        title_normal = _normalized_title(source)
        exact_title = int(bool(normalized_query) and normalized_query == title_normal)
        phrase = int(bool(normalized_query) and normalized_query in title_normal)
        title_overlap = len(title_terms.intersection(terms))
        author_overlap = len(author_terms.intersection(terms))
        abstract_overlap = len(abstract_terms.intersection(terms))
        coverage = len((title_terms | author_terms | abstract_terms).intersection(terms))
        score = 30 * exact_title + 12 * phrase + 5 * title_overlap + 3 * author_overlap + abstract_overlap + 2 * coverage
        return (-score, -title_overlap, title_normal, str(source.get("source_id") or ""))

    return [dict(source) for source in sorted(sources, key=key)]


class ArxivProvider:
    name = "arxiv"
    timeout = 4

    @staticmethod
    def url(query: str) -> str:
        return build_arxiv_url(query)

    @staticmethod
    def parse(response: Any, query: str) -> list[dict[str, Any]]:
        return normalize_arxiv_feed(response.content, query)


class CrossrefProvider:
    name = "crossref"
    timeout = 6

    @staticmethod
    def url(query: str) -> str:
        return build_crossref_url(query)

    @staticmethod
    def parse(response: Any, query: str) -> list[dict[str, Any]]:
        items = response.json().get("message", {}).get("items", [])
        if not isinstance(items, list):
            raise ValueError("Crossref items must be a list")
        return [source for item in items if isinstance(item, dict)
                if (source := normalize_crossref_item(item, query)) is not None]


class LiteratureSearchService:
    def __init__(self, http_get: Callable[..., Any], sleep: Callable[[float], None], providers: Sequence[str] = BUILTIN_PROVIDERS):
        unknown = set(providers) - set(BUILTIN_PROVIDERS)
        if unknown or not providers:
            raise ValueError(f"invalid literature providers: {sorted(unknown)}")
        self.http_get, self.sleep = http_get, sleep
        available = {"arxiv": ArxivProvider(), "crossref": CrossrefProvider()}
        self.providers = tuple(available[name] for name in dict.fromkeys(providers))

    def _get(self, url: str, provider: str, timeout: int) -> Any:
        headers = {"User-Agent": "MathSolverLiterature/1.0 (research client)"}
        for attempt in range(3):
            try:
                response = self.http_get(url, headers=headers, timeout=timeout)
            except Exception:
                if attempt == 2:
                    raise
                self.sleep(0.25 * (2 ** attempt))
                continue
            if int(response.status_code) in {429, 500, 502, 503, 504} and attempt < 2:
                self.sleep(0.25 * (2 ** attempt))
                continue
            if int(response.status_code) != 200:
                raise RuntimeError(f"{provider} HTTP {response.status_code}")
            return response
        raise RuntimeError(f"{provider} request failed")

    def search_provider(self, provider: str, query: str) -> dict[str, Any]:
        started = time.monotonic()
        result: dict[str, Any] = {"query": query, "provider": provider, "status": "failed", "sources": [],
                                  "error_type": "", "message": "", "latency_ms": 0.0, "result_count": 0}
        try:
            if not query.strip():
                raise ValueError("query cannot be empty")
            available = {item.name: item for item in self.providers}
            selected = available.get(provider)
            if selected is None:
                raise ValueError(f"unknown or disabled provider: {provider}")
            response = self._get(selected.url(query), selected.name, selected.timeout)
            sources = selected.parse(response, query)
            result.update(status="success" if sources else "no_results", sources=sources, result_count=len(sources))
        except Exception as exc:
            result.update(error_type=("parse_error" if isinstance(exc, (ValueError, ET.ParseError, TypeError, KeyError, AttributeError)) else
                                      "http_error" if isinstance(exc, RuntimeError) else "network_error"),
                          message=str(exc)[:200])
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        return result

    def search(self, query: str) -> dict[str, Any]:
        started = time.monotonic()
        query = " ".join(str(query).split())
        results = [self.search_provider(provider.name, query) for provider in self.providers]
        raw = [source for result in results for source in result["sources"]]
        sources = rank_sources(deduplicate_sources(raw), query)
        statuses = {result["provider"]: {key: result[key] for key in
                    ("status", "result_count", "latency_ms", "error_type", "message")} for result in results}
        status = "success" if sources else "failed" if all(result["status"] == "failed" for result in results) else "no_results"
        single_error = results[0] if len(results) == 1 and status == "failed" else None
        return {"query": query, "provider": "builtin_literature", "providers": [provider.name for provider in self.providers],
                "status": status, "sources": sources, "provider_statuses": statuses,
                "raw_result_count": len(raw), "deduplicated_result_count": len(sources),
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "error_type": (single_error["error_type"] if single_error else "provider_error" if status == "failed" else ""),
                "message": (single_error["message"] if single_error else "all built-in providers failed" if status == "failed" else "")}
