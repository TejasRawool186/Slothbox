"""Configuration module for Slothbox."""

import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

# Load .env file from project root if it exists
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_allowed_user_id: int
    ollama_base_url: str
    ollama_llm_model: str
    ollama_embed_model: str
    ollama_vision_model: str
    sqlite_db_path: Path
    media_dir: Path
    ollama_rag_model: str = "qwen3:4b"
    backboard_api_key: str = ""
    sentry_auth_token: str = ""
    elevenlabs_api_key: str = ""

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Settings":
        if env_path:
            load_dotenv(env_path, override=True)

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_user_raw = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()

        # Validate mandatory Telegram token
        if not token or token == "your_telegram_bot_token_here":
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is missing or not configured. "
                "Please configure it in your .env file."
            )

        # Validate mandatory Allowed User ID
        if not allowed_user_raw or allowed_user_raw == "123456789":
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_ID is missing or not configured. "
                "Please set your numeric Telegram User ID from @userinfobot."
            )

        try:
            allowed_user_id = int(allowed_user_raw)
        except ValueError as exc:
            raise ValueError(
                f"Invalid TELEGRAM_ALLOWED_USER_ID '{allowed_user_raw}'. Must be an integer."
            ) from exc

        # Ollama local configuration
        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        llm_model = os.getenv("OLLAMA_LLM_MODEL", "gemma2:2b").strip()
        embed_model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text").strip()
        vision_model = os.getenv("OLLAMA_VISION_MODEL", "moondream").strip()
        rag_model = os.getenv("OLLAMA_RAG_MODEL", "qwen3:4b").strip()

        # Storage paths
        db_path_str = os.getenv("SQLITE_DB_PATH", "./data/slothbox.db").strip()
        db_path = (PROJECT_ROOT / db_path_str).resolve() if not Path(db_path_str).is_absolute() else Path(db_path_str)
        
        media_dir_str = os.getenv("MEDIA_DIR", "./data/media").strip()
        media_dir = (PROJECT_ROOT / media_dir_str).resolve() if not Path(media_dir_str).is_absolute() else Path(media_dir_str)

        # Ensure storage directories exist
        db_path.parent.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            telegram_bot_token=token,
            telegram_allowed_user_id=allowed_user_id,
            ollama_base_url=ollama_url,
            ollama_llm_model=llm_model,
            ollama_embed_model=embed_model,
            ollama_vision_model=vision_model,
            ollama_rag_model=rag_model,
            sqlite_db_path=db_path,
            media_dir=media_dir,
            backboard_api_key=os.getenv("BACKBOARD_API_KEY", "").strip(),
            sentry_auth_token=os.getenv("SENTRY_AUTH_TOKEN", "").strip(),
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", "").strip(),
        )


# Global settings instance loaded on demand
settings: Settings | None = None


def get_settings() -> Settings:
    global settings
    if settings is None:
        settings = Settings.load()
    return settings
