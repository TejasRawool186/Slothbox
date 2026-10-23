"""SQLite Database Manager for Slothbox."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import numpy as np


class Database:
    """Manages SQLite connection, schema initialization, items, and vector persistence."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        """Initialize the database schema according to the SRS."""
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_content TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    file_path TEXT,
                    summary TEXT,
                    tags TEXT,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    is_archived INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    opened_at TEXT,
                    archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS embeddings (
                    item_id INTEGER PRIMARY KEY,
                    vector BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
                CREATE INDEX IF NOT EXISTS idx_items_archived ON items(is_archived);
                CREATE INDEX IF NOT EXISTS idx_items_created_at ON items(created_at);
                """
            )
            # Safe schema migration for full_text
            cols = [row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()]
            if "full_text" not in cols:
                conn.execute("ALTER TABLE items ADD COLUMN full_text TEXT;")

    # -------------------------------------------------------------------------
    # Item CRUD Operations
    # -------------------------------------------------------------------------

    def insert_item(
        self,
        raw_content: str,
        content_type: str,
        file_path: Optional[str] = None,
        full_text: Optional[str] = None,
    ) -> int:
        """Insert a newly captured item with 'pending' status."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO items (raw_content, content_type, file_path, full_text, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (raw_content, content_type, file_path, full_text),
            )
            return cursor.lastrowid  # type: ignore

    def update_item_success(
        self,
        item_id: int,
        summary: str,
        tags: list[str],
        full_text: Optional[str] = None,
    ) -> None:
        """Mark an item as successfully processed with summary and tags."""
        tags_json = json.dumps(tags)
        with self._get_connection() as conn:
            if full_text:
                conn.execute(
                    """
                    UPDATE items
                    SET summary = ?, tags = ?, full_text = ?, status = 'processed', error_message = NULL
                    WHERE id = ?
                    """,
                    (summary, tags_json, full_text, item_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE items
                    SET summary = ?, tags = ?, status = 'processed', error_message = NULL
                    WHERE id = ?
                    """,
                    (summary, tags_json, item_id),
                )

    def update_item_failure(self, item_id: int, error_message: str) -> None:
        """Mark an item as failed with explicit error details."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE items
                SET status = 'failed', error_message = ?
                WHERE id = ?
                """,
                (error_message, item_id),
            )

    def get_item(self, item_id: int) -> Optional[dict[str, Any]]:
        """Fetch a single item by ID."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            if data.get("tags"):
                try:
                    data["tags"] = json.loads(data["tags"])
                except Exception:
                    data["tags"] = []
            else:
                data["tags"] = []
            return data

    def get_recent_items(self, limit: int = 5) -> list[dict[str, Any]]:
        """Fetch the most recent items (default: 5)."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, raw_content, content_type, summary, tags, status, created_at
                FROM items
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                if item.get("tags"):
                    try:
                        item["tags"] = json.loads(item["tags"])
                    except Exception:
                        item["tags"] = []
                else:
                    item["tags"] = []
                results.append(item)
            return results

    # -------------------------------------------------------------------------
    # Resurfacing & Archiving Operations
    # -------------------------------------------------------------------------

    def get_resurface_candidates(
        self, days: int = 7, limit: int = 1
    ) -> list[dict[str, Any]]:
        """
        Query items older than `days` that are processed, not archived, and never opened.
        """
        cutoff_modifier = f"-{days} days"
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, raw_content, content_type, summary, tags, created_at
                FROM items
                WHERE status = 'processed'
                  AND is_archived = 0
                  AND opened_at IS NULL
                  AND created_at <= datetime('now', ?)
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (cutoff_modifier, limit),
            ).fetchall()
            candidates = []
            for row in rows:
                item = dict(row)
                if item.get("tags"):
                    try:
                        item["tags"] = json.loads(item["tags"])
                    except Exception:
                        item["tags"] = []
                else:
                    item["tags"] = []
                candidates.append(item)
            return candidates

    def mark_item_kept(self, item_id: int) -> bool:
        """Mark item as opened/reviewed, keeping it active."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE items SET opened_at = ? WHERE id = ?", (now, item_id)
            )
            return cursor.rowcount > 0

    def mark_item_archived(self, item_id: int) -> bool:
        """Mark item as archived, excluding it from future resurfacing."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE items
                SET is_archived = 1, archived_at = ?
                WHERE id = ?
                """,
                (now, item_id),
            )
            return cursor.rowcount > 0

    # -------------------------------------------------------------------------
    # Vector Embeddings Storage & Retrieval
    # -------------------------------------------------------------------------

    def save_embedding(
        self, item_id: int, vector: list[float] | np.ndarray
    ) -> None:
        """Store dense vector embedding as a 32-bit float binary BLOB."""
        arr = np.asarray(vector, dtype=np.float32)
        dim = int(arr.shape[0])
        blob = arr.tobytes()

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO embeddings (item_id, vector, dim)
                VALUES (?, ?, ?)
                """,
                (item_id, blob, dim),
            )

    def get_all_embeddings(self) -> list[tuple[int, np.ndarray]]:
        """
        Retrieve all stored embeddings unpacked into (item_id, np.ndarray).
        Only returns embeddings for non-archived items.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT e.item_id, e.vector, e.dim
                FROM embeddings e
                JOIN items i ON e.item_id = i.id
                WHERE i.is_archived = 0
                """
            ).fetchall()
            results = []
            for row in rows:
                item_id = row["item_id"]
                dim = row["dim"]
                arr = np.frombuffer(row["vector"], dtype=np.float32)
                if arr.shape[0] == dim:
                    results.append((item_id, arr))
            return results

    # -------------------------------------------------------------------------
    # Statistics
    # -------------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Aggregate stash statistics without mock data."""
        with self._get_connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            processed = conn.execute(
                "SELECT COUNT(*) FROM items WHERE status = 'processed'"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM items WHERE status = 'pending'"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM items WHERE status = 'failed'"
            ).fetchone()[0]
            archived = conn.execute(
                "SELECT COUNT(*) FROM items WHERE is_archived = 1"
            ).fetchone()[0]

            # Content types
            types_cursor = conn.execute(
                "SELECT content_type, COUNT(*) as cnt FROM items GROUP BY content_type"
            )
            type_counts = {row["content_type"]: row["cnt"] for row in types_cursor}

            # Tag frequencies
            tag_cursor = conn.execute(
                "SELECT tags FROM items WHERE tags IS NOT NULL AND status = 'processed'"
            )
            tag_counts: dict[str, int] = {}
            for row in tag_cursor:
                try:
                    for t in json.loads(row["tags"]):
                        cleaned = str(t).strip().lower()
                        if cleaned:
                            tag_counts[cleaned] = tag_counts.get(cleaned, 0) + 1
                except Exception:
                    continue

            # Sort top tags
            sorted_tags = sorted(
                tag_counts.items(), key=lambda x: x[1], reverse=True
            )[:10]

            return {
                "total_items": total,
                "processed": processed,
                "pending": pending,
                "failed": failed,
                "archived": archived,
                "by_type": type_counts,
                "top_tags": dict(sorted_tags),
            }
