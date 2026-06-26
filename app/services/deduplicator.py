import hashlib
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from typing import Dict, Set, List, Any

class Deduplicator:
    @staticmethod
    def normalize_url(url: str) -> str:
        """
        Normalizes a URL by:
        - Removing UTM query parameters
        - Removing fragment identifiers
        - Lowercasing the scheme and domain
        - Stripping the trailing slash
        """
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            
            # Lowercase network location (domain)
            netloc = parsed.netloc.lower()
            scheme = parsed.scheme.lower()
            
            # Filter query parameters, discarding UTM variables
            qsl = parse_qsl(parsed.query)
            filtered_qsl = [(k, v) for k, v in qsl if not k.lower().startswith("utm_")]
            
            # Reassemble URL (discarding fragment)
            normalized = urlunparse((
                scheme,
                netloc,
                parsed.path,
                parsed.params,
                urlencode(filtered_qsl) if filtered_qsl else "",
                ""  # No fragment
            ))
            
            # Strip trailing slash if present
            if normalized.endswith("/"):
                normalized = normalized[:-1]
                
            return normalized
        except Exception:
            return url.strip()

    @classmethod
    def get_primary_hash(cls, isbn: str, url: str) -> str:
        """
        Generates the primary deduplication hash: sha256(ISBN + URL_NORMALIZADA)
        """
        isbn_clean = str(isbn).strip()
        normalized_url = cls.normalize_url(url)
        input_str = f"{isbn_clean}{normalized_url}"
        return hashlib.sha256(input_str.encode("utf-8")).hexdigest()

    @staticmethod
    def get_secondary_key(isbn: str, domain: str, article_title: str) -> str:
        """
        Generates the secondary deduplication key: ISBN + dominio + título del artículo normalizado
        """
        isbn_clean = str(isbn).strip()
        dom_clean = str(domain).strip().lower()
        if dom_clean.startswith("www."):
            dom_clean = dom_clean[4:]
            
        # Clean title: lowercase, alphanumeric characters only
        title_clean = str(article_title).strip().lower()
        title_clean = re.sub(r"[^\w\s]", "", title_clean)
        title_clean = re.sub(r"\s+", "", title_clean) # Remove all whitespaces
        
        return f"{isbn_clean}:{dom_clean}:{title_clean}"

    @classmethod
    def extract_hashes_from_reviews(cls, reviews: List[Dict[str, Any]]) -> Set[str]:
        """
        Extracts all primary hashes from existing sheet review records.
        """
        hashes = set()
        for row in reviews:
            h = str(row.get("Hash deduplicación", "")).strip()
            if h:
                hashes.add(h)
        return hashes

    @classmethod
    def extract_secondary_keys_from_reviews(cls, reviews: List[Dict[str, Any]]) -> Set[str]:
        """
        Extracts all secondary keys from existing sheet review records.
        """
        keys = set()
        for row in reviews:
            isbn = str(row.get("ISBN", "")).strip()
            url = str(row.get("URL", "")).strip()
            title = str(row.get("Título del artículo", "")).strip()
            
            if isbn and url and title:
                try:
                    parsed = urlparse(url)
                    domain = parsed.netloc
                    key = cls.get_secondary_key(isbn, domain, title)
                    keys.add(key)
                except Exception:
                    pass
        return keys

deduplicator = Deduplicator()
