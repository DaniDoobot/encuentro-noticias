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
    MAX_QUERIES_PER_BOOK: int = 12
    ENABLE_GOOGLE_NEWS_RSS: bool = True
    SEARCH_PROVIDER_MODE: str = "auto"
    ENABLE_SERPAPI: bool = False
    SERPAPI_API_KEY: Optional[str] = None
    ENABLE_DATAFORSEO: bool = False
    DATAFORSEO_LOGIN: Optional[str] = None
    DATAFORSEO_PASSWORD: Optional[str] = None
    BLOCK_PROVIDER_FOR_FULL_RUN: bool = False
    # Domain indexer
    ENABLE_DOMAIN_INDEX: bool = True
    DOMAIN_INDEX_MAX_URLS_PER_DOMAIN: int = 500
    DOMAIN_INDEX_REFRESH_DAYS: int = 7
    DOMAIN_INDEX_MIN_SCORE: int = 70
    DOMAIN_INDEX_DB_PATH: str = "data/reviews_index.sqlite"
    DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES: int = 10
    ENRICH_INDEXED_URLS: bool = True
    DOMAIN_INDEX_ENRICH_MAX_PER_DOMAIN: int = 200
    DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS: int = 10
    DISCOVER_INTERNAL_ARTICLE_LINKS: bool = True
    DOMAIN_INDEX_INTERNAL_LINK_DEPTH: int = 1
    DOMAIN_INDEX_MAX_INTERNAL_LINKS_PER_PAGE: int = 50
    ENABLE_INTERNAL_DOMAIN_SEARCH: bool = True
    INTERNAL_SEARCH_MAX_QUERIES_PER_BOOK: int = 4
    INTERNAL_SEARCH_MAX_RESULTS_PER_DOMAIN: int = 10
    INTERNAL_SEARCH_TIMEOUT_SECONDS: int = 5
    INTERNAL_SEARCH_DOMAINS_LIMIT: int = 10
    DEFAULT_INCLUDE_UNKNOWN_DATES: bool = True
    DEFAULT_DATE_MIN: Optional[str] = None
    MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH: int = 5
    MIN_CANDIDATES_BEFORE_AI: int = 1
    ENABLE_CASCADE_SEARCH: bool = True
    ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS: bool = True
    ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH: bool = True
    DEFAULT_DATE_MAX: Optional[str] = None
    WORDPRESS_BASE_URL: Optional[str] = None
    WORDPRESS_USERNAME: Optional[str] = None
    WORDPRESS_APPLICATION_PASSWORD: Optional[str] = None
    WORDPRESS_POST_STATUS: str = "draft"
    WORDPRESS_POST_TYPE: str = "posts"
    WORDPRESS_DEFAULT_CATEGORY_ID: Optional[int] = None
    LOG_RETENTION_DAYS: int = 30
    LOG_MAX_ROWS: int = 1000
    DESCARTES_RETENTION_DAYS: int = 30
    DESCARTES_MAX_ROWS: int = 1000
    DEBUG_SEARCH_QUERIES: bool = False




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
