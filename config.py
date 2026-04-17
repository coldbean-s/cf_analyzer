"""Server-level configuration from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file in development
load_dotenv(Path(__file__).parent / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# --- Database ---
DATABASE_URL: str = _env("DATABASE_URL", "postgresql+asyncpg://postgres:dev@localhost:5432/postgres")
DATABASE_URL_SYNC: str = DATABASE_URL.replace("+asyncpg", "")

# --- Auth ---
JWT_SECRET: str = _env("JWT_SECRET", "change-me-in-production-at-least-32-bytes!!")
GITHUB_CLIENT_ID: str = _env("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET: str = _env("GITHUB_CLIENT_SECRET")

# --- Encryption ---
ENCRYPTION_KEY: str = _env("ENCRYPTION_KEY")  # Fernet key (base64, 32 bytes)

# --- LLM defaults (for users who haven't configured their own) ---
DEFAULT_LLM: str = _env("DEFAULT_LLM", "claude")
DEFAULT_LLM_KEY: str = _env("DEFAULT_LLM_KEY")

# --- Chrome ---
MAX_CHROME_INSTANCES: int = int(_env("MAX_CHROME_INSTANCES", "5"))
CF_HEADLESS: bool = _env("CF_HEADLESS", "false").lower() == "true"

# --- CORS ---
ALLOWED_ORIGINS: list[str] = [
    o.strip() for o in _env("ALLOWED_ORIGINS", "http://localhost:6010").split(",") if o.strip()
]

# --- Admin ---
ADMIN_GITHUB_IDS: set[int] = {
    int(x.strip()) for x in _env("ADMIN_GITHUB_IDS", "").split(",") if x.strip()
}
