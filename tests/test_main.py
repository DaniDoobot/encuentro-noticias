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

    def mock_extract(url):
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
        # If it's a broad query (doesn't contain "reseña" or "crítica" or "libro" and title is YOUCAT Biblia)
        q_lower = query.lower()
        if "reseña" not in q_lower and "crítica" not in q_lower and "libro" not in q_lower:
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
         
         # Verification 1: RUN_CONFIG_EFFECTIVE reflects config values and GOOGLE_NEWS_BROAD_MAX_QUERIES = 10
         effective_config_logs = [call for call in mock_log.call_args_list if call[0][1] == "RUN_CONFIG_EFFECTIVE"]
         assert len(effective_config_logs) == 1
         payload = json.loads(effective_config_logs[0][1].get("detail", "{}"))
         assert payload.get("MIN_MATCH_SCORE") == 10
         assert payload.get("GOOGLE_NEWS_BROAD_MAX_QUERIES") == 10
         
         # Verification 2: AUTHOR_NORMALIZED_AS_GENERIC is logged
         generic_author_logs = [call for call in mock_log.call_args_list if call[0][1] == "AUTHOR_NORMALIZED_AS_GENERIC"]
         assert len(generic_author_logs) == 1
         auth_payload = json.loads(generic_author_logs[0][1].get("detail", "{}"))
         assert auth_payload.get("original_author") == "VV.AA."
         assert auth_payload.get("author_is_generic") is True
         
         # Verification 3: BOOK_QUERIES_BUILT is logged separating categories
         queries_built_logs = [call for call in mock_log.call_args_list if call[0][1] == "BOOK_QUERIES_BUILT"]
         assert len(queries_built_logs) == 1
         queries_payload = json.loads(queries_built_logs[0][1].get("detail", "{}"))
         assert "prioritarias" in queries_payload
         assert "broad_queries" in queries_payload
         assert "youcat biblia" in queries_payload["broad_queries"]

         # Verification 4: GOOGLE_NEWS_BROAD_STARTED is logged
         broad_started_logs = [call for call in mock_log.call_args_list if call[0][1] == "GOOGLE_NEWS_BROAD_STARTED"]
         assert len(broad_started_logs) >= 1

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

    with patch("app.routers.setup.GoogleNewsRssSearchProvider.search", return_value=mock_rss_result):
        response = client.get("/debug/google-news?q=youcat%20biblia")

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "youcat biblia"
    assert data["parsed_results_count"] == 1
    assert data["results"][0]["source"] == "Zenda"
    assert data["results"][0]["url"] == "https://www.zendalibros.com/youcat-article"
