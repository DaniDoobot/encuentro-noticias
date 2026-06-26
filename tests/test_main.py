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
    
    queries = query_builder.build_queries(title, author, isbn)
    
    # Verify expected queries are in the output list
    assert f'"{title}" "{author}"' in queries
    assert f'"{title}" "{author}" reseña' in queries
    assert f'"{isbn}" "{title}"' in queries
    assert f'"{title}" "{author}" review' in queries
    assert f'"{title}" "{author}" reseña -comprar -amazon -fnac -casadellibro -iberlibro' in queries
    
    # Ensure double quotes inside titles are escaped/handled
    title_with_quotes = 'El "Quijote"'
    queries_q = query_builder.build_queries(title_with_quotes, author, isbn)
    assert f'"El \'Quijote\'" "{author}"' in queries_q

    # Verify site-specific queries
    domains = ["revistadelibros.com", "aceprensa.com"]
    queries_d = query_builder.build_queries(title, author, isbn, review_domains=domains)
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
