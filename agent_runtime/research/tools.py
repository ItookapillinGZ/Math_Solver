from __future__ import annotations

import gzip
import json
import random
import re
import tarfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable
from datetime import datetime, timezone

import requests

from agent_runtime.security import ArchiveSecurityError, SafeArchiveExtractor
from .literature_search import BUILTIN_PROVIDERS, LiteratureSearchService


class ResearchToolRuntime:
    """Research-domain tools used by the mathematical research agent."""

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    ]

    def __init__(
        self,
        base_dir: Path,
        terminal_print: Callable[[str], None] = print,
        http_get: Callable[..., object] = requests.get,
        sleep: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
        random_choice: Callable[[list[str]], str] = random.choice,
        archive_extractor: SafeArchiveExtractor | None = None,
        literature_providers: tuple[str, ...] = BUILTIN_PROVIDERS,
    ):
        self.base_dir = Path(base_dir).resolve()
        self.terminal_print = terminal_print
        self.http_get = http_get
        self.sleep = sleep
        self.random_uniform = random_uniform
        self.random_choice = random_choice
        self.archive_extractor = archive_extractor or SafeArchiveExtractor()
        self.literature_search = LiteratureSearchService(http_get, sleep, literature_providers)

    def search_literature(self, query: str) -> str:
        """Search all configured built-in providers independently of MCP."""
        return json.dumps(self.literature_search.search(query), ensure_ascii=False)

    def search_arxiv_result(self, query: str) -> dict:
        """Compatibility entry point for the older arXiv-only text tool."""
        return self.literature_search.search_provider("arxiv", str(query).strip())

    def search_arxiv(self, query: str, **kwargs) -> str:
        """Compatibility text view for callers of the older search method."""
        result = self.search_arxiv_result(query)
        if result["status"] == "no_results":
            return "No papers found matching the query."
        if result["status"] == "failed":
            if result["message"].casefold() == "arxiv http 503":
                return "Notice: arXiv API Rate limit exceeded. Please use view_latex_theorem."
            return f"Search failed due to {result['error_type']}: {result['message']}"
        return "\n---\n".join(
            f"Arxiv_ID: {source['source_id']}\nTitle: {source['title']}\nSummary: {source['abstract'][:300]}"
            for source in result["sources"]
        )

    def download_arxiv_source(self, arxiv_id: str) -> str:
        """Download an arXiv source archive into Conference/<title> (<id>)."""
        headers = {"User-Agent": "Mozilla/5.0"}
        clean_id = arxiv_id.split("v")[0]
        filesystem_id = re.sub(r"[^A-Za-z0-9._-]", "_", clean_id).strip("._")
        if not filesystem_id:
            return f"Invalid arXiv ID: {arxiv_id}"

        api_url = f"http://export.arxiv.org/api/query?id_list={clean_id}"
        paper_title = f"Paper_{clean_id}"
        try:
            resp = self.http_get(api_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                entry = root.find("{http://www.w3.org/2005/Atom}entry")
                if entry is not None:
                    title_node = entry.find("{http://www.w3.org/2005/Atom}title")
                    if title_node is not None:
                        raw_title = " ".join(title_node.text.strip().split())
                        paper_title = re.sub(r'[\/\\:\*\?"<>\|\$]', "", raw_title)
                        if len(paper_title) > 80:
                            paper_title = paper_title[:80].strip() + "..."
        except Exception as title_err:
            self.terminal_print(
                f"[Warning] Failed to fetch paper title, using ID instead: {title_err}"
            )

        try:
            folder_name = f"{paper_title} ({filesystem_id})"
            conference_root = (self.base_dir / "Conference").resolve()
            conference_dir = (conference_root / folder_name).resolve()
            if not conference_dir.is_relative_to(conference_root):
                return f"Invalid archive destination for arXiv ID: {arxiv_id}"
            conference_dir.mkdir(parents=True, exist_ok=True)

            source_url = f"https://arxiv.org/e-print/{clean_id}"
            temp_tar_path = conference_dir / "source_archive.tmp"
            self.terminal_print(
                f"  \033[33m[Download] Fetching LaTeX source for: {paper_title}...\033[0m"
            )
            resp = self.http_get(source_url, headers=headers, timeout=30)
            if resp.status_code != 200:
                return (
                    f"Failed to download source for {arxiv_id}. "
                    f"Status code: {resp.status_code}"
                )

            temp_tar_path.write_bytes(resp.content)
            try:
                self.archive_extractor.extract_tar_gzip(
                    temp_tar_path, conference_dir
                )
                temp_tar_path.unlink(missing_ok=True)
                status_msg = "Multi-file project extracted successfully."
            except ArchiveSecurityError as exc:
                temp_tar_path.unlink(missing_ok=True)
                return (
                    f"Unsafe source archive rejected for {arxiv_id}. "
                    f"Reason: {exc}"
                )
            except (tarfile.ReadError, tarfile.CompressionError):
                real_tex_path = conference_dir / f"main_{filesystem_id}.tex"
                try:
                    self.archive_extractor.inflate_single_gzip(
                        temp_tar_path, real_tex_path
                    )
                    temp_tar_path.unlink(missing_ok=True)
                    status_msg = "Single LaTeX file deflated successfully."
                except ArchiveSecurityError as exc:
                    temp_tar_path.unlink(missing_ok=True)
                    return (
                        f"Unsafe gzip source rejected for {arxiv_id}. "
                        f"Reason: {exc}"
                    )
                except (gzip.BadGzipFile, EOFError, OSError):
                    # Only format-detection failures reach the raw-text fallback.
                    # A recognized archive that violates safety policy fails closed
                    # above instead of being silently reclassified as plain text.
                    real_tex_path.unlink(missing_ok=True)
                    temp_tar_path.rename(real_tex_path)
                    status_msg = "Raw text LaTeX file recovered."

            files_extracted = [
                file.name for file in conference_dir.glob("*") if file.is_file()
            ]
            return (
                f"Successfully saved to: 'Conference/{folder_name}'\n"
                f"Status: {status_msg}\n"
                f"Available files for AI to read: {files_extracted}"
            )
        except Exception as exc:
            return f"Error processing LaTeX source: {exc}"

    def view_latex_theorem(
        self,
        file_path: str,
        start_line: int,
        num_lines: int = 150,
    ) -> str:
        """Read a numbered LaTeX snippet with fuzzy arXiv-ID lookup."""
        try:
            conference_base = self.base_dir / "Conference"
            target_file: Path | None = None

            arxiv_id_match = re.search(r"\d{4}\.\d{4,5}", file_path)
            if arxiv_id_match:
                clean_id = arxiv_id_match.group(0)
                found_files = list(conference_base.rglob(f"*{clean_id}*.tex"))
                if found_files:
                    target_file = found_files[0]

            if not target_file:
                path = Path(file_path)
                target_file = path if path.exists() else (self.base_dir / file_path)

            if not target_file or not target_file.exists():
                return (
                    "Error: Cannot locate any LaTeX file matching "
                    f"'{file_path}' in Conference folder."
                )

            content = ""
            for encoding in ["utf-8", "latin-1", "cp1252", "gbk"]:
                try:
                    content = target_file.read_text(encoding=encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                content = target_file.read_text(encoding="utf-8", errors="replace")

            lines = content.splitlines()
            total = len(lines)
            idx = max(0, start_line - 1)
            end_idx = min(total, idx + num_lines)
            snippet = lines[idx:end_idx]
            formatted = "\n".join(
                f"{line_no + 1}: {line}"
                for line_no, line in enumerate(snippet, start=idx)
            )
            try:
                display_path = target_file.relative_to(self.base_dir)
            except ValueError:
                display_path = target_file
            return (
                f"--- Found file: {display_path} ---\n"
                f"--- Snippet from line {start_line} to {end_idx} "
                f"(Total lines: {total}) ---\n"
                f"{formatted}\n"
                f"--- End of Snippet ---"
            )
        except Exception as exc:
            return f"Error viewing snippet: {exc}"
