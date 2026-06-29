import urllib.parse
from typing import List, Dict

class QueryBuilder:
    @staticmethod
    def build_queries(title: str, author: str, isbn: str, review_domains: List[str] = None) -> Dict[str, List[str]]:
        # Clean inputs: remove trailing/leading spaces, replace double quotes with single quotes inside text
        title_clean = title.replace('"', "'").strip()
        author_clean = author.replace('"', "'").strip()
        isbn_clean = isbn.replace('"', "").replace('-', "").strip()

        has_author = bool(author_clean)
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
                f'"{title_clean}" reseña libro',
                f'"{title_clean}" crítica libro',
                f'"{title_clean}" ediciones encuentro'
            ]
            if has_isbn:
                prioritarias.append(f'"{isbn_clean}"')
                prioritarias.append(f'"{isbn_clean}" "{title_clean}"')
                
            apoyo = [
                f'"{title_clean}" comentario libro',
                f'"{title_clean}" reseña',
                f'"{title_clean}" crítica',
                f'"{title_clean}" opinión libro',
                f'"{title_clean}" reseña -comprar -amazon -fnac -casadellibro -iberlibro'
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
