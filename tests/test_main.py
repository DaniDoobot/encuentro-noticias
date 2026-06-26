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

def test_search_rate_limit_detection():
    """
    Verifies that search providers correctly identify and raise rate-limiting exceptions.
    """
    provider = DuckDuckGoSearchProvider()
    
    mock_response = MagicMock()
    mock_response.status_code = 202
    
    with patch("httpx.Client.get", return_value=mock_response):
        with pytest.raises(SearchProviderRateLimitError):
            provider.search("Ficciones Borges", timeout=5)

def test_bing_parser():
    """
    Verifies that BingHtmlSearchProvider extracts organic result links and discards internal Bing ones.
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
        urls = provider.search("Borges", timeout=5)
        assert len(urls) == 2
        assert "https://revistadelibros.com/review1" in urls
        assert "https://www.aceprensa.com/critica" in urls
        assert "https://www.bing.com/search?q=something" not in urls

