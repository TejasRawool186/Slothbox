"""Tests for configuration loader."""

import os
import pytest
from pathlib import Path
from src.config import Settings


def test_settings_missing_token(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN is missing"):
        Settings.load()


def test_settings_missing_allowed_user_id(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:valid_token")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)

    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_ID is missing"):
        Settings.load()


def test_settings_invalid_allowed_user_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:valid_token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "not-a-number")

    with pytest.raises(ValueError, match="Must be an integer"):
        Settings.load()


def test_settings_valid_load(monkeypatch, tmp_path):
    db_file = tmp_path / "test.db"
    media_folder = tmp_path / "media"
    
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999999:ABCDEF")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "424242")
    monkeypatch.setenv("SQLITE_DB_PATH", str(db_file))
    monkeypatch.setenv("MEDIA_DIR", str(media_folder))

    cfg = Settings.load()
    assert cfg.telegram_bot_token == "999999:ABCDEF"
    assert cfg.telegram_allowed_user_id == 424242
    assert cfg.ollama_base_url == "http://localhost:11434"
    assert cfg.sqlite_db_path == db_file
    assert cfg.media_dir == media_folder
    assert media_folder.exists()
