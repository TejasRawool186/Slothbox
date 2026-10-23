"""Tests for content ingestion and processing pipeline."""

import pytest
from pathlib import Path
from src.ai import AIEngine
from src.db import Database
from src.ingest import detect_content_type, save_media_file, process_item_pipeline


def test_detect_content_type():
    assert detect_content_type("https://news.ycombinator.com") == "url"
    assert detect_content_type("http://example.com/test?a=1") == "url"
    assert detect_content_type("Remember to buy milk") == "text"
    assert detect_content_type("Check out this site: https://news.ycombinator.com") == "text"


def test_save_media_file(tmp_path):
    media_dir = tmp_path / "media"
    data = b"image binary content"
    saved_path = save_media_file(data, "sloth.jpg", media_dir)

    assert saved_path.exists()
    assert saved_path.suffix == ".jpg"
    assert saved_path.read_bytes() == data


@pytest.mark.asyncio
async def test_process_item_pipeline_offline_ollama(tmp_path):
    """Verify that when Ollama is offline, process_item_pipeline updates item to 'failed' with error message."""
    db_file = tmp_path / "test.db"
    db = Database(db_file)
    offline_ai = AIEngine(base_url="http://127.0.0.1:59998")

    item_id = db.insert_item("A note that cannot be processed", "text")
    success, msg, summary = await process_item_pipeline(item_id, db, offline_ai)

    assert success is False
    assert "Ollama not reachable" in msg
    assert summary is None

    # Check database state
    item = db.get_item(item_id)
    assert item["status"] == "failed"
    assert "Ollama" in item["error_message"]
    assert item["summary"] is None
