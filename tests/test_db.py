"""Unit tests for SQLite database layer."""

import numpy as np
import pytest
from src.db import Database


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / "test_slothbox.db"
    return Database(db_file)


def test_empty_db_stats(db):
    stats = db.get_stats()
    assert stats["total_items"] == 0
    assert stats["processed"] == 0
    assert stats["pending"] == 0
    assert stats["failed"] == 0
    assert stats["archived"] == 0
    assert stats["by_type"] == {}
    assert stats["top_tags"] == {}


def test_insert_and_get_item(db):
    item_id = db.insert_item("https://example.com", "url")
    assert item_id == 1

    item = db.get_item(item_id)
    assert item is not None
    assert item["raw_content"] == "https://example.com"
    assert item["content_type"] == "url"
    assert item["status"] == "pending"
    assert item["summary"] is None
    assert item["tags"] == []
    assert item["is_archived"] == 0


def test_update_item_success(db):
    item_id = db.insert_item("Note about RAG systems", "text")
    db.update_item_success(item_id, "Overview of RAG", ["ai", "rag", "search"])

    item = db.get_item(item_id)
    assert item["status"] == "processed"
    assert item["summary"] == "Overview of RAG"
    assert item["tags"] == ["ai", "rag", "search"]
    assert item["error_message"] is None


def test_update_item_failure(db):
    item_id = db.insert_item("Some link", "url")
    db.update_item_failure(item_id, "Ollama connection refused")

    item = db.get_item(item_id)
    assert item["status"] == "failed"
    assert item["error_message"] == "Ollama connection refused"
    assert item["summary"] is None


def test_recent_items(db):
    for i in range(7):
        item_id = db.insert_item(f"Item #{i}", "text")
        db.update_item_success(item_id, f"Summary #{i}", ["tag"])

    recents = db.get_recent_items(limit=5)
    assert len(recents) == 5
    # Most recent first
    assert recents[0]["raw_content"] == "Item #6"
    assert recents[4]["raw_content"] == "Item #2"


def test_embeddings_roundtrip(db):
    item_id = db.insert_item("Neural Networks note", "text")
    vector = np.array([0.1, -0.25, 0.85, 0.0], dtype=np.float32)
    db.save_embedding(item_id, vector)

    stored = db.get_all_embeddings()
    assert len(stored) == 1
    retrieved_id, retrieved_vec = stored[0]
    assert retrieved_id == item_id
    assert retrieved_vec.shape == (4,)
    assert np.allclose(vector, retrieved_vec)


def test_resurfacing_and_archive(db):
    # Insert an item and artificially backdate it by 10 days
    item_id = db.insert_item("Old Python Article", "url")
    db.update_item_success(item_id, "Old Python Article summary", ["python"])

    with db._get_connection() as conn:
        conn.execute(
            "UPDATE items SET created_at = datetime('now', '-10 days') WHERE id = ?",
            (item_id,),
        )

    candidates = db.get_resurface_candidates(days=7, limit=10)
    assert len(candidates) == 1
    assert candidates[0]["id"] == item_id

    # Archive the item
    success = db.mark_item_archived(item_id)
    assert success is True

    # Should no longer be a candidate
    candidates_after = db.get_resurface_candidates(days=7, limit=10)
    assert len(candidates_after) == 0

    # Also should not appear in active embeddings
    db.save_embedding(item_id, [0.1, 0.2])
    assert len(db.get_all_embeddings()) == 0
