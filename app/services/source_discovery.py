"""
source_discovery.py — Matches books against the local URL index.

Scoring heuristic (applied to each indexed URL):
    - Normalized title found in title/snippet/url  : +60
    - Normalized author found in title/snippet      : +25
    - ISBN found in title/snippet/url               : +80
    - Review/critique keywords in title/snippet     : +10
    - Exact title word match (all words present)    : +15 (bonus on top of +60)
"""
import unicodedata
import re
import logging
from typing import List, Dict, Any, Optional

from app.services.cache_service import cache_service
from app.config import settings

logger = logging.getLogger("encuentro-noticias")

# Keywords that indicate a review/cultural article
REVIEW_KEYWORDS = [
    "reseña", "resena", "crítica", "critica", "análisis", "analisis",
    "review", "opinión", "opinion", "lectura", "libro", "libros",
    "ensayo", "literatura", "comentario", "valoración",
]


STOPWORDS = {"el", "la", "los", "las", "de", "del", "en", "un", "una", "y", "sobre", "por", "a", "al"}


def _filter_stopwords(text: str) -> str:
    """Filter out common Spanish stopwords."""
    words = text.split()
    return " ".join([w for w in words if w not in STOPWORDS])


def _get_slug_text(url: str) -> str:
    """Extract and normalize the slug text of a URL."""
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path.rstrip("/")
        if not path:
            return ""
        slug = path.split("/")[-1]
        return slug.replace("-", " ").replace("_", " ")
    except Exception:
        return ""


def _normalize(text: str) -> str:
    """Lowercase, remove accents/diacritics, collapse whitespace, remove punctuation."""
    if not text:
        return ""
    # Remove accents
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Lowercase
    ascii_text = ascii_text.lower()
    # Remove punctuation except spaces and hyphens
    ascii_text = re.sub(r"[^\w\s-]", " ", ascii_text)
    # Collapse whitespace
    return re.sub(r"\s+", " ", ascii_text).strip()


def _normalize_isbn(isbn: str) -> str:
    """Strip hyphens from ISBN."""
    return re.sub(r"[-\s]", "", isbn or "")


class SourceDiscovery:

    def find_candidates(
        self,
        title: str,
        author: str,
        isbn: str,
        config: Dict[str, Any],
        domain_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search the local SQLite index for URLs that match a book.
        Returns a list of candidate dicts sorted by score descending.
        Each dict: {url, domain, title, snippet, score, matched_fields, provider, query}
        """
        min_score = int(config.get("DOMAIN_INDEX_MIN_SCORE", settings.DOMAIN_INDEX_MIN_SCORE))
        db_path = config.get("DOMAIN_INDEX_DB_PATH", settings.DOMAIN_INDEX_DB_PATH)

        # Ensure DB is initialised (no-op if already done)
        try:
            cache_service.init_db(db_path)
        except Exception as e:
            logger.warning(f"SourceDiscovery: could not init DB: {e}")
            return []

        norm_title = _normalize(title)
        norm_author = _normalize(author)
        norm_isbn = _normalize_isbn(isbn)
        
        # Filter stopwords from query title
        clean_title = _filter_stopwords(norm_title)
        title_words = [w for w in clean_title.split() if len(w) > 2]

        # Build search terms for the SQLite LIKE query
        search_terms: List[str] = []
        if clean_title:
            # Use the first 2-3 significant words for broad recall
            search_terms.extend(title_words[:3])
        if norm_author:
            author_parts = norm_author.split()
            search_terms.extend(author_parts[:2])
        if norm_isbn:
            search_terms.append(norm_isbn)

        if not search_terms:
            return []

        rows = cache_service.search_by_text(search_terms, domain_filter=domain_filter)

        candidates: List[Dict[str, Any]] = []
        for row in rows:
            score, matched_fields = self._score_row(
                row, norm_title, clean_title, norm_author, norm_isbn, title_words
            )
            if score >= min_score:
                candidates.append({
                    "url": row["url"],
                    "domain": row["domain"],
                    "title": row.get("title", ""),
                    "snippet": row.get("snippet", ""),
                    "pub_date": row.get("pub_date", ""),
                    "score": score,
                    "matched_fields": matched_fields,
                    "provider": "DomainIndex",
                    "query": "local_index",
                    "source_type": row.get("source_type", ""),
                })

        # Sort by score descending
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates

    def _score_row(
        self,
        row: Dict[str, Any],
        norm_title: str,
        clean_title: str,
        norm_author: str,
        norm_isbn: str,
        title_words: List[str],
    ):
        score = 0
        matched_fields = []

        row_title = _normalize(row.get("title", ""))
        row_snippet = _normalize(row.get("snippet", ""))
        row_url = _normalize(row.get("url", ""))
        slug_text = _normalize(_get_slug_text(row_url))
        
        # 1. Título exacto en title / snippet / slug_text: +60
        if clean_title:
            if clean_title in _filter_stopwords(row_title):
                score += 60
                matched_fields.append("title")
            elif clean_title in _filter_stopwords(row_snippet):
                score += 60
                matched_fields.append("snippet")
            elif clean_title in _filter_stopwords(slug_text):
                score += 60
                matched_fields.append("url_slug")

        # 2. Todas las palabras importantes del título presentes: +45
        # 3. Parte significativa del título presente (al menos la mitad): +25
        if title_words:
            words_in_meta = sum(1 for w in title_words if w in row_title or w in row_snippet)
            words_in_slug = sum(1 for w in title_words if w in slug_text)
            words_combined = sum(1 for w in title_words if w in row_title or w in row_snippet or w in slug_text)
            
            if words_combined == len(title_words):
                score += 45
                if words_in_meta == len(title_words):
                    matched_fields.append("title")
                if words_in_slug == len(title_words):
                    matched_fields.append("url_slug")
            elif words_combined >= max(1, len(title_words) // 2):
                score += 25
                if words_in_meta >= max(1, len(title_words) // 2):
                    matched_fields.append("title")
                if words_in_slug >= max(1, len(title_words) // 2):
                    matched_fields.append("url_slug")

        # 4. Autor presente: +25
        if norm_author:
            if norm_author in row_title or norm_author in row_snippet:
                score += 25
                matched_fields.append("author")
            else:
                author_words = [w for w in norm_author.split() if len(w) > 3]
                if author_words and all(w in row_title or w in row_snippet for w in author_words[:2]):
                    score += 25
                    matched_fields.append("author")

        # 5. ISBN presente: +80
        if norm_isbn:
            clean_combined = (row_title + " " + row_snippet + " " + row_url).replace("-", "").replace(" ", "")
            if norm_isbn in clean_combined:
                score += 80
                matched_fields.append("isbn")

        # 6. URL contiene términos del título (o slug): +20
        if title_words and any(w in slug_text or w in row_url for w in title_words):
            score += 20
            matched_fields.append("url_slug")

        # Review keyword bonus (+10) - optional extra
        for kw in REVIEW_KEYWORDS:
            if kw in row_title or kw in row_snippet:
                score += 10
                break

        # Deduplicate matched_fields and keep only requested ones
        allowed_fields = {"title", "snippet", "url_slug", "author", "isbn"}
        filtered_matched = []
        for f in matched_fields:
            if f in allowed_fields and f not in filtered_matched:
                filtered_matched.append(f)

        return score, filtered_matched


source_discovery = SourceDiscovery()

