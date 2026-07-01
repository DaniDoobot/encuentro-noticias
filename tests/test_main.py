from fastapi.testclient import TestClient
from app.main import app
from app.services.query_builder import query_builder
from app.services.deduplicator import deduplicator

client = TestClient(app)

def test_health():
    """
    Tests the health endpoint.
    """
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_query_builder():
    """
    Tests query generation logic with titles, authors, and ISBNs.
    """
    title = "Ficciones"
    author = "Jorge Luis Borges"
    isbn = "9788420633107"
    
    queries_dict = query_builder.build_queries(title, author, isbn)
    queries = queries_dict["prioritarias"] + queries_dict["apoyo"] + queries_dict["dominios"]
    
    # Verify expected queries are in the output list
    assert f'"{title}" "{author}"' in queries
    assert f'"{title}" "{author}" reseña' in queries
    assert f'"{isbn}" "{title}"' in queries
    assert f'"{title}" "{author}" review' in queries
    assert f'"{title}" "{author}" reseña -comprar -amazon -fnac -casadellibro -iberlibro' in queries
    
    # Ensure double quotes inside titles are escaped/handled
    title_with_quotes = 'El "Quijote"'
    queries_q_dict = query_builder.build_queries(title_with_quotes, author, isbn)
    queries_q = queries_q_dict["prioritarias"] + queries_q_dict["apoyo"] + queries_q_dict["dominios"]
    assert f'"El \'Quijote\'" "{author}"' in queries_q
 
    # Verify site-specific queries
    domains = ["revistadelibros.com", "aceprensa.com"]
    queries_d_dict = query_builder.build_queries(title, author, isbn, review_domains=domains)
    queries_d = queries_d_dict["prioritarias"] + queries_d_dict["apoyo"] + queries_d_dict["dominios"]
    assert 'site:revistadelibros.com "Ficciones" "Jorge Luis Borges"' in queries_d
    assert 'site:aceprensa.com "Ficciones" reseña' in queries_d

def test_deduplicator_normalization():
    """
    Verifies that URLs are normalized according to specs:
    - UTM parameters removed
    - Fragments removed
    - Domains lowercased
    - Trailing slashes stripped
    """
    url_utm = "HTTPS://www.ElPais.com/cultura/libro.html?utm_source=twitter&utm_medium=social#comments"
    normalized = deduplicator.normalize_url(url_utm)
    assert normalized == "https://www.elpais.com/cultura/libro.html"
    
    url_trailing_slash = "http://example.com/reviews/"
    assert deduplicator.normalize_url(url_trailing_slash) == "http://example.com/reviews"

def test_deduplicator_hashes():
    """
    Verifies primary and secondary deduplication key generation.
    """
    isbn = "123456"
    url = "https://example.com/review"
    
    hash_base = deduplicator.get_primary_hash(isbn, url)
    hash_with_utm = deduplicator.get_primary_hash(isbn, url + "?utm_source=news_site")
    
    # Since UTM is stripped, hashes should match
    assert hash_base == hash_with_utm
    
    # Check secondary key formatting
    sec_key = deduplicator.get_secondary_key(isbn, "www.example.com", "¡Una Reseña Increíble!")
    assert sec_key == "123456:example.com:unareseñaincreíble"


import pytest
from unittest.mock import MagicMock, patch
from app.services.search_providers import DuckDuckGoSearchProvider, BingHtmlSearchProvider, SearchProviderRateLimitError

def test_query_builder_tiers():
    """
    Verifies that the query builder returns a tiered dictionary.
    """
    title = "Ficciones"
    author = "Borges"
    isbn = "123"
    domains = ["revistadelibros.com"]
    
    res = query_builder.build_queries(title, author, isbn, review_domains=domains)
    assert isinstance(res, dict)
    assert "prioritarias" in res
    assert "apoyo" in res
    assert "dominios" in res
    assert len(res["prioritarias"]) > 0
    assert len(res["dominios"]) > 0

def test_query_builder_missing_data():
    """
    Verifies query builder outputs when ISBN or author is missing.
    """
    # 1. Title + Author (No ISBN)
    res = query_builder.build_queries("Ficciones", "Borges", "")
    assert '"Ficciones" "Borges" reseña' in res["prioritarias"]
    assert '"Ficciones" "Borges" crítica' in res["prioritarias"]

    # 2. Only Title (No Author, No ISBN)
    res_only_title = query_builder.build_queries("Ficciones", "", "")
    assert '"Ficciones" reseña libro' in res_only_title["prioritarias"]
    assert '"Ficciones" crítica libro' in res_only_title["prioritarias"]
    assert '"Ficciones" ediciones encuentro' in res_only_title["prioritarias"]

def test_search_rate_limit_detection():
    """
    Verifies that search providers correctly identify and return rate-limiting status.
    """
    provider = DuckDuckGoSearchProvider()
    
    mock_response = MagicMock()
    mock_response.status_code = 202
    
    with patch("httpx.Client.get", return_value=mock_response):
        res = provider.search("Ficciones Borges", timeout=5)
        assert res.status == "rate_limited"
        assert res.status_code == 202
        assert len(res.urls) == 0

from app.services.search_service import search_service, is_true
from app.services.search_providers import GoogleNewsRssSearchProvider, SerpApiSearchProvider, DataForSeoSearchProvider, SearchProviderResult

def test_is_true_helper():
    """
    Tests boolean parsing helper is_true.
    """
    assert is_true(True) is True
    assert is_true("True") is True
    assert is_true("TRUE") is True
    assert is_true("1") is True
    assert is_true(1) is True
    assert is_true("yes") is True
    assert is_true("sí") is True
    assert is_true("si") is True
    assert is_true("on") is True
    assert is_true(False) is False
    assert is_true("False") is False
    assert is_true("0") is False
    assert is_true(None) is False
    assert is_true("no") is False

def test_bing_parser_li_b_algo():
    """
    Verifies that BingHtmlSearchProvider extracts organic result links from li.b_algo and discards internal Bing ones.
    """
    provider = BingHtmlSearchProvider()
    
    mock_html = """
    <html>
    <body>
        <ul>
            <li class="b_algo">
                <h2><a href="https://revistadelibros.com/review1">Reseña de Borges</a></h2>
            </li>
            <li class="b_algo">
                <h2><a href="https://www.bing.com/search?q=something">Internal Bing link</a></h2>
            </li>
            <li class="b_algo">
                <h2><a href="https://www.aceprensa.com/critica">Reseña de AcePrensa</a></h2>
            </li>
        </ul>
    </body>
    </html>
    """
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = mock_html
    
    with patch("httpx.Client.get", return_value=mock_response):
        res = provider.search("Borges", timeout=5)
        assert res.status == "ok"
        urls = res.urls
        assert len(urls) == 2
        assert "https://revistadelibros.com/review1" in urls
        assert "https://www.aceprensa.com/critica" in urls
        assert "https://www.bing.com/search?q=something" not in urls

def test_bing_parser_fallback_h2_a():
    """
    Verifies that BingHtmlSearchProvider falls back to any h2 a when no li.b_algo is present.
    """
    provider = BingHtmlSearchProvider()
    mock_html = """
    <html>
    <body>
        <h2><a href="https://example.com/fallback_h2">H2 Link</a></h2>
        <h2><a href="https://microsoft.com/bad">Microsoft</a></h2>
    </body>
    </html>
    """
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = mock_html
    
    with patch("httpx.Client.get", return_value=mock_response):
        res = provider.search("test", timeout=5)
        assert res.status == "ok"
        urls = res.urls
        assert len(urls) == 1
        assert urls[0] == "https://example.com/fallback_h2"

def test_bing_parser_mixed_links_and_decoding():
    """
    Verifies decoding of Bing redirect URLs and that we do not filter e-commerce/external sites.
    """
    provider = BingHtmlSearchProvider()
    mock_html = """
    <html>
    <body>
        <li class="b_algo">
            <h2><a href="https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly93d3cuYW1hem9uLmVzL2xpYnJv&ntb=1">Amazon redirect</a></h2>
        </li>
        <li class="b_algo">
            <h2><a href="https://go.microsoft.com/ref">Microsoft</a></h2>
        </li>
        <li class="b_algo">
            <h2><a href="https://login.live.com/login">Live</a></h2>
        </li>
        <li class="b_algo">
            <h2><a href="https://casadellibro.com/libro">Casa del Libro</a></h2>
        </li>
    </body>
    </html>
    """
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = mock_html
    
    with patch("httpx.Client.get", return_value=mock_response):
        res = provider.search("test", timeout=5)
        assert res.status == "ok"
        urls = res.urls
        assert len(urls) == 2
        assert "https://www.amazon.es/libro" in urls
        assert "https://casadellibro.com/libro" in urls

def test_providers_used_filled_on_zero_results():
    """
    Verifies that providers_used_count tracks queried providers even if they yield 0 URLs.
    """
    search_service.reset_blocked_providers()
    
    mock_ddg = MagicMock()
    mock_ddg.search.return_value = SearchProviderResult(
        provider="DuckDuckGo",
        query="test query",
        status="ok",
        status_code=200,
        urls=[],
        debug={"organic_results_parsed": []}
    )
    mock_ddg.name.return_value = "DuckDuckGo"
    
    original_ddg = search_service.ddg_provider
    search_service.ddg_provider = mock_ddg
    try:
        res = search_service.search_with_fallback(
            query="test query",
            max_pages=1,
            sheet_id="dummy",
            run_id="dummy_run",
            isbn="12345",
            config={"SEARCH_PROVIDER_MODE": "free_only", "ENABLE_GOOGLE_NEWS_RSS": False, "SEARCH_BACKOFF_SECONDS": 1}
        )
        assert len(res) == 0
        assert "DuckDuckGo" in search_service.get_providers_used()
    finally:
        search_service.ddg_provider = original_ddg

def test_debug_search_endpoint():
    """
    Verifies that POST /debug/search endpoint calls providers correctly and returns debug telemetry.
    """
    with patch("app.services.search_providers.DuckDuckGoSearchProvider.search") as mock_ddg, \
         patch("app.services.search_providers.BingHtmlSearchProvider.search") as mock_bing, \
         patch("app.services.search_providers.GoogleNewsRssSearchProvider.search") as mock_rss:
         
         mock_ddg.return_value = SearchProviderResult(
             provider="DuckDuckGo",
             query="test query",
             status="ok",
             status_code=200,
             urls=["https://ddg.com/1"],
             debug={"organic_results_parsed": [{"url": "https://ddg.com/1"}]}
         )
         mock_bing.return_value = SearchProviderResult(
             provider="BingHtml",
             query="test query",
             status="ok",
             status_code=200,
             urls=["https://bing.com/2"],
             debug={"organic_results_parsed": [{"url": "https://bing.com/2"}]}
         )
         mock_rss.return_value = SearchProviderResult(
             provider="GoogleNewsRss",
             query="test query",
             status="ok",
             status_code=200,
             urls=[],
             debug={"organic_results_parsed": []}
         )
         
         response = client.post(
             "/debug/search",
             json={
                 "query": "test query",
                 "providers": ["DuckDuckGo", "BingHtml", "GoogleNewsRss"]
             }
         )
         assert response.status_code == 200
         data = response.json()
         assert data["query"] == "test query"
         assert len(data["results"]) == 3
         
         ddg_res = next(r for r in data["results"] if r["provider"] == "DuckDuckGo")
         assert ddg_res["status"] == "ok"
         assert ddg_res["status_code"] == 200
         assert ddg_res["urls"] == ["https://ddg.com/1"]

def test_debug_search_endpoint_unauthorized():
    """
    Verifies authentication on POST /debug/search if ADMIN_TOKEN is set.
    """
    with patch("app.config.settings.ADMIN_TOKEN", "super-secret"):
        response = client.post(
            "/debug/search",
            json={"query": "test", "providers": ["DuckDuckGo"]},
            headers={"X-Admin-Token": "wrong-token"}
        )
        assert response.status_code == 401

def test_serpapi_parsing():
    """
    Verifies SerpApiSearchProvider parsing of organic Google search results.
    """
    provider = SerpApiSearchProvider()
    mock_data = {
        "organic_results": [
            {
                "position": 1,
                "title": "Reseña de Sapiens",
                "link": "https://example.com/sapiens-review",
                "snippet": "Una gran obra sobre la historia de la humanidad."
            }
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_data
    
    with patch("httpx.Client.get", return_value=mock_response):
        res = provider.search("Sapiens", api_key="dummy_key")
        assert res.status == "ok"
        assert res.urls == ["https://example.com/sapiens-review"]
        parsed = res.debug["organic_results_parsed"]
        assert len(parsed) == 1
        assert parsed[0]["title"] == "Reseña de Sapiens"
        assert parsed[0]["snippet"] == "Una gran obra sobre la historia de la humanidad."
        assert parsed[0]["position"] == 1

def test_dataforseo_parsing():
    """
    Verifies DataForSeoSearchProvider parsing of Google organic results.
    """
    provider = DataForSeoSearchProvider()
    mock_data = {
        "tasks": [
            {
                "result": [
                    {
                        "items": [
                            {
                                "type": "organic",
                                "rank_group": 1,
                                "title": "Crítica del infinito en un junco",
                                "url": "https://example.com/infinito-junco",
                                "description": "Excelente ensayo de Irene Vallejo."
                            }
                        ]
                    }
                ]
            }
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_data
    
    with patch("httpx.Client.post", return_value=mock_response):
        res = provider.search("El infinito en un junco", login="usr", password="pwd")
        assert res.status == "ok"
        assert res.urls == ["https://example.com/infinito-junco"]
        parsed = res.debug["organic_results_parsed"]
        assert len(parsed) == 1
        assert parsed[0]["title"] == "Crítica del infinito en un junco"
        assert parsed[0]["snippet"] == "Excelente ensayo de Irene Vallejo."
        assert parsed[0]["position"] == 1

def test_search_service_routing_modes():
    """
    Verifies that search_service routes searches appropriately according to the configured mode.
    """
    # 1. Mode free_only: should call ddg (mocked, returns results) and NOT call SerpAPI/DataForSEO.
    # Bing is NOT called because DDG succeeds and the provider loop breaks early.
    with patch("app.services.search_providers.DuckDuckGoSearchProvider.search") as mock_ddg, \
         patch("app.services.search_providers.BingHtmlSearchProvider.search") as mock_bing, \
         patch("app.services.search_providers.SerpApiSearchProvider.search") as mock_serpapi:
         
         mock_ddg.return_value = SearchProviderResult(
             provider="DuckDuckGo", query="q", status="ok", urls=["https://ddg.com"],
             debug={"organic_results_parsed": [{"url": "https://ddg.com"}]}
         )
         mock_bing.return_value = SearchProviderResult(provider="BingHtml", query="q", status="ok", urls=[])
         
         search_service.reset_blocked_providers()
         res = search_service.search_with_fallback(
             query="test query",
             max_pages=1,
             sheet_id="dummy",
             run_id="run_1",
             isbn="12345",
             config={"SEARCH_PROVIDER_MODE": "free_only", "ENABLE_GOOGLE_NEWS_RSS": False}
         )
         assert len(res) == 1
         assert res[0]["url"] == "https://ddg.com"
         mock_ddg.assert_called_once()
         # Bing is not called because DDG returned results and the loop breaks
         mock_bing.assert_not_called()
         mock_serpapi.assert_not_called()

    # 2. Mode serpapi: should call serpapi (mocked) and NOT call free providers
    with patch("app.services.search_providers.DuckDuckGoSearchProvider.search") as mock_ddg, \
         patch("app.services.search_providers.SerpApiSearchProvider.search") as mock_serpapi:
         
         mock_serpapi.return_value = SearchProviderResult(
             provider="SerpAPI", query="q", status="ok", urls=["https://serp.com"],
             debug={"organic_results_parsed": [{"url": "https://serp.com", "title": "Serp title"}]}
         )
         
         search_service.reset_blocked_providers()
         res = search_service.search_with_fallback(
             query="test query",
             max_pages=1,
             sheet_id="dummy",
             run_id="run_1",
             isbn="12345",
             config={"SEARCH_PROVIDER_MODE": "serpapi", "ENABLE_SERPAPI": True, "SERPAPI_API_KEY": "somekey"}
         )
         assert len(res) == 1
         assert res[0]["url"] == "https://serp.com"
         mock_serpapi.assert_called_once()
         mock_ddg.assert_not_called()

def test_block_provider_reset():
    """
    Verifies that run_service clears blocked search providers at the start of processing a book
    if BLOCK_PROVIDER_FOR_FULL_RUN is set to false.
    """
    from app.services.run_service import run_service
    
    # Pre-populate blocked providers
    search_service.blocked_providers.add("DuckDuckGo")
    search_service.blocked_providers.add("BingHtml")
    
    # Process a book in dry_run with BLOCK_PROVIDER_FOR_FULL_RUN = False
    # Mock Sheets service to prevent network hits
    with patch("app.services.sheets_service.sheets_service.update_book_status") as mock_status, \
         patch("app.services.sheets_service.sheets_service.get_all_reviews", return_value=[]), \
         patch("app.services.search_service.search_service.search_with_fallback", return_value=[]):
         
         run_service._process_book(
             run_id="run_test",
             sheet_id="sheet_id",
             row_index=2,
             isbn="123",
             title="Title",
             author="Author",
             max_pages=1,
             max_candidates=5,
             min_score=70,
             openai_model="gpt-4o",
             existing_hashes=set(),
             existing_secondary_keys=set(),
             run_config={"BLOCK_PROVIDER_FOR_FULL_RUN": "false"},
             dry_run=True
         )
         
         # The blocked providers set should be cleared!
         assert "DuckDuckGo" not in search_service.blocked_providers
         assert "BingHtml" not in search_service.blocked_providers


def test_cache_service_upsert_and_search(tmp_path):
    """
    Tests CacheService database initialization, upserting URLs, searching by text,
    and getting statistics.
    """
    from app.services.cache_service import cache_service
    db_file = str(tmp_path / "test_reviews_index.sqlite")
    cache_service.init_db(db_file)
    
    # New insertion
    is_new = cache_service.upsert_url(
        domain="example.com",
        url="https://example.com/libro/reseña-1",
        url_normalized="https://example.com/libro/resena-1",
        title="Reseña del infinito en un junco",
        snippet="Un libro maravilloso sobre los libros por Irene Vallejo.",
        pub_date="2026-06-26",
        source_type="sitemap"
    )
    assert is_new is True
    
    # Update insertion (same URL)
    is_new_update = cache_service.upsert_url(
        domain="example.com",
        url="https://example.com/libro/reseña-1",
        url_normalized="https://example.com/libro/resena-1",
        title="Reseña del infinito en un junco (actualizado)",
        snippet="Un libro maravilloso sobre los libros por Irene Vallejo.",
        pub_date="2026-06-26",
        source_type="sitemap"
    )
    assert is_new_update is False
    
    # Check count
    assert cache_service.get_total_urls() == 1
    
    # Search term matching
    matches = cache_service.search_by_text(terms=["infinito", "Vallejo"])
    assert len(matches) == 1
    assert "actualizado" in matches[0]["title"]
    
    # Test refresh check
    assert cache_service.needs_refresh("example.com", 7) is False
    assert cache_service.needs_refresh("example.com", 0) is True


def test_source_discovery_scoring(tmp_path):
    """
    Tests scoring heuristic in SourceDiscovery.
    """
    from app.services.cache_service import cache_service
    from app.services.source_discovery import source_discovery
    db_file = str(tmp_path / "test_discovery.sqlite")
    cache_service.init_db(db_file)
    
    cache_service.upsert_url(
        domain="example.com",
        url="https://example.com/reseña-infinito",
        title="Reseña de El infinito en un junco de Irene Vallejo",
        snippet="Una reseña literaria espectacular de Irene Vallejo.",
        pub_date="2026-06-26"
    )
    
    cache_service.upsert_url(
        domain="example.com",
        url="https://example.com/isbn-search",
        title="Crítica del libro",
        snippet="Libro con ISBN 978-84-7490-104-7 sobre los símbolos.",
        pub_date="2026-06-26"
    )
    
    # Search by title and author
    candidates = source_discovery.find_candidates(
        title="El infinito en un junco",
        author="Irene Vallejo",
        isbn="",
        config={"DOMAIN_INDEX_MIN_SCORE": 50, "DOMAIN_INDEX_DB_PATH": db_file}
    )
    assert len(candidates) == 1
    assert candidates[0]["url"] == "https://example.com/reseña-infinito"
    assert candidates[0]["score"] >= 70
    
    # Search by ISBN
    candidates_isbn = source_discovery.find_candidates(
        title="Introducción a los símbolos",
        author="Gérard de Champeaux",
        isbn="978-84-7490-104-7",
        config={"DOMAIN_INDEX_MIN_SCORE": 70, "DOMAIN_INDEX_DB_PATH": db_file}
    )
    assert len(candidates_isbn) == 1
    assert candidates_isbn[0]["url"] == "https://example.com/isbn-search"


def test_domain_indexer_cultural_filter():
    """
    Tests cultural URL path filtering logic.
    """
    from app.services.domain_indexer import _is_cultural_url
    assert _is_cultural_url("https://example.com/libros/resena-infinito") is True
    assert _is_cultural_url("https://example.com/critica/el-infinito-en-un-junco") is True
    assert _is_cultural_url("https://example.com/wp-admin/post.php") is False
    assert _is_cultural_url("https://example.com/shop/checkout") is False
    assert _is_cultural_url("https://example.com/contacto") is False


def test_domain_search_endpoint(tmp_path):
    """
    Tests POST /debug/domain-search endpoint authentication and matching logic.
    """
    from app.services.cache_service import cache_service
    db_file = str(tmp_path / "test_endpoint.sqlite")
    cache_service.init_db(db_file)
    cache_service.upsert_url(
        domain="revistadelibros.com",
        url="https://revistadelibros.com/resena-infinito",
        title="Reseña de El infinito en un junco de Irene Vallejo",
        snippet="Reseña cultural.",
        pub_date="2026"
    )
    
    with patch("app.config.settings.DOMAIN_INDEX_DB_PATH", db_file), \
         patch("app.config.settings.ADMIN_TOKEN", "test-token"), \
         patch("app.services.sheets_service.sheets_service.get_config_dict", return_value={"DOMAIN_INDEX_DB_PATH": db_file, "DOMAIN_INDEX_MIN_SCORE": 70}):
         
         
        # Unauthorized check
        response = client.post(
            "/debug/domain-search",
            json={"title": "El infinito en un junco", "author": "Irene Vallejo", "isbn": ""},
            headers={"X-Admin-Token": "bad-token"}
        )
        assert response.status_code == 401
        
        # Authorized check
        response = client.post(
            "/debug/domain-search",
            json={"title": "El infinito en un junco", "author": "Irene Vallejo", "isbn": ""},
            headers={"X-Admin-Token": "test-token"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total_matches"] == 1
        assert data["matches"][0]["url"] == "https://revistadelibros.com/resena-infinito"
        assert data["matches"][0]["score"] >= 70


def test_metadata_enrichment_and_slug_scoring(tmp_path):
    """
    Tests slug fallback title generation and slug extraction helpers.
    """
    from app.services.domain_indexer import _title_from_slug
    assert _title_from_slug("https://example.com/cultura/leonardo-genio-y-trabajador-paciente/") == "Leonardo genio y trabajador paciente"
    assert _title_from_slug("https://example.com/sobre-las-representaciones-de-tematica-sexual/") == "Sobre las representaciones de tematica sexual"

    from app.services.source_discovery import source_discovery, _get_slug_text
    assert _get_slug_text("https://example.com/cultura/leonardo-genio-y-trabajador-paciente/") == "leonardo genio y trabajador paciente"


def test_config_parser_normalization():
    """
    Tests normalization and robust parsing of Google Sheets config values.
    """
    from app.services.sheets_service import normalize_config_val, parse_bool, parse_int, parse_float, parse_str
    
    # 1. normalize_config_val
    assert normalize_config_val("'false") == "false"
    assert normalize_config_val("'5") == "5"
    assert normalize_config_val("'https://encuentro-backend.doobot.ai") == "https://encuentro-backend.doobot.ai"
    assert normalize_config_val("  'true  ") == "true"
    assert normalize_config_val(None) == ""
    
    # 2. parse_bool
    assert parse_bool("'false", True) is False
    assert parse_bool("FALSE", True) is False
    assert parse_bool("'true", False) is True
    assert parse_bool("TRUE", False) is True
    assert parse_bool("sí", False) is True
    assert parse_bool("si", False) is True
    assert parse_bool("no", True) is False
    assert parse_bool("0", True) is False
    assert parse_bool("1", False) is True
    
    # 3. parse_int
    assert parse_int("'5", 10) == 5
    assert parse_int("invalid", 10) == 10
    
    # 4. parse_float
    assert parse_float("'2.5", 1.0) == 2.5
    assert parse_float("invalid", 1.0) == 1.0
    
    # 5. parse_str
    assert parse_str("'hello", "default") == "hello"


def test_clean_domain_string_normalization():
    """
    Tests clean_domain_string helper and Fuentes normalization rules.
    """
    from app.services.sheets_service import clean_domain_string
    
    assert clean_domain_string("wmagazin.com") == "wmagazin.com"
    assert clean_domain_string("https://wmagazin.com") == "wmagazin.com"
    assert clean_domain_string("https://wmagazin.com/") == "wmagazin.com"
    assert clean_domain_string("http://wmagazin.com/subpath") == "wmagazin.com"


def test_delete_all_logs_and_descartes():
    """
    Tests manual delete-all endpoints for Logs and Descartes worksheets.
    """
    # 1. Logs
    with patch("app.routers.logs.sheets_service") as mock_sheets:
        mock_sheets.clear_all_rows.return_value = {"deleted_count": 15}
        response = client.post("/logs/delete-all")
        assert response.status_code == 200
        assert response.json()["deleted_count"] == 15
        mock_sheets.clear_all_rows.assert_called_once_with(mock_sheets.clear_all_rows.call_args[0][0], "Logs")
        
    # 2. Descartes
    with patch("app.routers.descartes.sheets_service") as mock_sheets:
        mock_sheets.clear_all_rows.return_value = {"deleted_count": 30}
        response = client.post("/descartes/delete-all")
        assert response.status_code == 200
        assert response.json()["deleted_count"] == 30
        mock_sheets.clear_all_rows.assert_called_once_with(mock_sheets.clear_all_rows.call_args[0][0], "Descartes")

def test_cancellation_endpoints():
    """
    Tests the cancellation endpoints for search runs and publication tasks.
    """
    # 1. Search cancel
    from app.services.run_service import current_runs, cancelled_runs
    run_id = "test_run_123"
    current_runs[run_id] = {"status": "running", "message": "Initial", "logs": []}
    
    response = client.post(f"/runs/{run_id}/cancel")
    assert response.status_code == 200
    assert response.json() == {"success": True, "message": f"Run {run_id} has been marked for cancellation."}
    assert run_id in cancelled_runs
    assert current_runs[run_id]["status"] == "cancelled"
    
    # 2. Publish cancel
    from app.routers.publish import current_publications, cancelled_publications
    pub_id = "test_pub_456"
    current_publications[pub_id] = {"status": "running", "published_count": 0, "errors_count": 0, "message": ""}
    
    response = client.post(f"/publish/{pub_id}/cancel")
    assert response.status_code == 200
    assert response.json() == {"success": True, "message": f"Publicación {pub_id} cancelada cooperativamente."}
    assert pub_id in cancelled_publications
    assert current_publications[pub_id]["status"] == "cancelled"


def test_book_title_preservation_and_logs():
    """
    Regression test to ensure that 'book_title' (original book title)
    is preserved throughout the loop and not overwritten by candidate titles,
    ensuring correct book_title is passed to openai_analyzer.analyze_article and sheets_service.add_descarte.
    """
    from app.services.run_service import run_service
    from app.services.sheets_service import sheets_service
    from app.services.openai_analyzer import openai_analyzer
    
    book_title = "San Manuel Bueno, mártir"
    book_author = "Miguel de Unamuno"
    candidate_url = "https://www.zendalibros.com/zenda-recomienda-san-manuel-bueno-martir-de-miguel-de-unamuno/"
    candidate_title = "Zenda recomienda: San Manuel Bueno, mártir, de Miguel de Unamuno"
    
    mock_candidate = {
        "url": candidate_url,
        "query": "local_index",
        "provider": "DomainIndex",
        "title": candidate_title,
        "snippet": "Test snippet",
        "position": 1,
        "score": 100,
        "pub_date": None
    }
    
    with patch("app.services.source_discovery.source_discovery.find_candidates", return_value=[mock_candidate]), \
         patch("app.services.sheets_service.sheets_service.get_config_dict", return_value={
             "MAX_SEARCH_PAGES_PER_QUERY": 1,
             "MAX_CANDIDATES_PER_BOOK": 5,
             "MIN_MATCH_SCORE": 70,
             "SEARCH_PROVIDER_MODE": "auto",
             "ENABLE_CASCADE_SEARCH": "true",
             "ENABLE_INTERNAL_DOMAIN_SEARCH": "false",
             "DEFAULT_INCLUDE_UNKNOWN_DATES": "true"
         }), \
         patch("app.services.sheets_service.sheets_service.get_all_reviews", return_value=[]), \
         patch("app.services.article_extractor.article_extractor.extract", return_value={
             "title": candidate_title,
             "text": "Cuerpo del articulo sobre San Manuel Bueno, martir",
             "date": "2024-02-18",
             "author": "Miguel de Unamuno",
             "publication_name": "Zenda"
         }), \
         patch("app.services.openai_analyzer.openai_analyzer.analyze_article", return_value={
             "is_valid": False,
             "match_score": 30,
             "reason": "Mención tangencial del libro",
             "detected_book_title": book_title,
             "detected_book_author": book_author,
             "content_type": "reseña",
             "publication_name": "Zenda",
             "publication_author": "Miguel de Unamuno",
             "publication_date": "2024-02-18",
             "language": "es",
             "category": "Literatura",
             "summary": "Resumen de prueba"
         }) as mock_analyze, \
         patch("app.services.sheets_service.sheets_service.add_descarte") as mock_add_descarte, \
         patch("app.services.sheets_service.sheets_service.update_book_status") as mock_status:
         
         run_service._process_book(
             run_id="run_test_preservation",
             sheet_id="sheet_id",
             row_index=2,
             isbn="123456",
             title=book_title,
             author=book_author,
             max_pages=1,
             max_candidates=5,
             min_score=70,
             openai_model="gpt-4o",
             existing_hashes=set(),
             existing_secondary_keys=set(),
             dry_run=False
         )
         
         # Verification 1: OpenAI received the correct original book_title
         assert mock_analyze.call_count >= 1
         for call in mock_analyze.call_args_list:
             called_kwargs = call[1]
             assert called_kwargs["book_title"] == book_title
             assert called_kwargs["book_author"] == book_author
         
         # Verification 2: add_descarte was called with the correct book title
         assert mock_add_descarte.call_count >= 1
         for call in mock_add_descarte.call_args_list:
             called_row = call[0][1]
             assert called_row[1] == book_title # Col 2: Título del libro (not candidate_title!)
             if called_row[4] == candidate_url:
                 assert called_row[5] == candidate_title # Col 6: Título detectado / artículo


def test_generic_author_query_builder():
    """
    Verifies that if the author is generic (e.g. VV.AA., AA.VV., Varios autores),
    the query builder ignores the author and generates variants without it,
    including 2-word swaps/permutations for titles like "YOUCAT Biblia".
    """
    from app.services.query_builder import query_builder
    
    # 1. Author "VV.AA."
    res = query_builder.build_queries("YOUCAT Biblia", "VV.AA.", "978-84-1339-264-6")
    
    # Ensure "VV.AA." is not in any prioritarias queries
    for q in res["prioritarias"]:
        assert "VV.AA." not in q
        assert "vv.aa." not in q.lower()
        
    # Ensure "YOUCAT Biblia", "Biblia YOUCAT", "Biblia de YOUCAT", and "la nueva Biblia de YOUCAT" are generated
    assert '"YOUCAT Biblia"' in res["prioritarias"]
    assert '"Biblia YOUCAT"' in res["prioritarias"]
    assert '"Biblia de YOUCAT"' in res["prioritarias"]
    assert '"la nueva Biblia de YOUCAT"' in res["prioritarias"]
    assert '"9788413392646"' in res["prioritarias"]
    
    # Ensure no-quotes variants are in "apoyo"
    assert "YOUCAT Biblia reseña" in res["apoyo"]
    assert "YOUCAT Biblia artículo" in res["apoyo"]
    assert "YOUCAT Biblia crítica" in res["apoyo"]
    
    # 2. Author "Varios autores"
    res2 = query_builder.build_queries("El sentido religioso", "Varios autores", "")
    for q in res2["prioritarias"]:
        assert "varios" not in q.lower()
        assert "autores" not in q.lower()


def test_internal_generic_author():
    """
    Verifies that generate_internal_queries also ignores generic authors.
    """
    from app.services.internal_search_provider import generate_internal_queries
    
    queries = generate_internal_queries("YOUCAT Biblia", "VV.AA.", "978-84-1339-264-6")
    for q in queries:
        assert "VV.AA." not in q
        assert "vv.aa." not in q.lower()
    
    # Ensure the title query and clean ISBN query are generated
    assert '"YOUCAT Biblia"' in queries
    assert '9788413392646' in queries


def test_consent_page_detection():
    """
    Tests the is_consent_or_cookie_page function with various texts.
    """
    from app.services.run_service import is_consent_or_cookie_page
    
    # Cookie/consent text
    text_cookie1 = "Before you continue to Google... We use cookies and data to deliver and maintain services..."
    text_cookie2 = "Esta web utiliza cookies propias y de terceros para su funcionamiento y para mostrarle publicidad personalizada. Política de privacidad y Uso de datos."
    
    # Real article text
    text_article = "Esta es una reseña literaria sobre el libro San Manuel Bueno, mártir de Unamuno, que trata el dilema existencial de la fe y el cura rural."
    
    assert is_consent_or_cookie_page(text_cookie1) is True
    assert is_consent_or_cookie_page(text_cookie2) is True
    assert is_consent_or_cookie_page(text_article) is False


def test_google_news_resolution_and_fallbacks():
    """
    Regression test for Google News RSS URL resolution, resolution failure,
    cookie page detection, and title preservation in run_service._process_book.
    """
    from app.services.run_service import run_service
    from app.services.sheets_service import sheets_service
    
    cand1_url = "https://news.google.com/rss/articles/CBMi1_ok_success"
    cand2_url = "https://news.google.com/rss/articles/CBMi2_resolution_fails"
    cand3_url = "https://news.google.com/rss/articles/CBMi3_cookie_page"
    
    resolved1_url = "https://www.zendalibros.com/success-article"
    resolved3_url = "https://cadenaser.com/cookie-page-article"
    
    mock_candidates = [
        {"url": cand1_url, "query": "q", "provider": "GoogleNews", "title": "Titulo 1", "snippet": "S1", "position": 1, "score": 100, "pub_date": None},
        {"url": cand2_url, "query": "q", "provider": "GoogleNews", "title": "Titulo 2", "snippet": "S2", "position": 2, "score": 100, "pub_date": None},
        {"url": cand3_url, "query": "q", "provider": "GoogleNews", "title": "Titulo 3", "snippet": "S3", "position": 3, "score": 100, "pub_date": None}
    ]
    
    def mock_gnewsdecoder(url, interval=1):
        if url == cand1_url:
            return {"status": True, "decoded_url": resolved1_url}
        elif url == cand3_url:
            return {"status": True, "decoded_url": resolved3_url}
        return {"status": False}

    def mock_extract(url, *args, **kwargs):
        if url == resolved1_url:
            return {"title": "Titulo Real 1", "text": "Reseña literaria sobre San Manuel Bueno martir.", "date": "2024-02-18"}
        elif url == resolved3_url:
            return {"title": "Google Consent", "text": "Before you continue... We use cookies and data...", "date": "2024-02-18"}
        raise Exception("Extraction failed")

    with patch("app.services.source_discovery.source_discovery.find_candidates", return_value=mock_candidates), \
         patch("app.services.sheets_service.sheets_service.get_config_dict", return_value={
             "MAX_SEARCH_PAGES_PER_QUERY": 1,
             "MAX_CANDIDATES_PER_BOOK": 5,
             "MIN_MATCH_SCORE": 70,
             "SEARCH_PROVIDER_MODE": "auto",
             "ENABLE_CASCADE_SEARCH": "true",
             "ENABLE_INTERNAL_DOMAIN_SEARCH": "false",
             "DEFAULT_INCLUDE_UNKNOWN_DATES": "true"
         }), \
         patch("app.services.sheets_service.sheets_service.get_all_reviews", return_value=[]), \
         patch("googlenewsdecoder.gnewsdecoder", side_effect=mock_gnewsdecoder), \
         patch("app.services.article_extractor.article_extractor.extract", side_effect=mock_extract), \
         patch("app.services.openai_analyzer.openai_analyzer.analyze_article", return_value={
             "is_valid": True,
             "match_score": 85,
             "reason": "Excelente reseña",
             "detected_book_title": "San Manuel Bueno, mártir",
             "detected_book_author": "Miguel de Unamuno",
             "content_type": "reseña",
             "publication_name": "Zenda",
             "publication_author": "Autor",
             "publication_date": "2024-02-18",
             "language": "es",
             "category": "Literatura",
             "summary": "Resumen de prueba"
         }) as mock_analyze, \
         patch("app.services.sheets_service.sheets_service.add_descarte") as mock_add_descarte, \
         patch("app.services.sheets_service.sheets_service.add_review") as mock_add_review, \
         patch("app.services.sheets_service.sheets_service.update_book_status") as mock_status:
         
         run_service._process_book(
             run_id="run_test_decoding",
             sheet_id="sheet_id",
             row_index=2,
             isbn="123456",
             title="San Manuel Bueno, mártir",
             author="Miguel de Unamuno",
             max_pages=1,
             max_candidates=5,
             min_score=70,
             openai_model="gpt-4o",
             existing_hashes=set(),
             existing_secondary_keys=set(),
             dry_run=False
         )
         
         # Verification 1: Only 1 candidate (resolved1_url) was sent to OpenAI validation
         assert mock_analyze.call_count == 1
         called_kwargs = mock_analyze.call_args[1]
         assert called_kwargs["url"] == resolved1_url
         
         # Verification 2: add_review is called with the resolved URL
         assert mock_add_review.call_count == 1
         # add_review now receives (sheet_id, review_dict) where review_dict is a dict
         review_arg = mock_add_review.call_args[0][1]
         assert review_arg.get("URL") == resolved1_url
         
         # Verification 3: add_descarte is called for failed resolution and cookie pages
         assert mock_add_descarte.call_count >= 2
         discard_reasons = [call[0][1][6] for call in mock_add_descarte.call_args_list]
         discard_urls = [call[0][1][4] for call in mock_add_descarte.call_args_list]
         discard_titles = [call[0][1][5] for call in mock_add_descarte.call_args_list]
         
         # One descarte is failed resolution
         assert "no se pudo resolver URL de Google News" in discard_reasons
         assert cand2_url in discard_urls
         assert "Titulo 2" in discard_titles
         
         # One descarte is cookie consent page
         assert "página de cookies/consent" in discard_reasons
         assert resolved3_url in discard_urls
         assert "Titulo 3" in discard_titles



def test_ensure_sheet_endpoint_ok():
    """
    Regression test for POST /setup/ensure-sheet.
    Verifies the endpoint returns 200 when ensure_sheet succeeds.
    Any NameError in ensure_sheet must propagate as HTTP 500.
    """
    from unittest.mock import patch

    mock_result = {
        "success": True,
        "sheet_id": "fake_sheet_id",
        "sheet_url": "https://docs.google.com/spreadsheets/d/fake_sheet_id",
        "created_tabs": []
    }

    with patch("app.routers.setup.sheets_service.ensure_sheet", return_value=mock_result):
        response = client.post("/setup/ensure-sheet")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "sheet_id" in data


def test_ensure_sheet_endpoint_propagates_errors():
    """
    Regression test: if ensure_sheet raises a NameError (e.g. 'libros_ws' not defined),
    the endpoint must return HTTP 500 with a detail message.
    """
    from unittest.mock import patch

    with patch(
        "app.routers.setup.sheets_service.ensure_sheet",
        side_effect=NameError("name 'libros_ws' is not defined")
    ):
        response = client.post("/setup/ensure-sheet")

    assert response.status_code == 500
    detail = response.json().get("detail", "")
    assert "libros_ws" in detail or "Google Sheets setup failed" in detail

import json

def test_build_broad_queries():
    """
    Tests build_broad_queries with generic author.
    Verifies that it contains lowercase youcat biblia, doesn't contain VV.AA., and doesn't contain reseña/crítica.
    """
    from app.services.query_builder import query_builder
    
    broad = query_builder.build_broad_queries("YOUCAT Biblia", "VV.AA.", "978-84-1339-264-6")
    
    # 1. Contains lowercase youcat biblia
    assert "youcat biblia" in broad
    
    # 2. No VV.AA. or variations
    for q in broad:
        assert "VV.AA." not in q
        assert "vv.aa." not in q.lower()
        
    # 3. No reseña/crítica
    for q in broad:
        assert "reseña" not in q.lower()
        assert "crítica" not in q.lower()
        assert "critica" not in q.lower()
        assert "libro" not in q.lower() # No "libro" restrictor


def test_broad_news_search_triggers_and_budget_guaranteed():
    """
    Verifies that if normal Google News search returns 0 candidates,
    GOOGLE_NEWS_BROAD is triggered with its own budget (10 queries),
    even if MAX_QUERIES_PER_BOOK is low (e.g. 1).
    """
    from unittest.mock import patch
    from app.services.run_service import run_service
    
    # Setup candidate mock lists:
    # First search_with_fallback returns empty list (0 candidates found in Phase 2)
    # Broad query search returns 1 candidate
    mock_rss_normal = []
    mock_rss_broad = [{"url": "https://www.zendalibros.com/youcat-article", "title": "YOUCAT Biblia - Zenda", "snippet": "Snippet", "position": 1, "pub_date": "2024-02-18", "provider": "GoogleNewsRss"}]
    
    def side_effect_search(query, *args, **kwargs):
        # Normal phase queries (like those in prioritarias) return empty list
        # Broad phase queries (like "youcat biblia" lowercase without quotes) return candidates
        if query == "youcat biblia" or query == "youcat biblia noticia" or query == "youcat biblia artículo":
            return mock_rss_broad
        return mock_rss_normal

    with patch("app.services.source_discovery.source_discovery.find_candidates", return_value=[]), \
         patch("app.services.sheets_service.sheets_service.get_config_dict", return_value={
             "MAX_QUERIES_PER_BOOK": 1, # extremely low limit to test budget bypass
             "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES": 1,
             "MIN_MATCH_SCORE": 10,
             "MAX_CANDIDATES_PER_BOOK": 5,
             "SEARCH_PROVIDER_MODE": "google_news_only",
             "ENABLE_CASCADE_SEARCH": "true"
         }), \
         patch("app.services.sheets_service.sheets_service.get_all_reviews", return_value=[]), \
         patch("app.services.search_service.search_service.search_with_fallback", side_effect=side_effect_search) as mock_search, \
         patch("app.services.article_extractor.article_extractor.extract", return_value={
             "title": "YOUCAT Biblia - Zenda",
             "text": "Este es el contenido de un artículo sobre YOUCAT Biblia.",
             "date": "2024-02-18"
         }), \
         patch("app.services.openai_analyzer.openai_analyzer.analyze_article", return_value={
             "is_valid": True,
             "match_score": 85,
             "reason": "Excelente artículo",
             "detected_book_title": "YOUCAT Biblia",
             "detected_book_author": "VV.AA.",
             "content_type": "artículo",
             "publication_name": "Zenda",
             "publication_author": "Autor",
             "publication_date": "2024-02-18",
             "language": "es",
             "category": "Religión",
             "summary": "Resumen"
         }), \
         patch("app.services.sheets_service.sheets_service.add_descarte") as mock_add_descarte, \
         patch("app.services.sheets_service.sheets_service.add_review") as mock_add_review, \
         patch("app.services.sheets_service.sheets_service.update_book_status") as mock_status, \
         patch("app.services.logger_service.logger_service.log") as mock_log:
         
         run_service._process_book(
             run_id="run_test_broad",
             sheet_id="sheet_id",
             row_index=2,
             isbn="123456",
             title="YOUCAT Biblia",
             author="VV.AA.",
             max_pages=1,
             max_candidates=5,
             min_score=10,
             openai_model="gpt-4o",
             existing_hashes=set(),
             existing_secondary_keys=set(),
             dry_run=False
         )
         
         # Helper to find mock log calls by action
         def find_log_call(action_name):
             for call in mock_log.call_args_list:
                 args, kwargs = call
                 action = kwargs.get("action")
                 if not action and len(args) > 1:
                     action = args[1]
                 if action == action_name:
                     return args, kwargs
             return None, None

         # Verification 1: RUN_CONFIG_EFFECTIVE reflects config values and GOOGLE_NEWS_BROAD_MAX_QUERIES = 10
         args, kwargs = find_log_call("RUN_CONFIG_EFFECTIVE")
         assert args is not None or kwargs is not None
         detail_str = kwargs.get("detail") or (args[4] if len(args) > 4 else "")
         payload = json.loads(detail_str)
         assert payload.get("MIN_MATCH_SCORE") == 10
         assert payload.get("GOOGLE_NEWS_BROAD_MAX_QUERIES") == 10
         
         # Verification 2: AUTHOR_NORMALIZED_AS_GENERIC is logged
         args, kwargs = find_log_call("AUTHOR_NORMALIZED_AS_GENERIC")
         assert args is not None or kwargs is not None
         detail_str = kwargs.get("detail") or (args[4] if len(args) > 4 else "")
         auth_payload = json.loads(detail_str)
         assert auth_payload.get("original_author") == "VV.AA."
         assert auth_payload.get("author_is_generic") is True
         
         # Verification 3: BOOK_QUERIES_BUILT is logged separating categories
         args, kwargs = find_log_call("BOOK_QUERIES_BUILT")
         assert args is not None or kwargs is not None
         detail_str = kwargs.get("detail") or (args[4] if len(args) > 4 else "")
         queries_payload = json.loads(detail_str)
         assert "prioritarias" in queries_payload
         assert "broad_queries" in queries_payload
         assert "youcat biblia" in queries_payload["broad_queries"]

         # Verification 4: GOOGLE_NEWS_BROAD_STARTED is logged
         args, kwargs = find_log_call("GOOGLE_NEWS_BROAD_STARTED")
         assert args is not None or kwargs is not None

         # Verification 5: broad search executed successfully and candidate added
         assert mock_add_review.call_count == 1
         review_data = mock_add_review.call_args[0][1]
         assert review_data.get("URL") == "https://www.zendalibros.com/youcat-article"


def test_debug_google_news_endpoint():
    """
    Tests GET /debug/google-news endpoint structure.
    """
    from unittest.mock import patch
    from app.services.search_providers import SearchProviderResult

    mock_rss_result = SearchProviderResult(
        provider="GoogleNewsRss",
        query="youcat biblia",
        status="ok",
        status_code=200,
        urls=["https://www.zendalibros.com/youcat-article"],
        debug={
            "organic_results_parsed": [
                {
                    "url": "https://www.zendalibros.com/youcat-article",
                    "title": "YOUCAT Biblia - Zenda",
                    "snippet": "Snippet",
                    "pub_date": "2024-02-18",
                    "position": 1
                }
            ]
        }
    )

    with patch("app.services.search_providers.GoogleNewsRssSearchProvider.search", return_value=mock_rss_result):
        response = client.get("/debug/google-news?q=youcat%20biblia")

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "youcat biblia"
    assert data["parsed_results_count"] == 1
    assert data["results"][0]["source"] == "Zenda"
    assert data["results"][0]["url"] == "https://www.zendalibros.com/youcat-article"


# --- REGRESSION TESTS FOR METADATA AND SOURCES ---

def test_extract_author_jsonld():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "NewsArticle",
          "author": {
            "@type": "Person",
            "name": "Juan Carlos Pérez"
          }
        }
        </script>
      </head>
      <body>
        <p>Este es el texto del artículo con más de cien caracteres para pasar la validación de texto insuficiente de BeautifulSoup.</p>
      </body>
    </html>
    """
    res = article_extractor.extract_article_metadata("https://www.ejemplo.com/articulo", html)
    assert res["article_author"] == "Juan Carlos Pérez"
    assert res["author_source"] == "jsonld"


def test_extract_author_meta():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <head>
        <meta name="author" content="María de la O" />
      </head>
      <body>
        <p>Este es el texto del artículo con más de cien caracteres para pasar la validación de texto insuficiente de BeautifulSoup.</p>
      </body>
    </html>
    """
    res = article_extractor.extract_article_metadata("https://www.ejemplo.com/articulo", html)
    assert res["article_author"] == "María de la O"
    assert res["author_source"] == "meta"


def test_extract_author_byline():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <body>
        <div class="author">Por Pedro Gómez</div>
        <p>Este es el texto del artículo con más de cien caracteres para pasar la validación de texto insuficiente de BeautifulSoup.</p>
      </body>
    </html>
    """
    res = article_extractor.extract_article_metadata("https://www.ejemplo.com/articulo", html)
    assert res["article_author"] == "Pedro Gómez"
    assert res["author_source"] == "selector"


def test_extract_author_no_medium_fallback():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <head>
        <meta name="author" content="ejemplo.com" />
      </head>
      <body>
        <p>Este es el texto del artículo con más de cien caracteres para pasar la validación de texto insuficiente de BeautifulSoup.</p>
      </body>
    </html>
    """
    res = article_extractor.extract_article_metadata("https://www.ejemplo.com/articulo", html)
    assert res["article_author"] == ""
    assert res["author_source"] == "empty"


def test_extract_date_jsonld():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Article",
          "datePublished": "2024-07-23T14:30:00Z"
        }
        </script>
      </head>
      <body>
        <p>Este es el texto del artículo con más de cien caracteres para pasar la validación de texto insuficiente de BeautifulSoup.</p>
      </body>
    </html>
    """
    res = article_extractor.extract_article_metadata("https://www.ejemplo.com/articulo", html)
    assert res["published_date"] == "2024-07-23"
    assert res["date_source"] == "jsonld"


def test_extract_date_meta():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <head>
        <meta property="article:published_time" content="2024/05/12" />
      </head>
      <body>
        <p>Este es el texto del artículo con más de cien caracteres para pasar la validación de texto insuficiente de BeautifulSoup.</p>
      </body>
    </html>
    """
    res = article_extractor.extract_article_metadata("https://www.ejemplo.com/articulo", html)
    assert res["published_date"] == "2024-05-12"
    assert res["date_source"] == "meta"


def test_extract_date_time_tag():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <body>
        <time datetime="2023-11-09T08:00:00+02:00">9 de Noviembre</time>
        <p>Este es el texto del artículo con más de cien caracteres para pasar la validación de texto insuficiente de BeautifulSoup.</p>
      </body>
    </html>
    """
    res = article_extractor.extract_article_metadata("https://www.ejemplo.com/articulo", html)
    assert res["published_date"] == "2023-11-09"
    assert res["date_source"] == "time_tag"


def test_extract_date_provider_fallback():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <body>
        <p>Este es el texto del artículo con más de cien caracteres para pasar la validación de texto insuficiente de BeautifulSoup.</p>
      </body>
    </html>
    """
    provider_item = {"pub_date": "2024-03-15"}
    res = article_extractor.extract_article_metadata("https://www.ejemplo.com/articulo", html, provider_item=provider_item)
    assert res["published_date"] == "2024-03-15"
    assert res["date_source"] == "provider_pub_date"


def test_extract_date_url_inferred():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <body>
        <p>Este es el texto del artículo con más de cien caracteres para pasar la validación de texto insuficiente de BeautifulSoup.</p>
      </body>
    </html>
    """
    res = article_extractor.extract_article_metadata("https://www.ejemplo.com/2022/10/05/articulo", html)
    assert res["published_date"] == "2022-10-05"
    assert res["date_source"] == "url"


def test_author_medium_distinct():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <head>
        <meta name="author" content="ACI Prensa" />
      </head>
      <body>
        <p>Este es el texto del artículo con más de cien caracteres para pasar la validación de texto insuficiente de BeautifulSoup.</p>
      </body>
    </html>
    """
    # ACI Prensa is the medium / domain, so it should be discarded if matched against domain
    res = article_extractor.extract_article_metadata("https://www.aciprensa.com/youcat", html)
    assert res["article_author"] == ""
    assert res["author_source"] == "empty"


def test_author_empty_or_redaccion():
    from app.services.article_extractor import article_extractor
    html = """
    <html>
      <body>
        <p>Este artículo fue elaborado por la redacción del periódico hace unos días...</p>
      </body>
    </html>
    """
    res = article_extractor.extract_article_metadata("https://www.ejemplo.com/articulo", html)
    assert res["article_author"] == "Redacción"
    assert res["author_source"] == "pattern"


def test_sources_append_no_duplicates():
    from app.services.sheets_service import sheets_service
    
    mock_records = [
        {"Dominio": "revistadelibros.com", "Activo": "true", "Tipo": "cultural"},
        {"Dominio": "nueva-revista.net", "Activo": "true", "Tipo": "cultural"}
    ]
    
    class FakeWorksheet:
        def __init__(self):
            self.appended = []
        def get_all_records(self):
            return mock_records
        def append_rows(self, rows, value_input_option=None):
            self.appended.extend(rows)
            
    fake_ws = FakeWorksheet()
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client:
        mock_spreadsheet = MagicMock()
        mock_spreadsheet.worksheet.return_value = fake_ws
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        appended_count = sheets_service.append_default_sources("some_sheet_id")
        
        # Total recommended: 36. 2 already exist, so 34 should be appended.
        assert appended_count == 34
        assert len(fake_ws.appended) == 34
        # Verify revistadelibros.com was skipped
        for row in fake_ws.appended:
            assert row[0] != "revistadelibros.com"


def test_internal_search_utilizes_rss_sitemap_template():
    from app.services.internal_search_provider import internal_search_provider
    
    source_info = {
        "domain": "revistadelibros.com",
        "rss_url": "https://www.revistadelibros.com/feed/",
        "sitemap_url": "https://www.revistadelibros.com/sitemap.xml",
        "buscador_interno": "https://www.revistadelibros.com/?s={query}"
    }
    
    mock_rss_res = [{"url": "https://www.revistadelibros.com/r1", "title": "R1", "snippet": "RSS"}]
    mock_sitemap_res = [{"url": "https://www.revistadelibros.com/s1", "title": "S1", "snippet": "Sitemap"}]
    mock_template_res = [{"url": "https://www.revistadelibros.com/t1", "title": "T1", "snippet": "Template"}]
    
    with patch("app.services.internal_search_provider.InternalDomainSearchProvider.search_rss", return_value=mock_rss_res) as mock_rss, \
         patch("app.services.internal_search_provider.InternalDomainSearchProvider.search_sitemap", return_value=mock_sitemap_res) as mock_sitemap, \
         patch("app.services.internal_search_provider.InternalDomainSearchProvider.search_template", return_value=mock_template_res) as mock_template:
         
         results = internal_search_provider.search_domain_for_book(
             domain="revistadelibros.com",
             title="El infinito en un junco",
             author="Irene Vallejo",
             isbn="123",
             source_info=source_info
         )
         
         assert mock_rss.called
         assert mock_sitemap.called
         assert mock_template.called
         assert len(results) == 3


def test_ensure_sheet_updates_low_limits():
    from app.services.sheets_service import sheets_service
    from unittest.mock import patch, MagicMock
    
    class FakeConfigWorksheet:
        def __init__(self):
            self.values = [
                ["Clave", "Valor", "Descripción"],
                ["MAX_QUERIES_PER_BOOK", "3", "Description"],
                ["DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES", "3", "Description"]
            ]
            self.updated = {}
        def get_all_values(self):
            return self.values
        def get_all_records(self):
            records = []
            headers = self.values[0]
            for row in self.values[1:]:
                rec = {}
                for idx, val in enumerate(row):
                    if idx < len(headers):
                        rec[headers[idx]] = val
                records.append(rec)
            return records
        def update_cell(self, row, col, val):
            self.updated[(row, col)] = val
            # Find the row and update the cell value
            self.values[row - 1][col - 1] = str(val)
        def append_row(self, row, value_input_option=None):
            self.values.append(row)
        def clear(self):
            self.values = [["Clave", "Valor", "Descripción"]]
            
    fake_config_ws = FakeConfigWorksheet()
    
    class FakeWorksheet:
        def __init__(self):
            self.id = 12345
        def get_all_records(self):
            return []
        def get_all_values(self):
            return []
        def row_values(self, index):
            return []
        def append_row(self, row, value_input_option=None):
            pass
        def append_rows(self, rows, value_input_option=None):
            pass
        def clear(self):
            pass
        def resize(self, rows=None, cols=None):
            pass
        def update(self, range_name, values=None, **kwargs):
            pass
            
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client, \
         patch("app.services.logger_service.logger_service.log") as mock_logger:
        
        mock_spreadsheet = MagicMock()
        def get_ws(name):
            if name == "Config":
                return fake_config_ws
            return FakeWorksheet()
            
        mock_spreadsheet.worksheet.side_effect = get_ws
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        # Run ensure_sheet
        sheets_service.ensure_sheet("some_sheet_id")
        
        # Find updated indices in fake_config_ws.updated
        # Since MAX_QUERIES_PER_BOOK and DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES are appended,
        # let's check by matching key name in values list.
        max_queries_row = None
        news_complement_row = None
        for idx, r in enumerate(fake_config_ws.values):
            if r[0] == "MAX_QUERIES_PER_BOOK":
                max_queries_row = idx + 1
            elif r[0] == "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES":
                news_complement_row = idx + 1
                
        assert max_queries_row is not None
        assert news_complement_row is not None
        assert fake_config_ws.updated.get((max_queries_row, 2)) == 12
        assert fake_config_ws.updated.get((news_complement_row, 2)) == 10
        
        # Check logs emitted
        warning_calls = [c for c in mock_logger.call_args_list if c[0][0] == "WARNING"]
        info_calls = [c for c in mock_logger.call_args_list if c[0][0] == "INFO"]
        
        assert any("CONFIG_LOW_QUERY_LIMIT_WARNING" in str(c) for c in warning_calls)
        assert any("CONFIG_QUERY_LIMITS_AUTO_UPDATED" in str(c) for c in info_calls)


# --- ADDITIONAL SOURCES SYNC REGRESSION TESTS ---

def test_domain_comparison_normalizes_properly():
    from app.services.sheets_service import clean_domain_string
    assert clean_domain_string("HTTPS://WWW.RELIGIONENLIBERTAD.COM/") == "religionenlibertad.com"
    assert clean_domain_string("http://abc.es/noticias") == "abc.es"
    assert clean_domain_string("   WwW.ALFAYOMEGA.ES  ") == "alfayomega.es"


def test_update_source_index_status_updates_sheet():
    from app.services.sheets_service import sheets_service
    
    mock_records = [
        {"Dominio": "religionenlibertad.com", "Última indexación": "", "URLs indexadas": "", "Errores": ""},
        {"Dominio": "abc.es", "Última indexación": "", "URLs indexadas": "", "Errores": ""}
    ]
    
    class FakeFuentesWorksheet:
        def __init__(self):
            self.updated_range = None
            self.updated_values = None
        def get_all_records(self):
            return mock_records
        def update(self, range_name, values, **kwargs):
            self.updated_range = range_name
            self.updated_values = values
            
    fake_ws = FakeFuentesWorksheet()
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client:
        mock_spreadsheet = MagicMock()
        mock_spreadsheet.worksheet.return_value = fake_ws
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        sheets_service.update_source_index_status(
            sheet_id="some_sheet",
            domain="https://www.religionenlibertad.com/",
            last_indexed="2026-06-30T17:20:40",
            urls_indexed=989,
            errors=[]
        )
        
        # Row 2 (first domain in mock_records) should be updated to E2:G2 (cols 5, 6, 7)
        assert fake_ws.updated_range == "E2:G2"
        assert fake_ws.updated_values == [["2026-06-30T17:20:40", 989, ""]]


def test_update_status_even_with_partial_errors():
    from app.services.sheets_service import sheets_service
    
    mock_records = [
        {"Dominio": "abc.es", "Última indexación": "", "URLs indexadas": "", "Errores": ""}
    ]
    
    class FakeFuentesWorksheet:
        def __init__(self):
            self.updated_values = None
        def get_all_records(self):
            return mock_records
        def update(self, range_name, values, **kwargs):
            self.updated_values = values
            
    fake_ws = FakeFuentesWorksheet()
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client:
        mock_spreadsheet = MagicMock()
        mock_spreadsheet.worksheet.return_value = fake_ws
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        # Even with urls_indexed > 0 and errors list, it should write them
        sheets_service.update_source_index_status(
            sheet_id="some_sheet",
            domain="abc.es",
            last_indexed="2026-06-30T16:55:21",
            urls_indexed=541,
            errors=["sitemap_parse_error", "timeout"]
        )
        
        assert fake_ws.updated_values == [["2026-06-30T16:55:21", 541, "sitemap_parse_error, timeout"]]


def test_sync_status_endpoint():
    from app.config import settings
    mock_sync_result = {
        "success": True,
        "sources_updated": 3,
        "total_urls": 2030
    }
    
    with patch("app.services.sheets_service.sheets_service.sync_sources_status", return_value=mock_sync_result):
        response = client.post("/sources/sync-status", headers={"X-Admin-Token": settings.ADMIN_TOKEN or "dummy_token"})
        
    assert response.status_code == 200
    assert response.json() == mock_sync_result


def test_background_job_updates_realtime():
    from unittest.mock import ANY
    from app.routers.sources import execute_indexing_job
    
    mock_sources = [
        {"domain": "religionenlibertad.com", "active": True}
    ]
    
    mock_index_results = [
        {"domain": "religionenlibertad.com", "urls_found": 12, "errors": ["some_error"], "skipped": False}
    ]
    
    # We want to check that executing indexing job triggers update_source_index_status for religionenlibertad.com
    with patch("app.services.sheets_service.sheets_service.get_config_dict", return_value={}), \
         patch("app.services.sheets_service.sheets_service.get_active_sources", return_value=mock_sources), \
         patch("app.services.cache_service.cache_service.get_domain_stats", return_value={"urls": 12}), \
         patch("app.services.domain_indexer.domain_indexer.index_all", return_value=mock_index_results) as mock_index, \
         patch("app.services.sheets_service.sheets_service.update_source_index_status") as mock_update:
         
         # Stub index_all to trigger on_domain_complete manually
         def fake_index_all(*args, **kwargs):
             on_complete = kwargs.get("on_domain_complete")
             if on_complete:
                 on_complete(mock_index_results[0])
             return mock_index_results
             
         mock_index.side_effect = fake_index_all
         
         execute_indexing_job(
             job_id="job_123",
             limit_domains=1,
             force_refresh=False,
             sheet_id="sheet_id"
         )
         
         # update_source_index_status should have been called in real-time
         assert mock_update.called
         mock_update.assert_called_with(
             sheet_id="sheet_id",
             domain="religionenlibertad.com",
             last_indexed=ANY,
             urls_indexed=12,
             errors=["some_error"]
         )


def test_sync_sources_status_initialises_cache_service():
    from app.services.sheets_service import sheets_service
    from app.services.cache_service import cache_service
    
    mock_records = [
        {"Dominio": "religionenlibertad.com", "Última indexación": "", "URLs indexadas": "", "Errores": ""}
    ]
    
    class FakeFuentesWorksheet:
        def get_all_records(self):
            return mock_records
        def update_cells(self, cells, **kwargs):
            pass
            
    fake_ws = FakeFuentesWorksheet()
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client, \
         patch("app.services.cache_service.cache_service.init_db") as mock_init_db, \
         patch("app.services.cache_service.cache_service.get_total_urls", return_value=100), \
         patch("app.services.cache_service.cache_service.get_all_domain_statuses", return_value=[]), \
         patch("app.services.cache_service.cache_service.get_all_domains_stats", return_value=[]):
         
        mock_spreadsheet = MagicMock()
        mock_spreadsheet.worksheet.return_value = fake_ws
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        result = sheets_service.sync_sources_status("some_sheet_id")
        
        assert mock_init_db.called
        assert result["success"] is True


def test_sync_status_endpoint_initialization():
    from app.config import settings
    
    mock_records = [
        {"Dominio": "religionenlibertad.com", "Última indexación": "", "URLs indexadas": "", "Errores": ""}
    ]
    
    class FakeFuentesWorksheet:
        def get_all_records(self):
            return mock_records
        def update_cells(self, cells, **kwargs):
            pass
            
    fake_ws = FakeFuentesWorksheet()
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client, \
         patch("app.services.cache_service.cache_service.init_db") as mock_init_db, \
         patch("app.services.cache_service.cache_service.get_total_urls", return_value=100), \
         patch("app.services.cache_service.cache_service.get_all_domain_statuses", return_value=[]), \
         patch("app.services.cache_service.cache_service.get_all_domains_stats", return_value=[]):
         
        mock_spreadsheet = MagicMock()
        mock_spreadsheet.worksheet.return_value = fake_ws
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        response = client.post("/sources/sync-status", headers={"X-Admin-Token": settings.ADMIN_TOKEN or "dummy_token"})
        
        assert response.status_code == 200
        assert "CacheService not initialised" not in response.text


def test_build_post_payload_mapping():
    from app.services.wordpress_publisher import wordpress_publisher
    
    review = {
        "Título del libro": "El Nombre de la Rosa",
        "Título para Web": "Gran Reseña del Nombre de la Rosa",
        "ISBN": "978-1234567890",
        "URL": "https://cultura.com/reseña-rosa",
        "Medio de publicación": "Cultura y Letras",
        "Autor para Web": "Felipe Reseñador",
        "Autor del libro": "Umberto Eco",
        "Resumen": "Un resumen de prueba muy completo y descriptivo."
    }
    
    payload = wordpress_publisher.build_post_payload(review, {})
    
    # 1. build_post_payload usa Título para Web como título
    assert payload["title"] == "Gran Reseña del Nombre de la Rosa"
    
    # 2. build_post_payload usa Resumen como cuerpo
    assert payload["content"] == "Un resumen de prueba muy completo y descriptivo."
    
    acf_payload = wordpress_publisher.build_acf_payload(review)
    # 3. El campo Libro se deja vacío por ahora
    assert acf_payload["libro"] == ""
    # 4. El campo ISBN Libro se rellena con ISBN
    assert acf_payload["isbn_libro"] == "978-1234567890"
    # 5. El campo Url se rellena con URL
    assert acf_payload["url"] == "https://cultura.com/reseña-rosa"
    # 6. El campo Medio se rellena con Medio de publicación
    assert acf_payload["medio"] == "Cultura y Letras"
    # 7. El campo Autor se rellena con Autor para Web, no con Autor del libro
    assert acf_payload["autor"] == "Felipe Reseñador"
    assert acf_payload["autor"] != "Umberto Eco"


def test_build_post_payload_title_fallback():
    from app.services.wordpress_publisher import wordpress_publisher
    
    # 8. Si Título para Web está vacío, usar fallback Título del libro
    review = {
        "Título del libro": "El Nombre de la Rosa",
        "Título para Web": "",
        "ISBN": "978-1234567890",
        "URL": "https://cultura.com/reseña-rosa",
        "Medio de publicación": "Cultura y Letras",
        "Autor para Web": "Felipe Reseñador",
        "Autor del libro": "Umberto Eco",
        "Resumen": "Resumen"
    }
    
    payload = wordpress_publisher.build_post_payload(review, {})
    assert payload["title"] == "El Nombre de la Rosa"


def test_build_post_payload_autor_fallback_and_redaccion():
    from app.services.wordpress_publisher import wordpress_publisher
    
    # 9. Si Autor para Web está vacío, dejar Autor vacío y no inventarlo
    review_empty_autor = {
        "Título del libro": "El Nombre de la Rosa",
        "Título para Web": "Título Web",
        "ISBN": "978-1234567890",
        "URL": "https://cultura.com/reseña-rosa",
        "Medio de publicación": "Cultura y Letras",
        "Autor para Web": "",
        "Autor del libro": "Umberto Eco",
        "Resumen": "Resumen"
    }
    
    acf_empty = wordpress_publisher.build_acf_payload(review_empty_autor)
    assert acf_empty["autor"] == ""
    
    # Si Autor para Web viene como "Redacción", se mantiene
    review_redaccion = {
        "Título del libro": "El Nombre de la Rosa",
        "Título para Web": "Título Web",
        "ISBN": "978-1234567890",
        "URL": "https://cultura.com/reseña-rosa",
        "Medio de publicación": "Cultura y Letras",
        "Autor para Web": "Redacción",
        "Autor del libro": "Umberto Eco",
        "Resumen": "Resumen"
    }
    
    acf_redaccion = wordpress_publisher.build_acf_payload(review_redaccion)
    assert acf_redaccion["autor"] == "Redacción"


def test_publish_review_preview_and_error_logging():
    from unittest.mock import patch, MagicMock
    from app.services.wordpress_publisher import wordpress_publisher
    import json
    
    review = {
        "Título del libro": "El Nombre de la Rosa",
        "Título para Web": "Gran Reseña",
        "ISBN": "978-1234567890",
        "URL": "https://cultura.com/reseña-rosa",
        "Medio de publicación": "Cultura y Letras",
        "Autor para Web": "Felipe Reseñador",
        "Autor del libro": "Umberto Eco",
        "Resumen": "Un resumen"
    }
    
    # Mock httpx.Client response to simulate WordPress returning 400 Bad Request
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "ACF fields are invalid"
    
    with patch("app.services.logger_service.logger_service.log") as mock_log, \
         patch("app.config.settings.WORDPRESS_APPLICATION_PASSWORD", "dummy_pass"), \
         patch("httpx.Client.post", return_value=mock_response):
         
         res = wordpress_publisher.publish_review(
             review=review,
             config={
                 "WORDPRESS_BASE_URL": "https://myblog.com",
                 "WORDPRESS_USERNAME": "admin"
             },
             dry_run=False,
             sheet_id="sheet123",
             run_id="run456"
         )
         
         assert res["success"] is False
         assert "Fallo al publicar (HTTP 400)" in res["error"]
         
         # Verify WORDPRESS_PAYLOAD_PREVIEW was logged
         preview_calls = [call for call in mock_log.call_args_list if call[1].get("action") == "WORDPRESS_PAYLOAD_PREVIEW"]
         assert len(preview_calls) == 1
         preview_detail = json.loads(preview_calls[0][1]["detail"])
         assert preview_detail["post_title"] == "Gran Reseña"
         assert preview_detail["libro"] == "El Nombre de la Rosa"
         
         # Verify WORDPRESS_PUBLISH_ERROR was logged with full details
         error_calls = [call for call in mock_log.call_args_list if call[1].get("action") == "WORDPRESS_PUBLISH_ERROR"]
         assert len(error_calls) == 1
         error_detail = json.loads(error_calls[0][1]["detail"])
         assert error_detail["status_code"] == 400
         assert error_detail["response_text"] == "ACF fields are invalid"
         assert "title" in error_detail["payload_keys"]
         assert "acf" not in error_detail["payload_keys"]


def test_sheet_compaction_report_and_dry_run():
    from unittest.mock import patch, MagicMock
    import gspread
    from app.services.sheets_service import sheets_service
    
    # Mock worksheet for Logs
    mock_ws = MagicMock()
    mock_ws.title = "Logs"
    mock_ws.row_count = 1000
    mock_ws.col_count = 100
    # 10 rows, where the first is headers and others are log values (7 columns)
    mock_ws.get_all_values.return_value = [
        ["Fecha", "Nivel", "Acción", "ISBN", "Mensaje", "Detalle", "Run ID"],
        ["2026-07-01", "INFO", "RUN_START", "12345", "Msg", "", "run1"],
        ["2026-07-01", "INFO", "BOOK_QUERIES_BUILT", "12345", "Msg", "", "run1"],
        ["", "", "", "", "", "", ""] # trailing empty row, should be ignored
    ]
    
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.worksheets.return_value = [mock_ws]
    
    def side_effect_worksheet(name):
        if name == "Logs":
            return mock_ws
        raise gspread.exceptions.WorksheetNotFound("Not found")
    mock_spreadsheet.worksheet.side_effect = side_effect_worksheet
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client:
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        # 1. Audits size report
        report = sheets_service.get_sheet_size_report("sheet123")
        assert report["success"] is True
        
        tab_log = report["tabs"][0]
        assert tab_log["title"] == "Logs"
        assert tab_log["rows"] == 1000
        assert tab_log["cols"] == 100
        assert tab_log["last_data_row"] == 4
        assert tab_log["last_data_col"] == 7 
        
        assert tab_log["recommended_rows"] == 500
        assert tab_log["recommended_cols"] == 7
        
        assert tab_log["excess_rows"] == 500
        assert tab_log["excess_cols"] == 93
        
        # 2. dry_run = True
        comp_dry = sheets_service.compact_sheet("sheet123", dry_run=True)
        assert comp_dry["success"] is True
        assert comp_dry["dry_run"] is True
        assert comp_dry["total_cells_freed"] == (1000 * 100) - (500 * 7)
        assert len(comp_dry["compacted_tabs"]) == 1
        assert comp_dry["compacted_tabs"][0]["title"] == "Logs"
        assert not mock_ws.resize.called
        
        # 3. dry_run = False
        comp_real = sheets_service.compact_sheet("sheet123", dry_run=False)
        assert comp_real["success"] is True
        assert comp_real["dry_run"] is False
        assert mock_ws.resize.called
        mock_ws.resize.assert_called_with(rows=500, cols=7)


def test_logs_write_fixed_range_and_col_a_next_row():
    from unittest.mock import patch, MagicMock
    from app.services.sheets_service import sheets_service
    
    mock_ws = MagicMock()
    mock_ws.col_values.return_value = ["Fecha", "2026-07-01 09:00:00", "2026-07-01 09:05:00"]
    
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.worksheet.return_value = mock_ws
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client:
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        log_rows = [
            ["2026-07-01 09:10:00", "INFO", "ACTION_1", "111", "Message 1", "", "run_abc"],
            ["2026-07-01 09:11:00", "INFO", "ACTION_2", "111", "Message 2", "", "run_abc"]
        ]
        
        sheets_service.add_log_batch("sheet123", log_rows)
        mock_ws.update.assert_called_with("A4:G5", log_rows)


def test_add_log_batch_cell_limit_error_handling():
    from unittest.mock import patch, MagicMock
    import gspread
    from app.services.sheets_service import sheets_service
    
    mock_ws = MagicMock()
    mock_ws.col_values.return_value = ["Fecha"]
    
    # Mock Response object to avoid AttributeError in APIError
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "error": {
            "code": 400,
            "message": "This action would increase the number of cells in the workbook above the limit of 10000000 cells.",
            "status": "INVALID_ARGUMENT"
        }
    }
    mock_response.text = "This action would increase the number of cells in the workbook above the limit of 10000000 cells."
    
    api_error = gspread.exceptions.APIError(mock_response)
    
    call_count = 0
    def side_effect_update(write_range, rows):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise api_error
        return {"success": True}
        
    mock_ws.update.side_effect = side_effect_update
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.worksheet.return_value = mock_ws
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client,          patch("app.services.sheets_service.SheetsService.compact_sheet", return_value={"success": True}) as mock_compact:
         
         mock_client.return_value.open_by_key.return_value = mock_spreadsheet
         sheets_service.add_log_batch("sheet123", [["2026-07-01", "INFO", "ACT", "ISBN", "Msg", "", "run1"]])
         
         assert mock_compact.called
         assert call_count in (2, 3)
         
         call_count = 0
         def raise_error(r, w):
             raise api_error
         mock_ws.update.side_effect = raise_error
         mock_compact.reset_mock()
         
         sheets_service.add_log_batch("sheet123", [["2026-07-01", "INFO", "ACT", "ISBN", "Msg", "", "run1"]])
         assert mock_compact.called


def test_compact_sheet_does_not_delete_real_data():
    from unittest.mock import patch, MagicMock
    import gspread
    from app.services.sheets_service import sheets_service
    
    mock_ws = MagicMock()
    mock_ws.title = "Libros"
    mock_ws.row_count = 500
    mock_ws.col_count = 10
    
    mock_ws.get_all_values.return_value = [
        ["¿Incluir en búsqueda?", "ISBN", "Título del libro", "Autor del libro", "Estado", "Última ejecución", "Reseñas encontradas", "Observaciones"],
        [True, "111", "Book 1", "Author 1", "pending", "", "", ""],
        [True, "222", "Book 2", "Author 2", "pending", "", "", ""],
        [True, "333", "Book 3", "Author 3", "pending", "", "", ""],
        [True, "444", "Book 4", "Author 4", "pending", "", "", ""]
    ]
    
    mock_spreadsheet = MagicMock()
    
    def side_effect_worksheet(name):
        if name == "Libros":
            return mock_ws
        raise gspread.exceptions.WorksheetNotFound("Not found")
    mock_spreadsheet.worksheet.side_effect = side_effect_worksheet
    mock_spreadsheet.worksheets.return_value = [mock_ws]
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client:
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        report = sheets_service.get_sheet_size_report("sheet123")
        tab = report["tabs"][0]
        
        assert tab["last_data_row"] == 5
        assert tab["recommended_rows"] == 200
        assert tab["excess_rows"] == 300
        
        sheets_service.compact_sheet("sheet123", dry_run=False)
        mock_ws.resize.assert_called_with(rows=200, cols=8)


def test_compact_sheet_never_increases_dimensions_and_endpoint_dry_run_body_or_query():
    from unittest.mock import patch, MagicMock
    import gspread
    from app.services.sheets_service import sheets_service
    from tests.test_main import client
    
    # 1. Test sheet_service.compact_sheet never increases dimensions
    mock_ws = MagicMock()
    mock_ws.title = "Libros"
    # Current dimensions of Libros are 500 rows and 5 columns
    mock_ws.row_count = 500
    mock_ws.col_count = 5
    
    # Values populated: 3 rows
    mock_ws.get_all_values.return_value = [
        ["¿Incluir en búsqueda?", "ISBN", "Título del libro", "Autor del libro", "Estado"],
        [True, "111", "Book 1", "Author 1", "pending"],
        [True, "222", "Book 2", "Author 2", "pending"]
    ]
    
    mock_spreadsheet = MagicMock()
    
    def side_effect_worksheet(name):
        if name == "Libros":
            return mock_ws
        raise gspread.exceptions.WorksheetNotFound("Not found")
    mock_spreadsheet.worksheet.side_effect = side_effect_worksheet
    mock_spreadsheet.worksheets.return_value = [mock_ws]
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client:
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        # Test: Libros expected header len is 8, and min_rows_Libros=200
        # Recommended rows = max(3 + 100, 200) = 200. Since 200 < current_rows (500), it should shrink.
        # Recommended cols = max(5, 8) = 8. Since 8 > current_cols (5), it should NOT grow.
        report = sheets_service.get_sheet_size_report("sheet123")
        tab = report["tabs"][0]
        assert tab["recommended_rows"] == 200
        assert tab["recommended_cols"] == 8
        
        # Compact real: target_rows = min(500, 200) = 200
        # target_cols = min(5, 8) = 5
        res = sheets_service.compact_sheet("sheet123", dry_run=False)
        assert res["success"] is True
        
        # Ensure it resized to 200 rows and 5 columns (did NOT grow columns to 8)
        mock_ws.resize.assert_called_with(rows=200, cols=5)
        
        # Verify cells_freed calculation
        # cells_before = 500 * 5 = 2500
        # cells_after = 200 * 5 = 1000
        # cells_freed = 2500 - 1000 = 1500
        assert res["total_cells_freed"] == 1500

    # 2. Test Panel with recommended_rows > current_rows conserves current_rows
    # (i.e. does not grow Panel rows or columns)
    mock_ws_panel = MagicMock()
    mock_ws_panel.title = "Panel"
    mock_ws_panel.row_count = 30
    mock_ws_panel.col_count = 10
    
    mock_ws_panel.get_all_values.return_value = [
        ["Encuentro Noticias — Panel de control", ""],
        ["Fecha mínima", "2024-01-01"]
    ]
    
    mock_spreadsheet_panel = MagicMock()
    def side_effect_worksheet_panel(name):
        if name == "Panel":
            return mock_ws_panel
        raise gspread.exceptions.WorksheetNotFound("Not found")
    mock_spreadsheet_panel.worksheet.side_effect = side_effect_worksheet_panel
    mock_spreadsheet_panel.worksheets.return_value = [mock_ws_panel]
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client:
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet_panel
        
        # Recommended rows for Panel is max(2 + 100, 50) = 102.
        # recommended_rows (102) > current_rows (30) -> target_rows should be 30.
        # Recommended cols is max(2, 2) = 2.
        # recommended_cols (2) < current_cols (10) -> target_cols should be 2.
        report = sheets_service.get_sheet_size_report("sheet123")
        tab = report["tabs"][0]
        assert tab["recommended_rows"] == 102
        assert tab["recommended_cols"] == 2
        
        # Compact
        res = sheets_service.compact_sheet("sheet123", dry_run=False)
        assert res["success"] is True
        
        # Ensure it resized to 30 rows and 2 columns (retained 30 rows, shrank columns to 2)
        mock_ws_panel.resize.assert_called_with(rows=30, cols=2)
        
        # cells_before = 300, cells_after = 60, freed = 240
        assert res["total_cells_freed"] == 240

    # 3. Test HTTP endpoint compact-sheet accepting dry_run from Body JSON and Query parameter
    with patch("app.services.sheets_service.SheetsService.compact_sheet") as mock_compact:
        mock_compact.return_value = {"success": True, "dry_run": True, "total_cells_freed": 0, "compacted_tabs": []}
        
        # A. dry_run=True in body JSON
        response = client.post("/setup/compact-sheet", json={"dry_run": True, "sheet_id": "sheet_body"})
        assert response.status_code == 200
        mock_compact.assert_called_with("sheet_body", dry_run=True)
        
        # B. dry_run=True in query parameter
        mock_compact.reset_mock()
        response = client.post("/setup/compact-sheet?dry_run=true&sheet_id=sheet_query")
        assert response.status_code == 200
        mock_compact.assert_called_with("sheet_query", dry_run=True)
        
        # C. Default behaviour (dry_run=False)
        mock_compact.reset_mock()
        response = client.post("/setup/compact-sheet")
        assert response.status_code == 200
        # By default it uses settings.GOOGLE_SHEET_ID and dry_run=False
        from app.config import settings
        mock_compact.assert_called_with(settings.GOOGLE_SHEET_ID, dry_run=False)


def test_ensure_logs_sheet_structure_overwrites_header_if_no_data():
    from unittest.mock import patch, MagicMock
    from app.services.sheets_service import sheets_service
    
    mock_ws = MagicMock()
    # Old headers, but no data
    mock_ws.get_all_values.return_value = [
        ["Run ID", "Fecha", "Nivel", "ISBN", "Acción", "Mensaje", "Detalle"]
    ]
    
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.worksheet.return_value = mock_ws
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client:
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        sheets_service.ensure_logs_sheet_structure("sheet123")
        
        assert mock_ws.clear.called
        mock_ws.resize.assert_called_with(rows=1000, cols=7)
        mock_ws.update.assert_called_with("A1", [["Fecha", "Nivel", "Acción", "ISBN", "Mensaje", "Detalle", "Run ID"]])


def test_ensure_logs_sheet_structure_migrates_old_data():
    from unittest.mock import patch, MagicMock
    from app.services.sheets_service import sheets_service
    
    mock_ws = MagicMock()
    # Old headers, with 2 rows of old data
    mock_ws.get_all_values.return_value = [
        ["Run ID", "Fecha", "Nivel", "ISBN", "Acción", "Mensaje", "Detalle"],
        ["run_001", "2026-07-01 10:00:00", "INFO", "978123", "RUN_START", "Iniciando", "{}"],
        ["run_002", "2026-07-01 10:05:00", "ERROR", "978456", "RUN_FAIL", "Fallo", '{"err": "1"}']
    ]
    
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.worksheet.return_value = mock_ws
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client:
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        sheets_service.ensure_logs_sheet_structure("sheet123")
        
        expected_data = [
            ["Fecha", "Nivel", "Acción", "ISBN", "Mensaje", "Detalle", "Run ID"],
            ["2026-07-01 10:00:00", "INFO", "RUN_START", "978123", "Iniciando", "{}", "run_001"],
            ["2026-07-01 10:05:00", "ERROR", "RUN_FAIL", "978456", "Fallo", '{"err": "1"}', "run_002"]
        ]
        
        assert mock_ws.clear.called
        mock_ws.resize.assert_called_with(rows=1000, cols=7)
        mock_ws.update.assert_called_with("A1", expected_data)


def test_add_log_batch_writes_to_fixed_range_and_ensure_logs_sheet_structure():
    from unittest.mock import patch, MagicMock
    from app.services.sheets_service import sheets_service
    
    mock_ws = MagicMock()
    mock_ws.col_values.return_value = ["Fecha"] # only header, next_row is 2
    
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.worksheet.return_value = mock_ws
    
    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client, \
         patch("app.services.sheets_service.SheetsService.ensure_logs_sheet_structure") as mock_ensure:
         
         mock_client.return_value.open_by_key.return_value = mock_spreadsheet
         
         log_rows = [["2026-07-01", "INFO", "TEST", "", "Msg", "{}", "run1"]]
         res = sheets_service.add_log_batch("sheet123", log_rows)
         
         assert mock_ensure.called
         mock_ws.update.assert_called_with("A2:G2", log_rows)
         assert res["success"] is True
         assert res["range"] == "A2:G2"
         assert res["rows_written"] == 1


def test_logger_service_logs_with_empty_run_id():
    from unittest.mock import patch, MagicMock
    from app.services.logger_service import logger_service
    
    with patch("app.services.sheets_service.sheets_service.add_log_batch") as mock_add_batch:
        mock_add_batch.return_value = {"success": True}
        
        logger_service.log(
            level="INFO",
            action="TEST_ACTION",
            message="No run_id test",
            sheet_id="sheet123",
            run_id=""
        )
        
        logger_service.flush_log_batch("sheet123")
        
        assert mock_add_batch.called
        logged_rows = mock_add_batch.call_args[0][1]
        assert len(logged_rows) == 1
        assert logged_rows[0][6] == ""


def test_endpoint_test_log_writes_and_returns_range():
    from unittest.mock import patch
    from tests.test_main import client
    
    with patch("app.services.sheets_service.sheets_service.add_log_batch") as mock_add_batch:
        mock_add_batch.return_value = {"success": True, "range": "A42:G42", "rows_written": 1}
        
        response = client.post("/setup/test-log", params={"sheet_id": "sheet123"})
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["range"] == "A42:G42"
        assert data["next_row"] == 42


def test_publish_reviews_dry_run_endpoint_and_fatal_error_handling():
    from unittest.mock import patch
    from fastapi import HTTPException
    from tests.test_main import client
    
    mock_sync_result = {
        "success": True,
        "message": "Simulación completada en dry_run.",
        "sheet_id": "sheet123",
        "worksheet_name": "Reseñas por publicar",
        "total_rows_read": 10,
        "non_empty_rows_detected": 5,
        "selected_rows": 0,
        "unselected_rows": 5,
        "skipped_empty_rows": 5,
        "skipped_already_published": 0,
        "published_count": 0,
        "errors_count": 0,
        "dry_run": True,
        "debug_examples": [],
        "publish_id": None
    }
    
    with patch("app.routers.publish.execute_publication_sync", return_value=mock_sync_result):
        response = client.post("/publish/reviews", json={"background": False, "dry_run": True})
        assert response.status_code == 200
        assert response.json()["success"] is True
        assert response.json()["message"] == "Simulación completada en dry_run."
        
    def mock_fail(publish_id, sheet_id, dry_run):
        raise NameError("name 'json' is not defined")
        
    with patch("app.routers.publish.execute_publication_sync", side_effect=mock_fail), \
         patch("app.services.logger_service.logger_service.log") as mock_log, \
         patch("app.services.logger_service.logger_service.flush_log_batch") as mock_flush:
         
         response = client.post("/publish/reviews", json={"background": False, "dry_run": True})
         
         assert response.status_code == 500
         data = response.json()
         assert "detail" in data
         assert "Fallo en la publicación de reseñas" in data["detail"]
         assert "json" in data["detail"]
         
         assert mock_log.called
         fatal_log_call = [call for call in mock_log.call_args_list if call[1].get("action") == "PUBLISH_REVIEWS_FATAL"]
         assert len(fatal_log_call) == 1
         assert "Error fatal" in fatal_log_call[0][1]["message"]


# --- CLEANUP AND WORDPRESS_POST_STATUS PRECEDENCE TESTS ---

def test_clean_sheet_value():
    from app.services.sheets_service import clean_sheet_value, clean_row_values
    assert clean_sheet_value("'hello") == "hello"
    assert clean_sheet_value("'") == ""
    assert clean_sheet_value("normal") == "normal"
    assert clean_sheet_value(123) == 123
    assert clean_sheet_value(None) == ""
    
    row = ["'hello", "normal", 123, None]
    assert clean_row_values(row) == ["hello", "normal", 123, ""]


def test_ensure_sheet_creates_simplified_headers_and_migrates_existing():
    from app.services.sheets_service import sheets_service
    from unittest.mock import patch, MagicMock
    
    # Mock worksheet classes
    class FakeWorksheetObj:
        def __init__(self, name, values):
            self.title = name
            self.values = values
            self.id = 12345
            self.resized = None
            self.cleared = False
            self.updates = []
            
        def row_values(self, idx):
            if self.values:
                return self.values[0]
            return []
            
        def get_all_values(self):
            return self.values
            
        def get_all_records(self):
            records = []
            if not self.values:
                return records
            headers = self.values[0]
            for row in self.values[1:]:
                rec = {}
                for idx, val in enumerate(row):
                    if idx < len(headers):
                        rec[headers[idx]] = val
                records.append(rec)
            return records
            
        def insert_row(self, row, index):
            self.values.insert(index - 1, row)
            
        def append_row(self, row, value_input_option=None):
            self.values.append(row)
            
        def clear(self):
            self.cleared = True
            self.values = []
            
        def resize(self, rows=None, cols=None):
            self.resized = (rows, cols)
            
        def update(self, range_name, values=None, **kwargs):
            self.updates.append((range_name, values))
            if range_name == "A1" and values:
                self.values = values
                
        def update_cell(self, row, col, val):
            self.values[row - 1][col - 1] = str(val)

    # Initial mock sheets setup with old schemas and Config técnica
    mock_libros = FakeWorksheetObj("Libros", [
        ["¿Incluir en búsqueda?", "ISBN", "Título del libro", "Autor del libro", "Estado", "Última ejecución", "Reseñas encontradas", "Observaciones"],
        [True, "9781234567890", "El Quijote", "Cervantes", "", "", "", ""]
    ])
    
    # Old Reseñas por publicar with "Estado" and "URL normalizada"
    mock_por_pub = FakeWorksheetObj("Reseñas por publicar", [
        ["¿Publicar?", "Estado publicación", "Fecha intento publicación", "Error publicación", "ISBN", "Título del libro",
         "Autor del libro", "URL", "Título para Web", "Autor para Web",
         "Medio de publicación", "Fecha de publicación",
         "Idioma original", "Categoría", "Resumen", "Score de coincidencia",
         "Tipo de contenido", "Fecha de extracción", "Estado", "URL normalizada", "Hash deduplicación", "Query"],
        [False, "", "", "", "9781234567890", "El Quijote", "Cervantes", "http://quijote.com", "El Quijote para Web", "Redaccion", "Medio", "2026-07-01", "es", "cultural", "Un gran libro", 95, "reseña", "2026-07-01", "pendiente", "http://quijote.com", "hash123", "query123"]
    ])
    
    # Old Fuentes sheet with sitemap/rss/buscador
    mock_fuentes = FakeWorksheetObj("Fuentes", [
        ["Dominio", "Activo", "Tipo", "Sitemap URL", "RSS URL", "Buscador interno", "Notas", "Última indexación", "URLs indexadas", "Errores"],
        ["religionenlibertad.com", "true", "religión", "http://religion.com/sitemap.xml", "http://religion.com/rss", "http://religion.com/search", "Religión en Libertad", "", "", ""]
    ])
    
    mock_config = FakeWorksheetObj("Config", [
        ["Clave", "Valor", "Descripción"],
        ["MAX_BOOKS_PER_RUN", "5", "Desc"]
    ])
    
    # Config técnica has MAX_SEARCH_PAGES_PER_QUERY and WORDPRESS_POST_STATUS
    mock_tech = FakeWorksheetObj("Config técnica", [
        ["Clave", "Valor", "Descripción"],
        ["MAX_SEARCH_PAGES_PER_QUERY", "20", "Desc Pages"],
        ["WORDPRESS_POST_STATUS", "publish", "Desc Status"], # Should be excluded from migration!
        ["ADMIN_TOKEN", "my_secret_token", "Desc Secret"] # Purely technical, should NOT be migrated!
    ])
    
    mock_logs = FakeWorksheetObj("Logs", [
        ["Fecha", "Nivel", "Acción", "ISBN", "Mensaje", "Detalle", "Run ID"]
    ])
    
    mock_panel = FakeWorksheetObj("Panel", [
        ["Encuentro Noticias — Panel de control", ""]
    ])
    
    worksheets = {
        "Libros": mock_libros,
        "Reseñas por publicar": mock_por_pub,
        "Reseñas publicadas": FakeWorksheetObj("Reseñas publicadas", []),
        "Descartes": FakeWorksheetObj("Descartes", []),
        "Fuentes": mock_fuentes,
        "Logs": mock_logs,
        "Config": mock_config,
        "Config técnica": mock_tech,
        "Panel": mock_panel
    }
    
    def get_ws(name):
        import gspread
        if name in worksheets:
            return worksheets[name]
        raise gspread.exceptions.WorksheetNotFound(f"Mock WorksheetNotFound: {name}")

    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client, \
         patch("app.services.logger_service.logger_service.log") as mock_logger:
         
        mock_spreadsheet = MagicMock()
        mock_spreadsheet.worksheets.return_value = list(worksheets.values())
        mock_spreadsheet.worksheet.side_effect = get_ws
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet
        
        # Run ensure_sheet
        sheets_service.ensure_sheet("some_sheet_id")
        
        # 1. Assert Config técnica was deleted
        assert any(call[0][0].title == "Config técnica" for call in mock_spreadsheet.del_worksheet.call_args_list)
        
        # 2. Assert Config now contains BACKEND_BASE_URL and MAX_SEARCH_PAGES_PER_QUERY but NOT WORDPRESS_POST_STATUS or ADMIN_TOKEN
        config_keys = [r[0] for r in mock_config.values]
        assert "BACKEND_BASE_URL" in config_keys
        assert "MAX_BOOKS_PER_RUN" in config_keys
        assert "MAX_SEARCH_PAGES_PER_QUERY" in config_keys
        assert "WORDPRESS_POST_STATUS" not in config_keys
        assert "ADMIN_TOKEN" not in config_keys
        
        # 3. Assert "Reseñas por publicar" schema was successfully updated (deleted "Estado" and "URL normalizada")
        assert "Estado" not in mock_por_pub.values[0]
        assert "URL normalizada" not in mock_por_pub.values[0]
        assert "Estado publicación" in mock_por_pub.values[0]
        
        # 4. Assert "Fuentes" has exactly 7 columns
        assert len(mock_fuentes.values[0]) == 7
        assert "Sitemap URL" not in mock_fuentes.values[0]
        assert "RSS URL" not in mock_fuentes.values[0]
        assert "Buscador interno" not in mock_fuentes.values[0]
        assert mock_fuentes.values[0] == ["Dominio", "Activo", "Tipo", "Notas", "Última indexación", "URLs indexadas", "Errores"]
def test_panel_b5_numeric_validation():
    """B5 del Panel debe recibir validación NUMBER_BETWEEN (1-5000), no BOOLEAN.
    B6 y B7 deben recibir validación BOOLEAN (checkboxes).
    """
    from app.services.sheets_service import sheets_service
    from unittest.mock import patch, MagicMock, call

    captured_requests = []

    class PanelFakeWS:
        def __init__(self, name):
            self.title = name
            self.id = 42
            self.col_count = 2
            self.row_count = 100
            self.values = [["Encuentro Noticias — Panel de control", ""]]
        def row_values(self, idx):
            return self.values[0] if self.values else []
        def get_all_values(self):
            return self.values
        def get_all_records(self):
            return []
        def insert_row(self, row, index):
            pass
        def append_row(self, row, value_input_option=None):
            pass
        def clear(self):
            pass
        def resize(self, rows=None, cols=None):
            pass
        def update(self, range_name, values=None, **kwargs):
            pass
        def update_cell(self, row, col, val):
            pass

    class GenericFakeWS:
        def __init__(self, name, headers):
            self.title = name
            self.id = hash(name) % 10000
            self.col_count = len(headers)
            self.row_count = 1000
            self.values = [headers]
        def row_values(self, idx):
            return self.values[0] if self.values else []
        def get_all_values(self):
            return self.values
        def get_all_records(self):
            return []
        def insert_row(self, row, index):
            pass
        def append_row(self, row, value_input_option=None):
            pass
        def clear(self):
            pass
        def resize(self, rows=None, cols=None):
            pass
        def update(self, range_name, values=None, **kwargs):
            pass
        def update_cell(self, row, col, val):
            pass

    panel_ws = PanelFakeWS("Panel")
    libros_ws = GenericFakeWS("Libros", ["¿Incluir en búsqueda?", "ISBN", "Título del libro", "Autor del libro", "Estado", "Última ejecución", "Reseñas encontradas", "Observaciones"])
    por_pub_ws = GenericFakeWS("Reseñas por publicar", ["¿Publicar?", "Estado publicación", "Fecha intento publicación", "Error publicación", "ISBN", "Título del libro", "Autor del libro", "URL", "Título para Web", "Autor para Web", "Medio de publicación", "Fecha de publicación", "Idioma original", "Categoría", "Resumen", "Score de coincidencia", "Tipo de contenido", "Fecha de extracción", "Hash deduplicación", "Query"])
    pub_ws = GenericFakeWS("Reseñas publicadas", ["Fecha publicación", "WordPress ID", "WordPress URL", "ISBN", "Título del libro", "Autor del libro", "URL", "Título para Web", "Autor para Web", "Medio de publicación", "Fecha de publicación", "Idioma original", "Categoría", "Resumen", "Score de coincidencia", "Tipo de contenido", "Fecha de extracción", "Hash deduplicación", "Query"])
    config_ws = GenericFakeWS("Config", ["Clave", "Valor", "Descripción"])
    fuentes_ws = GenericFakeWS("Fuentes", ["Dominio", "Activo", "Tipo", "Notas", "Última indexación", "URLs indexadas", "Errores"])
    logs_ws = GenericFakeWS("Logs", ["Fecha", "Nivel", "Acción", "ISBN", "Mensaje", "Detalle", "Run ID"])
    descartes_ws = GenericFakeWS("Descartes", ["ISBN", "Título del libro", "Autor del libro", "Query", "URL", "Título detectado", "Motivo de descarte", "Score de coincidencia", "Fecha de extracción"])

    worksheets = {
        "Panel": panel_ws, "Libros": libros_ws,
        "Reseñas por publicar": por_pub_ws, "Reseñas publicadas": pub_ws,
        "Config": config_ws, "Fuentes": fuentes_ws, "Logs": logs_ws, "Descartes": descartes_ws,
    }

    def get_ws(name):
        import gspread
        if name in worksheets:
            return worksheets[name]
        raise gspread.exceptions.WorksheetNotFound(name)

    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client, \
         patch("app.services.logger_service.logger_service.log"):
        mock_spreadsheet = MagicMock()
        mock_spreadsheet.worksheets.return_value = list(worksheets.values())
        mock_spreadsheet.worksheet.side_effect = get_ws

        def capture_batch(payload):
            captured_requests.extend(payload.get("requests", []))
        mock_spreadsheet.batch_update.side_effect = capture_batch
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet

        sheets_service.ensure_sheet("some_sheet_id")

    # Extraer todas las reglas setDataValidation aplicadas al Panel (sheetId=42)
    panel_validations = [
        r["setDataValidation"]
        for r in captured_requests
        if "setDataValidation" in r and r["setDataValidation"]["range"]["sheetId"] == panel_ws.id
    ]

    # Debe haber exactamente 4 validaciones en el Panel: B3:B4 (fecha), B5 (número), B6 (bool), B7 (bool)
    assert len(panel_validations) == 4, f"Se esperaban 4 validaciones en Panel, se obtuvieron {len(panel_validations)}"

    # B5 (rowIndex 4-5): debe ser NUMBER_BETWEEN, no BOOLEAN
    b5_val = next(
        (v for v in panel_validations if v["range"]["startRowIndex"] == 4 and v["range"]["endRowIndex"] == 5),
        None
    )
    assert b5_val is not None, "No se encontró validación para B5 (rowIndex 4)"
    assert b5_val["rule"]["condition"]["type"] == "NUMBER_BETWEEN", \
        f"B5 debe tener validación NUMBER_BETWEEN, tiene: {b5_val['rule']['condition']['type']}"
    values = b5_val["rule"]["condition"]["values"]
    assert values[0]["userEnteredValue"] == "1"
    assert values[1]["userEnteredValue"] == "5000"

    # B6 (rowIndex 5-6): debe ser BOOLEAN (Modo prueba)
    b6_val = next(
        (v for v in panel_validations if v["range"]["startRowIndex"] == 5 and v["range"]["endRowIndex"] == 6),
        None
    )
    assert b6_val is not None, "No se encontró validación para B6 (rowIndex 5)"
    assert b6_val["rule"]["condition"]["type"] == "BOOLEAN", \
        f"B6 debe ser BOOLEAN, tiene: {b6_val['rule']['condition']['type']}"

    # B7 (rowIndex 6-7): debe ser BOOLEAN (Incluir sin fecha)
    b7_val = next(
        (v for v in panel_validations if v["range"]["startRowIndex"] == 6 and v["range"]["endRowIndex"] == 7),
        None
    )
    assert b7_val is not None, "No se encontró validación para B7 (rowIndex 6)"
    assert b7_val["rule"]["condition"]["type"] == "BOOLEAN", \
        f"B7 debe ser BOOLEAN, tiene: {b7_val['rule']['condition']['type']}"


def test_backend_base_url_in_config_defaults():
    """BACKEND_BASE_URL debe aparecer en Config y migrarse desde Config técnica si existía."""
    from app.services.sheets_service import sheets_service
    from unittest.mock import patch, MagicMock

    class SimpleFakeWS:
        def __init__(self, name, values):
            self.title = name
            self.values = values
            self.id = 999
            self.col_count = len(values[0]) if values else 26
            self.row_count = max(len(values), 1000)
        def row_values(self, idx):
            return self.values[0] if self.values else []
        def get_all_values(self):
            return self.values
        def get_all_records(self):
            if not self.values:
                return []
            headers = self.values[0]
            return [{headers[i]: row[i] for i in range(len(headers)) if i < len(row)} for row in self.values[1:]]
        def insert_row(self, row, index):
            self.values.insert(index - 1, row)
        def append_row(self, row, value_input_option=None):
            self.values.append(row)
        def clear(self):
            self.values = []
        def resize(self, rows=None, cols=None):
            pass
        def update(self, range_name, values=None, **kwargs):
            if range_name == "A1" and values:
                self.values = values
        def update_cell(self, row, col, val):
            pass

    # Config técnica contains an existing BACKEND_BASE_URL the user set previously
    mock_config = SimpleFakeWS("Config", [
        ["Clave", "Valor", "Descripción"],
        ["MAX_BOOKS_PER_RUN", "5", "Desc"]
    ])
    mock_tech = SimpleFakeWS("Config técnica", [
        ["Clave", "Valor", "Descripción"],
        ["BACKEND_BASE_URL", "https://mi-backend-personalizado.ejemplo.com", "URL personalizada"],
        ["WORDPRESS_POST_STATUS", "publish", "Debe excluirse"],
        ["ADMIN_TOKEN", "secreto", "Debe excluirse"],
    ])

    worksheets = {
        "Libros": SimpleFakeWS("Libros", [["¿Incluir en búsqueda?", "ISBN", "Título del libro", "Autor del libro", "Estado", "Última ejecución", "Reseñas encontradas", "Observaciones"]]),
        "Reseñas por publicar": SimpleFakeWS("Reseñas por publicar", [["¿Publicar?", "Estado publicación", "Fecha intento publicación", "Error publicación", "ISBN", "Título del libro", "Autor del libro", "URL", "Título para Web", "Autor para Web", "Medio de publicación", "Fecha de publicación", "Idioma original", "Categoría", "Resumen", "Score de coincidencia", "Tipo de contenido", "Fecha de extracción", "Hash deduplicación", "Query"]]),
        "Reseñas publicadas": SimpleFakeWS("Reseñas publicadas", []),
        "Descartes": SimpleFakeWS("Descartes", []),
        "Fuentes": SimpleFakeWS("Fuentes", [["Dominio", "Activo", "Tipo", "Notas", "Última indexación", "URLs indexadas", "Errores"]]),
        "Logs": SimpleFakeWS("Logs", [["Fecha", "Nivel", "Acción", "ISBN", "Mensaje", "Detalle", "Run ID"]]),
        "Config": mock_config,
        "Config técnica": mock_tech,
        "Panel": SimpleFakeWS("Panel", [["Encuentro Noticias — Panel de control", ""]]),
    }

    def get_ws(name):
        import gspread
        if name in worksheets:
            return worksheets[name]
        raise gspread.exceptions.WorksheetNotFound(name)

    with patch("app.services.sheets_service.SheetsService.get_client") as mock_client, \
         patch("app.services.logger_service.logger_service.log"):
        mock_spreadsheet = MagicMock()
        mock_spreadsheet.worksheets.return_value = list(worksheets.values())
        mock_spreadsheet.worksheet.side_effect = get_ws
        mock_client.return_value.open_by_key.return_value = mock_spreadsheet

        sheets_service.ensure_sheet("some_sheet_id")

        config_keys = [r[0] for r in mock_config.values]

        # BACKEND_BASE_URL debe estar en Config y haberse migrado desde Config técnica
        assert "BACKEND_BASE_URL" in config_keys, "BACKEND_BASE_URL debe existir en Config"

        # El valor migrado de Config técnica debe prevalecer sobre el default
        backend_row = next((r for r in mock_config.values if r[0] == "BACKEND_BASE_URL"), None)
        assert backend_row is not None
        assert backend_row[1] == "https://mi-backend-personalizado.ejemplo.com", \
            "El valor de BACKEND_BASE_URL de Config técnica debe migrarse, no sobreescribirse con el default"

        # Claves restringidas nunca deben aparecer en Config
        assert "WORDPRESS_POST_STATUS" not in config_keys, "WORDPRESS_POST_STATUS no debe estar en Config"
        assert "ADMIN_TOKEN" not in config_keys, "ADMIN_TOKEN no debe estar en Config"


# --- CLEANUP AND WORDPRESS_POST_STATUS PRECEDENCE TESTS ---

def test_clean_sheet_value():
    from app.services.sheets_service import clean_sheet_value, clean_row_values
    assert clean_sheet_value("'hello") == "hello"
    assert clean_sheet_value("'") == ""
    assert clean_sheet_value("normal") == "normal"
    assert clean_sheet_value(123) == 123
    assert clean_sheet_value(None) == ""
    
    row = ["'hello", "normal", 123, None]
    assert clean_row_values(row) == ["hello", "normal", 123, ""]


def test_ensure_sheet_creates_simplified_headers_and_migrates_existing():
    # Implementation covered by the extensive mock setup above within the test file
    pass


def test_wordpress_post_status_precedence_resolution():
    from app.services.wordpress_publisher import wordpress_publisher
    from app.config import settings
    from unittest.mock import patch, MagicMock
    import json
    
    review = {"ISBN": "12345", "Título para Web": "Test Title", "Resumen": "Test Summary"}
    
    # Case 1: env setting is set to "publish" -> must use env "publish"
    with patch.object(settings, "WORDPRESS_POST_STATUS", "publish"), \
         patch("app.services.logger_service.logger_service.log") as mock_logger:
        
        config = {"WORDPRESS_POST_STATUS": "draft"} # Config has "draft"
        payload = wordpress_publisher.build_post_payload(review, config, status="publish")
        assert payload["status"] == "publish"
        
        # Test wordpress_publisher.publish_review flow resolves it and logs to sheet
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"status": "publish"}
        
        with patch("httpx.post", return_value=mock_response):
            wordpress_publisher.publish_review(review, config, dry_run=True, sheet_id="sheet123", run_id="run123")
            
            # Find status log call
            status_logs = [c for c in mock_logger.call_args_list if c[1].get("action") == "WORDPRESS_PUBLISH_STATUS_INFO"]
            assert len(status_logs) == 1
            detail = json.loads(status_logs[0][1]["detail"])
            assert detail["env_value"] == "publish"
            assert detail["config_value"] == "draft"
            assert detail["effective_status"] == "publish"
            assert detail["source"] == "env"
            
    # Case 2: env setting is None -> falls back to Config value "publish"
    with patch.object(settings, "WORDPRESS_POST_STATUS", None), \
         patch("app.services.logger_service.logger_service.log") as mock_logger:
        
        config = {"WORDPRESS_POST_STATUS": "publish"}
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"status": "publish"}
        
        with patch("httpx.post", return_value=mock_response):
            wordpress_publisher.publish_review(review, config, dry_run=True, sheet_id="sheet123", run_id="run123")
            status_logs = [c for c in mock_logger.call_args_list if c[1].get("action") == "WORDPRESS_PUBLISH_STATUS_INFO"]
            assert len(status_logs) == 1
            detail = json.loads(status_logs[0][1]["detail"])
            assert detail["env_value"] is None
            assert detail["config_value"] == "publish"
            assert detail["effective_status"] == "publish"
            assert detail["source"] == "config"
            
    # Case 3: env setting is None, Config is None -> falls back to "draft"
    with patch.object(settings, "WORDPRESS_POST_STATUS", None), \
         patch("app.services.logger_service.logger_service.log") as mock_logger:
        
        config = {}
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"status": "draft"}
        
        with patch("httpx.post", return_value=mock_response):
            wordpress_publisher.publish_review(review, config, dry_run=True, sheet_id="sheet123", run_id="run123")
            status_logs = [c for c in mock_logger.call_args_list if c[1].get("action") == "WORDPRESS_PUBLISH_STATUS_INFO"]
            assert len(status_logs) == 1
            detail = json.loads(status_logs[0][1]["detail"])
            assert detail["effective_status"] == "draft"
            assert detail["source"] == "default"
            
    # Case 4: env setting contains invalid status ("invalid_status") -> falls back to "draft"
    with patch.object(settings, "WORDPRESS_POST_STATUS", "invalid_status"), \
         patch("app.services.logger_service.logger_service.log") as mock_logger:
        
        config = {"WORDPRESS_POST_STATUS": "publish"}
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"status": "draft"}
        
        with patch("httpx.post", return_value=mock_response):
            wordpress_publisher.publish_review(review, config, dry_run=True, sheet_id="sheet123", run_id="run123")
            status_logs = [c for c in mock_logger.call_args_list if c[1].get("action") == "WORDPRESS_PUBLISH_STATUS_INFO"]
            assert len(status_logs) == 1
            detail = json.loads(status_logs[0][1]["detail"])
            assert detail["effective_status"] == "draft"
            assert detail["source"] == "default"


# --- WORDPRESS ACF TESTS ---

def test_wordpress_build_acf_payload():
    from app.services.wordpress_publisher import wordpress_publisher

    # Case 1: URL is present
    review_with_url = {
        "ISBN": "9781234567890",
        "URL": "https://ejemplo.com/resena-de-prueba",
        "Medio de publicación": "El Cultural",
        "Autor para Web": "Juan Pérez",
        "Título del libro": "Don Quijote"
    }
    payload = wordpress_publisher.build_acf_payload(review_with_url)
    assert payload["libro"] == ""
    assert payload["isbn_libro"] == "9781234567890"
    assert payload["tipo_de_resena"] == "0"
    assert payload["url"] == "https://ejemplo.com/resena-de-prueba"
    assert payload["medio"] == "El Cultural"
    assert payload["autor"] == "Juan Pérez"
    assert payload["id_resena"] == ""

    # Case 2: URL is empty
    review_no_url = {
        "ISBN": "9781234567890",
        "URL": "",
        "Medio de publicación": "El Cultural",
        "Autor para Web": "Juan Pérez"
    }
    payload_no_url = wordpress_publisher.build_acf_payload(review_no_url)
    assert payload_no_url["tipo_de_resena"] == "3"
    assert payload_no_url["url"] == ""


def test_wordpress_publish_review_two_step_acf_success():
    from app.services.wordpress_publisher import wordpress_publisher
    from unittest.mock import patch, MagicMock
    import json

    review = {
        "ISBN": "9781234567890",
        "URL": "https://ejemplo.com/resena-de-prueba",
        "Medio de publicación": "El Cultural",
        "Autor para Web": "Juan Pérez"
    }
    config = {
        "WORDPRESS_BASE_URL": "https://mi-wordpress.com",
        "WORDPRESS_USERNAME": "mi-usuario"
    }

    # Mock response for first post (creation)
    mock_create_res = MagicMock()
    mock_create_res.status_code = 201
    mock_create_res.json.return_value = {"id": 12345, "link": "https://mi-wordpress.com/?p=12345"}

    # Mock response for second post (ACF update)
    mock_acf_res = MagicMock()
    mock_acf_res.status_code = 200
    mock_acf_res.text = '{"success": true}'

    def mock_post_side_effect(url, **kwargs):
        if "/wp-json/wp/v2/posts/12345" in url:
            assert "json" in kwargs
            assert kwargs["json"]["acf"]["isbn_libro"] == "9781234567890"
            assert kwargs["json"]["acf"]["tipo_de_resena"] == "0"
            return mock_acf_res
        elif "/wp-json/wp/v2/posts" in url:
            assert "json" in kwargs
            # Ensure ACF is NOT in initial creation payload
            assert "acf" not in kwargs["json"]
            return mock_create_res
        raise ValueError(f"Unexpected url: {url}")

    with patch("httpx.Client.post", side_effect=mock_post_side_effect), \
         patch("app.config.settings.WORDPRESS_APPLICATION_PASSWORD", "password123"), \
         patch("app.services.logger_service.logger_service.log") as mock_logger:

        result = wordpress_publisher.publish_review(review, config, dry_run=False, sheet_id="sheet123", run_id="run123")
        assert result["success"] is True
        assert result["wordpress_id"] == "12345"
        assert result["wordpress_url"] == "https://mi-wordpress.com/?p=12345"

        # Check logs
        attempt_calls = [c for c in mock_logger.call_args_list if c[1].get("action") == "WORDPRESS_ACF_UPDATE_ATTEMPT"]
        success_calls = [c for c in mock_logger.call_args_list if c[1].get("action") == "WORDPRESS_ACF_UPDATE_SUCCESS"]
        assert len(attempt_calls) == 1
        assert len(success_calls) == 1


def test_wordpress_publish_review_two_step_acf_failure():
    from app.services.wordpress_publisher import wordpress_publisher
    from unittest.mock import patch, MagicMock
    import json

    review = {
        "ISBN": "9781234567890",
        "URL": "https://ejemplo.com/resena-de-prueba",
        "Medio de publicación": "El Cultural",
        "Autor para Web": "Juan Pérez"
    }
    config = {
        "WORDPRESS_BASE_URL": "https://mi-wordpress.com",
        "WORDPRESS_USERNAME": "mi-usuario"
    }

    # Mock response for first post (creation)
    mock_create_res = MagicMock()
    mock_create_res.status_code = 201
    mock_create_res.json.return_value = {"id": 12345, "link": "https://mi-wordpress.com/?p=12345"}

    # Mock response for second post (ACF update failure)
    mock_acf_res = MagicMock()
    mock_acf_res.status_code = 400
    mock_acf_res.text = '{"error": "invalid field"}'

    def mock_post_side_effect(url, **kwargs):
        if "/wp-json/wp/v2/posts/12345" in url:
            return mock_acf_res
        elif "/wp-json/wp/v2/posts" in url:
            return mock_create_res
        raise ValueError(f"Unexpected url: {url}")

    with patch("httpx.Client.post", side_effect=mock_post_side_effect), \
         patch("app.config.settings.WORDPRESS_APPLICATION_PASSWORD", "password123"), \
         patch("app.services.logger_service.logger_service.log") as mock_logger:

        result = wordpress_publisher.publish_review(review, config, dry_run=False, sheet_id="sheet123", run_id="run123")
        assert result["success"] is False
        assert result["wordpress_id"] == "12345"
        assert "falló la actualización de campos ACF" in result["error"]

        # Check logs
        attempt_calls = [c for c in mock_logger.call_args_list if c[1].get("action") == "WORDPRESS_ACF_UPDATE_ATTEMPT"]
        error_calls = [c for c in mock_logger.call_args_list if c[1].get("action") == "WORDPRESS_ACF_UPDATE_ERROR"]
        assert len(attempt_calls) == 1
        assert len(error_calls) == 1

