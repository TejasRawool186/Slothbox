"""Unit tests for Telegram bot handlers and security authorization."""

from unittest.mock import AsyncMock, MagicMock
import numpy as np
import pytest

from src.ai import AIEngine
from src.bot import (
    authorized_only,
    start_command,
    help_command,
    stats_command,
    recent_command,
    search_command,
    digest_command,
    keep_command,
    archive_command,
    get_command,
    read_command,
)
from src.config import Settings
from src.db import Database


@pytest.fixture
def mock_settings(tmp_path):
    return Settings(
        telegram_bot_token="123456:FAKE_TOKEN",
        telegram_allowed_user_id=777777,
        ollama_base_url="http://127.0.0.1:59997",
        ollama_llm_model="gemma2:2b",
        ollama_embed_model="nomic-embed-text",
        ollama_vision_model="llava",
        sqlite_db_path=tmp_path / "bot_test.db",
        media_dir=tmp_path / "media",
    )


@pytest.fixture
def test_db(mock_settings):
    return Database(mock_settings.sqlite_db_path)


@pytest.fixture
def test_ai(mock_settings):
    return AIEngine(base_url=mock_settings.ollama_base_url)


@pytest.fixture
def mock_context(mock_settings, test_db, test_ai):
    ctx = MagicMock()
    ctx.bot_data = {
        "settings": mock_settings,
        "db": test_db,
        "ai_engine": test_ai,
    }
    ctx.args = []
    ctx.bot = AsyncMock()
    return ctx


def make_update(user_id: int, text: str = ""):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "test_user"
    update.message = AsyncMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = user_id
    return update


# -----------------------------------------------------------------------------
# Acceptance Test T1: Authorization Guard
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthorized_user_blocked(mock_context):
    """Test T1: Requests from unauthorized user IDs must be rejected."""
    unauthorized_update = make_update(user_id=999999, text="/start")

    @authorized_only
    async def dummy_handler(update, context):
        return "Allowed"

    result = await dummy_handler(unauthorized_update, mock_context)
    assert result is None
    unauthorized_update.message.reply_text.assert_awaited_once_with(
        "⛔ Access Denied. This Slothbox instance is private."
    )


@pytest.mark.asyncio
async def test_authorized_user_allowed(mock_context):
    authorized_update = make_update(user_id=777777, text="/start")

    @authorized_only
    async def dummy_handler(update, context):
        return "Allowed"

    result = await dummy_handler(authorized_update, mock_context)
    assert result == "Allowed"


# -----------------------------------------------------------------------------
# Command Handler Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_and_help_commands(mock_context):
    update = make_update(user_id=777777, text="/start")
    await start_command(update, mock_context)
    assert update.message.reply_text.await_count == 1
    call_args = update.message.reply_text.call_args[0][0]
    assert "Slothbox" in call_args
    assert "Private Second Brain" in call_args
    assert "/search" in call_args
    assert "/ask" in call_args
    assert "/get" in call_args

    update = make_update(user_id=777777, text="/help")
    await help_command(update, mock_context)
    help_msg = update.message.reply_text.call_args[0][0]
    for cmd in ["/search", "/ask", "/get", "/read", "/recent", "/stats", "/save", "/digest", "/keep", "/archive", "/voice", "/start", "/help"]:
        assert cmd in help_msg


@pytest.mark.asyncio
async def test_stats_empty_and_populated(mock_context, test_db):
    update = make_update(user_id=777777, text="/stats")
    # Empty DB (Acceptance Test T9)
    await stats_command(update, mock_context)
    msg = update.message.reply_text.call_args[0][0]
    assert "0 items" in msg

    # Insert items
    item_id = test_db.insert_item("Note", "text")
    test_db.update_item_success(item_id, "Summary", ["ai", "python"])

    await stats_command(update, mock_context)
    populated_msg = update.message.reply_text.call_args[0][0]
    assert "Total items:* 1" in populated_msg
    assert "#ai" in populated_msg


@pytest.mark.asyncio
async def test_recent_command(mock_context, test_db):
    update = make_update(user_id=777777, text="/recent")
    # Empty stash
    await recent_command(update, mock_context)
    assert "empty" in update.message.reply_text.call_args[0][0]

    # Add items
    id1 = test_db.insert_item("Python note", "text")
    test_db.update_item_success(id1, "Python is great", ["python"])

    await recent_command(update, mock_context)
    recent_msg = update.message.reply_text.call_args[0][0]
    assert "#1" in recent_msg
    assert "Python is great" in recent_msg


@pytest.mark.asyncio
async def test_search_offline_ollama(mock_context, test_db):
    """Test T5: Search with offline Ollama must reply with explicit error message, no fallback."""
    update = make_update(user_id=777777, text="/search machine learning")
    mock_context.args = ["machine", "learning"]

    # Insert an item with embedding
    id1 = test_db.insert_item("ML note", "text")
    test_db.save_embedding(id1, np.array([0.1, 0.2, 0.3], dtype=np.float32))

    await search_command(update, mock_context)
    error_reply = update.message.reply_text.call_args[0][0]
    assert "Slothbox search failed: embedding model unavailable." in error_reply


@pytest.mark.asyncio
async def test_keep_and_archive_commands(mock_context, test_db):
    item_id = test_db.insert_item("Forgotten Note", "text")
    test_db.update_item_success(item_id, "Summary", ["tag"])

    # Keep command
    update = make_update(user_id=777777, text=f"/keep {item_id}")
    mock_context.args = [str(item_id)]
    await keep_command(update, mock_context)
    assert "Kept item" in update.message.reply_text.call_args[0][0]

    item = test_db.get_item(item_id)
    assert item["opened_at"] is not None

    # Archive command (Acceptance Test T7)
    update = make_update(user_id=777777, text=f"/archive {item_id}")
    mock_context.args = [str(item_id)]
    await archive_command(update, mock_context)
    assert "Archived item" in update.message.reply_text.call_args[0][0]

    item_archived = test_db.get_item(item_id)
    assert item_archived["is_archived"] == 1
    assert item_archived["archived_at"] is not None


@pytest.mark.asyncio
async def test_get_and_read_commands(mock_context, test_db, tmp_path):
    # 1. Text item retrieval
    id_text = test_db.insert_item("My secret formula", "text")
    test_db.update_item_success(id_text, "Summary formula", ["secret"])

    update = make_update(user_id=777777, text=f"/get {id_text}")
    mock_context.args = [str(id_text)]
    await get_command(update, mock_context)
    mock_context.bot.send_message.assert_awaited()
    assert "My secret formula" in mock_context.bot.send_message.call_args[1]["text"]

    # 2. Image item retrieval
    test_img = tmp_path / "test.jpg"
    test_img.write_bytes(b"fake image data")
    id_img = test_db.insert_item("Poster", "image", file_path=str(test_img))
    test_db.update_item_success(id_img, "Poster summary", ["art"])

    update_img = make_update(user_id=777777, text=f"/get {id_img}")
    mock_context.args = [str(id_img)]
    await get_command(update_img, mock_context)
    mock_context.bot.send_photo.assert_awaited()

    # 3. Read command for web article
    id_url = test_db.insert_item("https://example.com/article", "url")
    test_db.update_item_success(id_url, "Article summary", ["news"], full_text="Full article clean body text here")

    update_read = make_update(user_id=777777, text=f"/read {id_url}")
    mock_context.args = [str(id_url)]
    await read_command(update_read, mock_context)
    assert "Full article clean body text here" in update_read.message.reply_text.call_args[0][0]

