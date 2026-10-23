"""Content ingestion, URL extraction, media file saving, and background pipeline processing."""

import logging
import re
import uuid
from html import unescape
from pathlib import Path
from typing import Optional
import httpx

from src.ai import AIEngine, OllamaProcessingError
from src.db import Database

logger = logging.getLogger(__name__)

URL_REGEX = re.compile(
    r"^(https?://[^\s/$.?#].[^\s]*)$", re.IGNORECASE
)


def detect_content_type(text: str) -> str:
    """Detect whether plain string input is a URL or text note."""
    cleaned = text.strip()
    if URL_REGEX.match(cleaned):
        return "url"
    return "text"


async def fetch_url_content(url: str, timeout_seconds: float = 10.0) -> str:
    """
    Fetch and extract clean readable text from a URL.
    Returns cleaned text, or falls back to raw URL if network fails.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36 Slothbox/1.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"URL fetch returned status {resp.status_code} for {url}")
                return url

            html_text = resp.text
            # Basic HTML to clean text extraction
            # 1. Remove scripts, styles
            clean_html = re.sub(
                r"<(script|style|nav|footer|header)[^>]*>.*?</\1>",
                " ",
                html_text,
                flags=re.DOTALL | re.IGNORECASE,
            )
            # 2. Extract title if present
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
            title = title_match.group(1).strip() if title_match else ""

            # 3. Strip all HTML tags
            text_only = re.sub(r"<[^>]+>", " ", clean_html)
            # 4. Unescape HTML entities & clean whitespace
            clean_text = unescape(re.sub(r"\s+", " ", text_only)).strip()

            if title:
                full_content = f"Title: {title}\nURL: {url}\n\nContent:\n{clean_text[:4000]}"
            else:
                full_content = f"URL: {url}\n\nContent:\n{clean_text[:4000]}"

            return full_content if len(clean_text) > 50 else url
    except Exception as exc:
        logger.warning(f"Failed to fetch URL {url}: {exc}")
        return url


def save_media_file(
    file_bytes: bytes,
    original_filename: str,
    media_dir: Path,
) -> Path:
    """Save media or document bytes to local media directory with unique filename."""
    media_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(original_filename).suffix or ".bin"
    unique_name = f"{uuid.uuid4().hex[:12]}_{Path(original_filename).stem}{suffix}"
    target_path = media_dir / unique_name
    target_path.write_bytes(file_bytes)
    return target_path


async def process_item_pipeline(
    item_id: int,
    db: Database,
    ai_engine: AIEngine,
) -> tuple[bool, str, Optional[str]]:
    """
    Executes the background AI processing pipeline for a single item.
    Returns (success: bool, user_message: str, summary: Optional[str]).
    Strictly records 'failed' on Ollama error without fake fallbacks.
    """
    item = db.get_item(item_id)
    if not item:
        return False, f"Item #{item_id} not found in database.", None

    content_type = item["content_type"]
    raw_content = item["raw_content"]
    file_path = item.get("file_path")

    try:
        summary = ""
        tags: list[str] = []
        text_for_embedding = ""

        full_content_str = ""
        if content_type == "url":
            page_text = await fetch_url_content(raw_content)
            full_content_str = page_text
            summary, tags = await ai_engine.generate_summary_and_tags(page_text, content_type="web article")
            text_for_embedding = f"{summary} {' '.join(tags)} {page_text[:1000]}"

        elif content_type == "text":
            full_content_str = raw_content
            summary, tags = await ai_engine.generate_summary_and_tags(raw_content, content_type="text note")
            text_for_embedding = f"{summary} {' '.join(tags)} {raw_content}"

        elif content_type == "image":
            if not file_path or not Path(file_path).exists():
                raise OllamaProcessingError(f"Image file is missing on disk: {file_path}")
            summary, tags = await ai_engine.describe_image(file_path)
            full_content_str = f"Image description: {summary}. User caption: {raw_content}"
            text_for_embedding = f"{summary} {' '.join(tags)} {raw_content}"

        elif content_type == "document":
            # For text documents or PDFs
            doc_path = Path(file_path) if file_path else None
            doc_text = raw_content
            if doc_path and doc_path.exists() and doc_path.suffix.lower() in [".txt", ".md", ".csv", ".json"]:
                try:
                    doc_text = doc_path.read_text(encoding="utf-8", errors="ignore")[:4000]
                except Exception:
                    pass
            full_content_str = doc_text
            summary, tags = await ai_engine.generate_summary_and_tags(doc_text, content_type="document")
            text_for_embedding = f"{summary} {' '.join(tags)} {doc_text[:1000]}"

        else:
            raise OllamaProcessingError(f"Unsupported content type: {content_type}")

        # Generate embedding vector
        vector = await ai_engine.generate_embedding(text_for_embedding)

        # Persist success state in SQLite
        db.update_item_success(item_id, summary, tags, full_text=full_content_str)
        db.save_embedding(item_id, vector)

        tag_str = " ".join([f"#{t}" for t in tags])
        user_msg = f"Processed #{item_id}\n\n{summary}\n\nTags: {tag_str}"
        return True, user_msg, summary

    except OllamaProcessingError as err:
        err_msg = f"Ollama not reachable: {err}"
        db.update_item_failure(item_id, err_msg)
        user_msg = (
            f"Slothbox couldn't process item #{item_id}: "
            f"Ollama not reachable or model error. Item saved but not processed."
        )
        return False, user_msg, None

    except Exception as err:
        err_msg = f"Processing error: {err}"
        db.update_item_failure(item_id, err_msg)
        user_msg = f"Slothbox encountered an error processing item #{item_id}: {err}"
        return False, user_msg, None
