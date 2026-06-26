import time
import logging
from typing import List, Tuple, Set, Dict, Any

from app.config import settings
from app.services.logger_service import logger_service
from app.services.search_providers import (
    SearchProvider,
    DuckDuckGoSearchProvider,
    BingHtmlSearchProvider,
    GoogleNewsRssSearchProvider,
    SearchProviderRateLimitError,
    SearchProviderError
)

logger = logging.getLogger("encuentro-noticias")

class SearchService:
    def __init__(self):
        self.ddg_provider = DuckDuckGoSearchProvider()
        self.bing_provider = BingHtmlSearchProvider()
        self.rss_provider = GoogleNewsRssSearchProvider()
        
        # Keep track of blocked providers for the current execution run
        self.blocked_providers: Set[str] = set()
        self.provider_errors_count = 0

    def reset_blocked_providers(self):
        """
        Resets provider block states at the start of a run.
        """
        self.blocked_providers.clear()
        self.provider_errors_count = 0
        logger.info("Search providers blocked status reset.")

    def get_and_reset_errors_count(self) -> int:
        """
        Returns the accumulated provider error count and resets it to 0.
        """
        errs = self.provider_errors_count
        self.provider_errors_count = 0
        return errs

    def search_with_fallback(
        self,
        query: str,
        max_pages: int,
        sheet_id: str,
        run_id: str,
        isbn: str,
        config: Dict[str, Any]
    ) -> List[Tuple[str, str]]:
        """
        Executes a search query using active search providers.
        DuckDuckGo is the primary search. If blocked or failing, it falls back to Bing.
        If Google News RSS is enabled, it queries Google News RSS as a complement.
        Returns a list of tuples: (url, provider_name)
        """
        results: List[Tuple[str, str]] = []
        
        # Load configs
        backoff_seconds = config.get("SEARCH_BACKOFF_SECONDS", settings.SEARCH_BACKOFF_SECONDS)
        enable_rss = config.get("ENABLE_GOOGLE_NEWS_RSS", settings.ENABLE_GOOGLE_NEWS_RSS)
        timeout = settings.REQUEST_TIMEOUT_SECONDS

        # 1. Primary & Secondary search fallback list
        primary_providers = [self.ddg_provider, self.bing_provider]
        urls_found = []
        selected_provider_name = ""

        for provider in primary_providers:
            p_name = provider.name()
            if p_name in self.blocked_providers:
                continue

            logger_service.log(
                level="INFO",
                action="SEARCH_PROVIDER_USED",
                message=f"Buscando query con {p_name}: {query}",
                isbn=isbn,
                detail=f"Provider: {p_name}",
                sheet_id=sheet_id,
                run_id=run_id
            )

            try:
                urls_found = provider.search(query, max_pages=max_pages, timeout=timeout)
                selected_provider_name = p_name
                break
            except SearchProviderRateLimitError as e:
                self.provider_errors_count += 1
                logger_service.log(
                    level="WARNING",
                    action="SEARCH_PROVIDER_ERROR",
                    message=f"Rate limit en {p_name} para query: {query}",
                    isbn=isbn,
                    detail=f"provider={p_name} | status_code={e.status_code} | query={query} | message={e.message}",
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                self.blocked_providers.add(p_name)
                logger.warning(f"Temporarily blocking provider {p_name} due to rate limiting.")
                
                logger.info(f"Applying backoff delay of {backoff_seconds} seconds...")
                time.sleep(backoff_seconds)
            except SearchProviderError as e:
                self.provider_errors_count += 1
                logger_service.log(
                    level="WARNING",
                    action="SEARCH_PROVIDER_ERROR",
                    message=f"Error en {p_name}: {e.message}",
                    isbn=isbn,
                    detail=f"provider={p_name} | status_code={e.status_code} | query={query} | message={e.message}",
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                continue

        # Add organic results
        for url in urls_found:
            results.append((url, selected_provider_name))

        # 2. Complementary search: Google News RSS (if enabled)
        if enable_rss:
            p_name = self.rss_provider.name()
            if p_name not in self.blocked_providers:
                try:
                    rss_urls = self.rss_provider.search(query, max_pages=max_pages, timeout=timeout)
                    for url in rss_urls:
                        if not any(r[0] == url for r in results):
                            results.append((url, p_name))
                except SearchProviderRateLimitError as e:
                    self.provider_errors_count += 1
                    logger_service.log(
                        level="WARNING",
                        action="SEARCH_PROVIDER_ERROR",
                        message=f"Rate limit en {p_name} para query: {query}",
                        isbn=isbn,
                        detail=f"provider={p_name} | status_code={e.status_code} | query={query} | message={e.message}",
                        sheet_id=sheet_id,
                        run_id=run_id
                    )
                    self.blocked_providers.add(p_name)
                except Exception as e:
                    logger.debug(f"Google News RSS complementary search failed for query '{query}': {e}")

        return results

search_service = SearchService()
