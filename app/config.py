import base64
import json
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    APP_ENV: str = "local"
    ADMIN_TOKEN: Optional[str] = None
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    GOOGLE_SERVICE_ACCOUNT_JSON_BASE64: Optional[str] = None
    GOOGLE_SHEET_ID: str = "121MqN4CFpCBOvJ__cOlpcIvKx3gdNu5tuKtN624td8c"
    GOOGLE_SHARE_WITH_EMAIL: Optional[str] = None
    MIN_MATCH_SCORE: int = 75
    MAX_BOOKS_PER_RUN: int = 10
    MAX_SEARCH_PAGES_PER_QUERY: int = 3
    MAX_CANDIDATES_PER_BOOK: int = 50
    REQUEST_TIMEOUT_SECONDS: int = 20
    SCRAPER_USER_AGENT: str = "Mozilla/5.0 compatible encuentro-noticias"
    SEARCH_DELAY_SECONDS: int = 3
    SEARCH_BACKOFF_SECONDS: int = 30
    MAX_QUERIES_PER_BOOK: int = 20
    ENABLE_GOOGLE_NEWS_RSS: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def get_google_credentials(self) -> Optional[dict]:
        if not self.GOOGLE_SERVICE_ACCOUNT_JSON_BASE64:
            return None
        try:
            # Handle padding issues by adding equal signs if necessary
            missing_padding = len(self.GOOGLE_SERVICE_ACCOUNT_JSON_BASE64) % 4
            b64_str = self.GOOGLE_SERVICE_ACCOUNT_JSON_BASE64
            if missing_padding:
                b64_str += '=' * (4 - missing_padding)
            decoded = base64.b64decode(b64_str).decode("utf-8")
            return json.loads(decoded)
        except Exception as e:
            raise ValueError(f"Error decoding GOOGLE_SERVICE_ACCOUNT_JSON_BASE64: {e}")

settings = Settings()
