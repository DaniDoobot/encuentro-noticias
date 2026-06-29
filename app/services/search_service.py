import time
import json
import logging
from typing import List, Set, Dict, Any, Optional

from app.config import settings
from app.services.logger_service import logger_service
from app.services.sheets_service import sheets_service
from app.services.cache_service import cache_service
from app.services.search_providers import (
    SearchProvider,
    DuckDuckGoSearchProvider,
    BingHtmlSearchProvider,
    GoogleNewsRssSearchProvider,
    SerpApiSearchProvider,
    DataForSeoSearchProvider,
    SearchProviderResult
)

logger = logging.getLogger("encuentro-noticias")

def is_true(val) -> bool:
    """
    Helper that interprets various formats of boolean values from Google Sheets (true, yes, sí, 1).
    """
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    val_str = str(val).strip().lower()
    return val_str in ("true", "1", "yes", "sí", "si", "on")

class SearchService:
    def __init__(self):
        self.ddg_provider = DuckDuckGoSearchProvider()
        self.bing_provider = BingHtmlSearchProvider()
        self.rss_provider = GoogleNewsRssSearchProvider()
        self.serpapi_provider = SerpApiSearchProvider()
        self.dataforseo_provider = DataForSeoSearchProvider()
        
        # Keep track of blocked providers for the current execution run
        self.blocked_providers: Set[str] = set()
        self.providers_used_count: Set[str] = set()
        self.provider_errors_count = 0

    def reset_blocked_providers(self):
        """
        Resets provider block states and tracked provider list at the start of a run.
        """
        self.blocked_providers.clear()
        self.providers_used_count.clear()
        self.provider_errors_count = 0
        logger.info("Search providers blocked status reset.")

    def get_and_reset_errors_count(self) -> int:
        """
        Returns the accumulated provider error count and resets it to 0.
        """
        errs = self.provider_errors_count
        self.provider_errors_count = 0
        return errs

    def get_providers_used(self) -> List[str]:
        return list(self.providers_used_count)

    def search_with_fallback(
        self,
        query: str,
        max_pages: int,
        sheet_id: str,
        run_id: str,
        isbn: str,
        config: Dict[str, Any],
        log_callback = None,
        title: str = "",
        author: str = ""
    ) -> List[Dict[str, Any]]:
        """
        Executes a search query using search providers based on configuration.
        Returns a list of dictionaries with key metadata:
        [
            {
                "url": str,
                "provider": str,
                "title": Optional[str],
                "snippet": Optional[str],
                "position": Optional[int],
                "query": str
            }
        ]
        """
        results: List[Dict[str, Any]] = []
        
        # Load configs
        mode = config.get("SEARCH_PROVIDER_MODE", settings.SEARCH_PROVIDER_MODE)
        backoff_seconds = config.get("SEARCH_BACKOFF_SECONDS", settings.SEARCH_BACKOFF_SECONDS)
        timeout = settings.REQUEST_TIMEOUT_SECONDS

        if mode == "domain_index_plus_news":
            from app.services.source_discovery import source_discovery
            # 1. Local index search
            local_matches = source_discovery.find_candidates(
                title=title or query,
                author=author,
                isbn=isbn,
                config=config
            )
            for match in local_matches:
                url = match["url"]
                if not any(r["url"] == url for r in results):
                    results.append({
                        "url": url,
                        "provider": "DomainIndex",
                        "title": match.get("title") or "",
                        "snippet": match.get("snippet") or "",
                        "position": match.get("score"),
                        "query": "local_index"
                    })
            self.providers_used_count.add("DomainIndex")

            max_candidates = int(config.get("MAX_CANDIDATES_PER_BOOK", settings.MAX_CANDIDATES_PER_BOOK))
            enable_internal_search = is_true(config.get("ENABLE_INTERNAL_DOMAIN_SEARCH", settings.ENABLE_INTERNAL_DOMAIN_SEARCH))

            # 2. Internal Domain Search if candidates are few
            if len(results) < max_candidates and enable_internal_search:
                from app.services.internal_search_provider import internal_search_provider
                from app.services.domain_indexer import _enrich_page_metadata
                
                # Fetch active sources
                try:
                    sources = sheets_service.get_active_sources(sheet_id)
                except Exception:
                    sources = []
                    
                domains_limit = int(config.get("INTERNAL_SEARCH_DOMAINS_LIMIT", settings.INTERNAL_SEARCH_DOMAINS_LIMIT))
                domains = [s["domain"] for s in sources if s.get("active", True) and s.get("domain")]
                domains = domains[:domains_limit]

                if domains:
                    logger_service.log(
                        level="INFO",
                        action="SEARCH_PROVIDER_USED",
                        message=f"Buscando libro de forma interna en {len(domains)} dominios",
                        isbn=isbn,
                        detail=f"Domains: {domains}",
                        sheet_id=sheet_id,
                        run_id=run_id
                    )
                    if log_callback:
                        log_callback(run_id, "INFO", "SEARCH_PROVIDER_USED", f"Buscando libro de forma interna en {len(domains)} dominios", isbn, f"Domains: {domains}")
                    
                    new_urls_found = 0
                    for domain in domains:
                        try:
                            items = internal_search_provider.search_domain_for_book(
                                domain=domain,
                                title=title or query,
                                author=author,
                                isbn=isbn,
                                config=config
                            )
                            for item in items:
                                url = item["url"]
                                title_found = item.get("title") or ""
                                snippet_found = item.get("snippet") or ""
                                
                                # Store basic record in SQLite
                                cache_service.upsert_url(
                                    domain=domain,
                                    url=url,
                                    url_normalized=url,
                                    title=title_found,
                                    snippet=snippet_found,
                                    source_type="internal_search"
                                )
                                
                                # Enrich page metadata if enabled
                                enrich_enabled_val = config.get("ENRICH_INDEXED_URLS", settings.ENRICH_INDEXED_URLS)
                                enrich_enabled = is_true(enrich_enabled_val)
                                
                                if enrich_enabled:
                                    enrich_timeout = int(config.get("DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS", settings.DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS))
                                    meta = _enrich_page_metadata(url, timeout=enrich_timeout)
                                    if meta.get("title") or meta.get("snippet"):
                                        cache_service.upsert_url(
                                            domain=domain,
                                            url=url,
                                            url_normalized=url,
                                            title=meta.get("title") or title_found,
                                            snippet=meta.get("snippet") or snippet_found,
                                            source_type="internal_search"
                                        )
                                new_urls_found += 1
                        except Exception as e:
                            logger.warning(f"Internal domain search error on {domain}: {e}")
                            
                    # Re-run SourceDiscovery if we found new candidates
                    if new_urls_found > 0:
                        local_matches = source_discovery.find_candidates(
                            title=title or query,
                            author=author,
                            isbn=isbn,
                            config=config
                        )
                        results = []
                        for match in local_matches:
                            url = match["url"]
                            if not any(r["url"] == url for r in results):
                                results.append({
                                    "url": url,
                                    "provider": "DomainIndex",
                                    "title": match.get("title") or "",
                                    "snippet": match.get("snippet") or "",
                                    "position": match.get("score"),
                                    "query": "local_index"
                                })

            # 3. Complement with Google News RSS (if enabled and still few candidates)
            if len(results) < max_candidates:
                enable_rss_val = config.get("ENABLE_GOOGLE_NEWS_RSS", settings.ENABLE_GOOGLE_NEWS_RSS)
                enable_rss = is_true(enable_rss_val)
                rss_max = int(config.get("DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES", settings.DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES))
                if enable_rss and rss_max > 0:
                    p_name = self.rss_provider.name()
                    if p_name not in self.blocked_providers:
                        self.providers_used_count.add(p_name)
                        logger_service.log(
                            level="INFO",
                            action="SEARCH_PROVIDER_USED",
                            message=f"Buscando query con {p_name} (complemento de index): {query}",
                            isbn=isbn,
                            detail=f"Provider: {p_name}",
                            sheet_id=sheet_id,
                            run_id=run_id
                        )
                        if log_callback:
                            log_callback(run_id, "INFO", "SEARCH_PROVIDER_USED", f"Buscando query con {p_name} (complemento de index): {query}", isbn, f"Provider: {p_name}")

                        res = self.rss_provider.search(query, max_pages=max_pages, timeout=timeout)
                        if res.status == "ok":
                            parsed_items = res.debug.get("organic_results_parsed", [])
                            for item in parsed_items:
                                url = item["url"]
                                if not any(r["url"] == url for r in results):
                                    results.append({
                                        "url": url,
                                        "provider": p_name,
                                        "title": item.get("title"),
                                        "snippet": item.get("snippet"),
                                        "pub_date": item.get("pub_date"),
                                        "position": item.get("position"),
                                        "query": query
                                    })
            return results
        
        enable_serpapi_val = config.get("ENABLE_SERPAPI", settings.ENABLE_SERPAPI)
        enable_serpapi = is_true(enable_serpapi_val)
        serpapi_key = config.get("SERPAPI_API_KEY", settings.SERPAPI_API_KEY)
        
        enable_dataforseo_val = config.get("ENABLE_DATAFORSEO", settings.ENABLE_DATAFORSEO)
        enable_dataforseo = is_true(enable_dataforseo_val)
        dataforseo_login = config.get("DATAFORSEO_LOGIN", settings.DATAFORSEO_LOGIN)
        dataforseo_password = config.get("DATAFORSEO_PASSWORD", settings.DATAFORSEO_PASSWORD)

        need_free_fallback = False
        need_rss_only = False
        primary_external_provider = None
        external_credentials = {}

        if mode == "google_news_only":
            # Exclusive mode: only use GoogleNewsRss, no DDG/Bing/external providers
            need_rss_only = True
        elif mode == "serpapi":
            primary_external_provider = self.serpapi_provider
            external_credentials = {"api_key": serpapi_key}
        elif mode == "dataforseo":
            primary_external_provider = self.dataforseo_provider
            external_credentials = {"login": dataforseo_login, "password": dataforseo_password}
        elif mode == "auto":
            if enable_serpapi and serpapi_key:
                primary_external_provider = self.serpapi_provider
                external_credentials = {"api_key": serpapi_key}
            elif enable_dataforseo and dataforseo_login and dataforseo_password:
                primary_external_provider = self.dataforseo_provider
                external_credentials = {"login": dataforseo_login, "password": dataforseo_password}
            else:
                need_free_fallback = True
        else:  # free_only
            need_free_fallback = True

        # google_news_only: skip all other providers, go straight to RSS
        if need_rss_only:
            p_name = self.rss_provider.name()
            if p_name not in self.blocked_providers:
                self.providers_used_count.add(p_name)
                logger_service.log(
                    level="INFO",
                    action="SEARCH_PROVIDER_USED",
                    message=f"Buscando query con {p_name} (modo google_news_only): {query}",
                    isbn=isbn,
                    detail=f"Provider: {p_name}",
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                if log_callback:
                    log_callback(run_id, "INFO", "SEARCH_PROVIDER_USED", f"Buscando query con {p_name} (modo google_news_only): {query}", isbn, f"Provider: {p_name}")

                res = self.rss_provider.search(query, max_pages=max_pages, timeout=timeout)

                logger_service.log(
                    level="DEBUG",
                    action="SEARCH_PROVIDER_DEBUG",
                    message=f"Debug info para {p_name}",
                    isbn=isbn,
                    detail=json.dumps(res.debug),
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                if log_callback:
                    log_callback(run_id, "DEBUG", "SEARCH_PROVIDER_DEBUG", f"Debug info para {p_name}", isbn, json.dumps(res.debug))

                if res.status == "ok":
                    parsed_items = res.debug.get("organic_results_parsed", [])
                    for item in parsed_items:
                        results.append({
                            "url": item["url"],
                            "provider": p_name,
                            "title": item.get("title"),
                            "snippet": item.get("snippet"),
                            "pub_date": item.get("pub_date"),
                            "position": item.get("position"),
                            "query": query
                        })
                elif res.status == "rate_limited":
                    self.provider_errors_count += 1
                    logger_service.log(
                        level="WARNING",
                        action="SEARCH_PROVIDER_ERROR",
                        message=f"Rate limit en {p_name} para query: {query}",
                        isbn=isbn,
                        detail=f"provider={p_name} | status_code={res.status_code} | query={query}",
                        sheet_id=sheet_id,
                        run_id=run_id
                    )
                    if log_callback:
                        log_callback(run_id, "WARNING", "SEARCH_PROVIDER_ERROR", f"Rate limit en {p_name} para query: {query}", isbn, f"provider={p_name} | status_code={res.status_code} | query={query}")
                    self.blocked_providers.add(p_name)
                else:
                    self.provider_errors_count += 1
                    logger_service.log(
                        level="WARNING",
                        action="SEARCH_PROVIDER_ERROR",
                        message=f"Error en {p_name}: {res.debug.get('error', 'Unknown error')}",
                        isbn=isbn,
                        detail=f"provider={p_name} | status_code={res.status_code} | query={query}",
                        sheet_id=sheet_id,
                        run_id=run_id
                    )
                    if log_callback:
                        log_callback(run_id, "WARNING", "SEARCH_PROVIDER_ERROR", f"Error en {p_name}: {res.debug.get('error', 'Unknown error')}", isbn, f"provider={p_name} | status_code={res.status_code} | query={query}")
            return results

        if primary_external_provider is not None:
            p_name = primary_external_provider.name()
            if p_name not in self.blocked_providers:
                logger_service.log(
                    level="INFO",
                    action="SEARCH_PROVIDER_USED",
                    message=f"Buscando query con proveedor externo {p_name}: {query}",
                    isbn=isbn,
                    detail=f"Provider: {p_name}",
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                if log_callback:
                    log_callback(run_id, "INFO", "SEARCH_PROVIDER_USED", f"Buscando query con proveedor externo {p_name}: {query}", isbn, f"Provider: {p_name}")
                    
                self.providers_used_count.add(p_name)
                
                res = primary_external_provider.search(query, timeout=timeout, **external_credentials)
                
                logger_service.log(
                    level="DEBUG",
                    action="SEARCH_PROVIDER_DEBUG",
                    message=f"Debug info para {p_name}",
                    isbn=isbn,
                    detail=json.dumps(res.debug),
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                if log_callback:
                    log_callback(run_id, "DEBUG", "SEARCH_PROVIDER_DEBUG", f"Debug info para {p_name}", isbn, json.dumps(res.debug))
                    
                if res.status == "ok":
                    parsed_items = res.debug.get("organic_results_parsed", [])
                    for item in parsed_items:
                        results.append({
                            "url": item["url"],
                            "provider": p_name,
                            "title": item.get("title"),
                            "snippet": item.get("snippet"),
                            "position": item.get("position"),
                            "query": query
                        })
                else:
                    self.provider_errors_count += 1
                    logger_service.log(
                        level="WARNING",
                        action="SEARCH_PROVIDER_ERROR",
                        message=f"Error en proveedor externo {p_name}: {res.debug.get('error', 'Unknown error')}",
                        isbn=isbn,
                        detail=f"provider={p_name} | status_code={res.status_code} | query={query}",
                        sheet_id=sheet_id,
                        run_id=run_id
                    )
                    if log_callback:
                        log_callback(run_id, "WARNING", "SEARCH_PROVIDER_ERROR", f"Error en proveedor externo {p_name}: {res.debug.get('error', 'Unknown error')}", isbn, f"provider={p_name} | status_code={res.status_code} | query={query}")
                    
                    self.blocked_providers.add(p_name)
                    
                    if mode == "auto":
                        need_free_fallback = True
            else:
                if mode == "auto":
                    need_free_fallback = True

        if need_free_fallback:
            # 1. Primary & Secondary search fallback list (DDG, Bing)
            primary_providers = [self.ddg_provider, self.bing_provider]
            urls_found = []
            selected_provider_name = ""
            free_result = None

            for provider in primary_providers:
                p_name = provider.name()
                if p_name in self.blocked_providers:
                    continue

                self.providers_used_count.add(p_name)
                
                logger_service.log(
                    level="INFO",
                    action="SEARCH_PROVIDER_USED",
                    message=f"Buscando query con {p_name}: {query}",
                    isbn=isbn,
                    detail=f"Provider: {p_name}",
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                if log_callback:
                    log_callback(run_id, "INFO", "SEARCH_PROVIDER_USED", f"Buscando query con {p_name}: {query}", isbn, f"Provider: {p_name}")

                res = provider.search(query, max_pages=max_pages, timeout=timeout)
                free_result = res
                
                logger_service.log(
                    level="DEBUG",
                    action="SEARCH_PROVIDER_DEBUG",
                    message=f"Debug info para {p_name}",
                    isbn=isbn,
                    detail=json.dumps(res.debug),
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                if log_callback:
                    log_callback(run_id, "DEBUG", "SEARCH_PROVIDER_DEBUG", f"Debug info para {p_name}", isbn, json.dumps(res.debug))
                
                if res.status == "ok":
                    urls_found = res.urls
                    selected_provider_name = p_name
                    break
                elif res.status == "rate_limited":
                    self.provider_errors_count += 1
                    logger_service.log(
                        level="WARNING",
                        action="SEARCH_PROVIDER_ERROR",
                        message=f"Rate limit en {p_name} para query: {query}",
                        isbn=isbn,
                        detail=f"provider={p_name} | status_code={res.status_code} | query={query}",
                        sheet_id=sheet_id,
                        run_id=run_id
                    )
                    if log_callback:
                        log_callback(run_id, "WARNING", "SEARCH_PROVIDER_ERROR", f"Rate limit en {p_name} para query: {query}", isbn, f"provider={p_name} | status_code={res.status_code} | query={query}")
                    
                    self.blocked_providers.add(p_name)
                    logger.warning(f"Temporarily blocking provider {p_name} due to rate limiting.")
                    logger.info(f"Applying backoff delay of {backoff_seconds} seconds...")
                    time.sleep(backoff_seconds)
                else:  # error
                    self.provider_errors_count += 1
                    logger_service.log(
                        level="WARNING",
                        action="SEARCH_PROVIDER_ERROR",
                        message=f"Error en {p_name}: {res.debug.get('error', 'Unknown error')}",
                        isbn=isbn,
                        detail=f"provider={p_name} | status_code={res.status_code} | query={query}",
                        sheet_id=sheet_id,
                        run_id=run_id
                    )
                    if log_callback:
                        log_callback(run_id, "WARNING", "SEARCH_PROVIDER_ERROR", f"Error en {p_name}: {res.debug.get('error', 'Unknown error')}", isbn, f"provider={p_name} | status_code={res.status_code} | query={query}")

            # Add primary organic results
            for url in urls_found:
                meta = {}
                if free_result and free_result.debug:
                    parsed_items = free_result.debug.get("organic_results_parsed", [])
                    for item in parsed_items:
                        if item["url"] == url:
                            meta = item
                            break
                results.append({
                    "url": url,
                    "provider": selected_provider_name,
                    "title": meta.get("title"),
                    "snippet": meta.get("snippet"),
                    "position": meta.get("position"),
                    "query": query
                })

            # 2. Complementary search: Google News RSS (if enabled)
            enable_rss_val = config.get("ENABLE_GOOGLE_NEWS_RSS", settings.ENABLE_GOOGLE_NEWS_RSS)
            enable_rss = is_true(enable_rss_val)
            if enable_rss:
                p_name = self.rss_provider.name()
                if p_name not in self.blocked_providers:
                    self.providers_used_count.add(p_name)
                    
                    logger_service.log(
                        level="INFO",
                        action="SEARCH_PROVIDER_USED",
                        message=f"Buscando query con {p_name}: {query}",
                        isbn=isbn,
                        detail=f"Provider: {p_name}",
                        sheet_id=sheet_id,
                        run_id=run_id
                    )
                    if log_callback:
                        log_callback(run_id, "INFO", "SEARCH_PROVIDER_USED", f"Buscando query con {p_name}: {query}", isbn, f"Provider: {p_name}")

                    res = self.rss_provider.search(query, max_pages=max_pages, timeout=timeout)
                    
                    logger_service.log(
                        level="DEBUG",
                        action="SEARCH_PROVIDER_DEBUG",
                        message=f"Debug info para {p_name}",
                        isbn=isbn,
                        detail=json.dumps(res.debug),
                        sheet_id=sheet_id,
                        run_id=run_id
                    )
                    if log_callback:
                        log_callback(run_id, "DEBUG", "SEARCH_PROVIDER_DEBUG", f"Debug info para {p_name}", isbn, json.dumps(res.debug))
                        
                    if res.status == "ok":
                        parsed_items = res.debug.get("organic_results_parsed", [])
                        for item in parsed_items:
                            url = item["url"]
                            if not any(r["url"] == url for r in results):
                                results.append({
                                    "url": url,
                                    "provider": p_name,
                                    "title": item.get("title"),
                                    "snippet": item.get("snippet"),
                                    "pub_date": item.get("pub_date"),
                                    "position": item.get("position"),
                                    "query": query
                                })
                    elif res.status == "rate_limited":
                        self.provider_errors_count += 1
                        logger_service.log(
                            level="WARNING",
                            action="SEARCH_PROVIDER_ERROR",
                            message=f"Rate limit en {p_name} para query: {query}",
                            isbn=isbn,
                            detail=f"provider={p_name} | status_code={res.status_code} | query={query}",
                            sheet_id=sheet_id,
                            run_id=run_id
                        )
                        if log_callback:
                            log_callback(run_id, "WARNING", "SEARCH_PROVIDER_ERROR", f"Rate limit en {p_name} para query: {query}", isbn, f"provider={p_name} | status_code={res.status_code} | query={query}")
                        self.blocked_providers.add(p_name)
                    else:  # error
                        self.provider_errors_count += 1
                        logger_service.log(
                            level="WARNING",
                            action="SEARCH_PROVIDER_ERROR",
                            message=f"Error en {p_name}: {res.debug.get('error', 'Unknown error')}",
                            isbn=isbn,
                            detail=f"provider={p_name} | status_code={res.status_code} | query={query}",
                            sheet_id=sheet_id,
                            run_id=run_id
                        )
                        if log_callback:
                            log_callback(run_id, "WARNING", "SEARCH_PROVIDER_ERROR", f"Error en {p_name}: {res.debug.get('error', 'Unknown error')}", isbn, f"provider={p_name} | status_code={res.status_code} | query={query}")
            else:
                logger_service.log(
                    level="INFO",
                    action="SEARCH_PROVIDER_INFO",
                    message="GoogleNewsRss desactivado por configuración",
                    isbn=isbn,
                    detail="Provider: GoogleNewsRss",
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                if log_callback:
                    log_callback(run_id, "INFO", "SEARCH_PROVIDER_INFO", "GoogleNewsRss desactivado por configuración", isbn, "Provider: GoogleNewsRss")

        return results

search_service = SearchService()
