import urllib.parse
from typing import List, Dict

class QueryBuilder:
    @staticmethod
    def build_queries(title: str, author: str, isbn: str, review_domains: List[str] = None) -> Dict[str, List[str]]:
        # Clean inputs: remove trailing/leading spaces, replace double quotes with single quotes inside text
        title_clean = title.replace('"', "'").strip()
        author_clean = author.replace('"', "'").strip()
        isbn_clean = isbn.replace('"', "").replace('-', "").strip()

        # Tier 1 - Prioritarias (Nivel 1)
        prioritarias = [
            f'"{title_clean}" "{author_clean}"',
            f'"{title_clean}" "{author_clean}" reseña',
            f'"{title_clean}" "{author_clean}" crítica',
            f'"{title_clean}" "{author_clean}" libro',
            f'"{title_clean}" review',
            f'"{title_clean}" crítica literaria',
            f'"{title_clean}" entrevista'
        ]
        
        if isbn_clean:
            prioritarias.extend([
                f'"{isbn_clean}" "{title_clean}"',
                f'"{isbn_clean}" reseña',
                f'"{isbn_clean}" crítica'
            ])

        # Tier 2 - Apoyo (Nivel 2)
        apoyo = [
            f'"{title_clean}" "{author_clean}" comentario',
            f'"{title_clean}" "{author_clean}" opinión',
            f'"{title_clean}" "{author_clean}" análisis',
            f'"{title_clean}" "{author_clean}" artículo',
            
            # Negatives to bypass commercial sites in some queries
            f'"{title_clean}" "{author_clean}" reseña -comprar -amazon -fnac -casadellibro -iberlibro',
            f'"{title_clean}" "{author_clean}" crítica -comprar -amazon -fnac -casadellibro -iberlibro',
            f'"{title_clean}" "{author_clean}" comentario -comprar -amazon -fnac -casadellibro -iberlibro',
            
            # International terms
            f'"{title_clean}" "{author_clean}" review',
            f'"{title_clean}" "{author_clean}" critique',
            f'"{title_clean}" "{author_clean}" recensione',
            f'"{title_clean}" "{author_clean}" recension',
            f'"{title_clean}" recensión'
        ]

        # Tier 3 - Dominios (Nivel 3)
        dominios = []
        if review_domains:
            for domain in review_domains:
                domain_clean = domain.strip().lower()
                if domain_clean:
                    dominios.extend([
                        f'site:{domain_clean} "{title_clean}" "{author_clean}"',
                        f'site:{domain_clean} "{title_clean}"',
                        f'site:{domain_clean} "{title_clean}" reseña'
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
