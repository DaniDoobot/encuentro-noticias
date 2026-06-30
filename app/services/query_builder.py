import urllib.parse
from typing import List, Dict
import re
import unicodedata

class QueryBuilder:
    @staticmethod
    def is_generic_author(auth_str: str) -> bool:
        if not auth_str:
            return True
        norm = auth_str.strip().lower()
        # Remove accents/diacritics
        norm = "".join(c for c in unicodedata.normalize('NFD', norm) if unicodedata.category(c) != 'Mn')
        # Keep alphanumeric only
        norm = re.sub(r'[^a-z0-9]', '', norm)
        # Match against common generic patterns
        return norm in {"vvaa", "aavv", "variosautores", "varios", "anonimo", "autorvario", "autoresvarios"}

    @staticmethod
    def build_broad_queries(title: str, author: str, isbn: str) -> List[str]:
        title_clean = title.replace('"', "'").strip()
        isbn_clean = isbn.replace('"', "").replace('-', "").strip()
        
        # Broad queries must never contain generic authors, nor restrictors like reseña/crítica.
        # They start with broad queries like title without quotes, title with quotes, etc.
        queries = []
        
        # Add lowercase variant first/second as requested ("youcat biblia")
        title_lower = title_clean.lower()
        queries.append(title_lower)
        
        # Original case title without quotes
        queries.append(title_clean)
        
        # Title with quotes
        queries.append(f'"{title_clean}"')
        
        words = title_clean.split()
        if len(words) >= 2:
            w1, w2 = words[0], words[1]
            w1_lower, w2_lower = w1.lower(), w2.lower()
            queries.extend([
                f'{w2_lower} {w1_lower}',  # Permuted lowercase
                f'{w2} {w1}',              # Permuted original case
                f'"{w2} {w1}"',
                f'"{w2} de {w1}"',
                f'"la nueva {w2} de {w1}"',
                f'"nueva {w2} {w1}"'
            ])
            
        queries.extend([
            f'{title_clean} noticia',
            f'{title_clean} artículo'
        ])
        
        if isbn_clean:
            queries.append(isbn_clean)
        if isbn and isbn.strip():
            queries.append(isbn.strip())
            
        # Deduplicate preserving order
        seen = set()
        cleaned = []
        for q in queries:
            q_strip = q.strip()
            if q_strip and q_strip not in seen:
                seen.add(q_strip)
                cleaned.append(q_strip)
        return cleaned

    @staticmethod
    def build_queries(title: str, author: str, isbn: str, review_domains: List[str] = None) -> Dict[str, List[str]]:
        # Clean inputs: remove trailing/leading spaces, replace double quotes with single quotes inside text
        title_clean = title.replace('"', "'").strip()
        author_clean = author.replace('"', "'").strip()
        isbn_clean = isbn.replace('"', "").replace('-', "").strip()

        has_author = bool(author_clean) and not QueryBuilder.is_generic_author(author_clean)
        has_isbn = bool(isbn_clean)

        prioritarias = []
        apoyo = []
        dominios = []

        if has_author:
            # Title + Author (+ optional ISBN)
            prioritarias = [
                f'"{title_clean}" "{author_clean}" reseña',
                f'"{title_clean}" "{author_clean}" crítica',
                f'"{title_clean}" "{author_clean}"',
                f'"{title_clean}" "{author_clean}" libro',
                f'"{title_clean}" review',
                f'"{title_clean}" crítica literaria'
            ]
            if has_isbn:
                prioritarias.append(f'"{isbn_clean}"')
                prioritarias.append(f'"{isbn_clean}" "{title_clean}"')

            apoyo = [
                f'"{title_clean}" "{author_clean}" comentario',
                f'"{title_clean}" "{author_clean}" opinión',
                f'"{title_clean}" "{author_clean}" análisis',
                f'"{title_clean}" "{author_clean}" artículo',
                f'"{title_clean}" "{author_clean}" reseña -comprar -amazon -fnac -casadellibro -iberlibro',
                f'"{title_clean}" "{author_clean}" crítica -comprar -amazon -fnac -casadellibro -iberlibro',
                f'"{title_clean}" "{author_clean}" review',
                f'"{title_clean}" recensión'
            ]
            
            if review_domains:
                for domain in review_domains:
                    domain_clean = domain.strip().lower()
                    if domain_clean:
                        dominios.extend([
                            f'site:{domain_clean} "{title_clean}" "{author_clean}"',
                            f'site:{domain_clean} "{title_clean}"',
                            f'site:{domain_clean} "{title_clean}" reseña'
                        ])
        else:
            # Only Title (+ optional ISBN)
            prioritarias = [
                f'"{title_clean}"',
                f'"{title_clean}" reseña libro',
                f'"{title_clean}" crítica libro',
                f'"{title_clean}" ediciones encuentro'
            ]
            if has_isbn:
                prioritarias.append(f'"{isbn_clean}"')
                prioritarias.append(f'"{isbn_clean}" "{title_clean}"')

            # Permuted/compound variants for titles with multiple words (e.g. "YOUCAT Biblia" -> "Biblia YOUCAT")
            words = title_clean.split()
            if len(words) >= 2:
                w1, w2 = words[0], words[1]
                prioritarias.extend([
                    f'"{w2} {w1}"',
                    f'"{w2} de {w1}"',
                    f'"la nueva {w2} de {w1}"'
                ])
                
            apoyo = [
                f'"{title_clean}" comentario libro',
                f'"{title_clean}" reseña',
                f'"{title_clean}" crítica',
                f'"{title_clean}" opinión libro',
                f'"{title_clean}" reseña -comprar -amazon -fnac -casadellibro -iberlibro',
                f'{title_clean} reseña',
                f'{title_clean} artículo',
                f'{title_clean} crítica'
            ]
            
            if review_domains:
                for domain in review_domains:
                    domain_clean = domain.strip().lower()
                    if domain_clean:
                        dominios.extend([
                            f'site:{domain_clean} "{title_clean}" reseña',
                            f'site:{domain_clean} "{title_clean}"'
                        ])

        # Deduplicate preserving order helper
        def clean_list(lst: List[str]) -> List[str]:
            seen = set()
            cleaned = []
            for item in lst:
                item_strip = item.strip()
                if item_strip and item_strip not in seen:
                    seen.add(item_strip)
                    cleaned.append(item_strip)
            return cleaned

        return {
            "prioritarias": clean_list(prioritarias),
            "apoyo": clean_list(apoyo),
            "dominios": clean_list(dominios)
        }

query_builder = QueryBuilder()
