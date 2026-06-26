import urllib.parse
from typing import List

class QueryBuilder:
    @staticmethod
    def build_queries(title: str, author: str, isbn: str, review_domains: List[str] = None) -> List[str]:
        # Clean inputs: remove trailing/leading spaces, replace double quotes with single quotes inside text
        title_clean = title.replace('"', "'").strip()
        author_clean = author.replace('"', "'").strip()
        isbn_clean = isbn.replace('"', "").replace('-', "").strip()

        # Base organic queries
        queries = [
            # Exact title + author search
            f'"{title_clean}" "{author_clean}"',
            
            # Spanish review terms
            f'"{title_clean}" "{author_clean}" reseña',
            f'"{title_clean}" "{author_clean}" crítica',
            f'"{title_clean}" "{author_clean}" comentario',
            f'"{title_clean}" "{author_clean}" opinión',
            f'"{title_clean}" "{author_clean}" análisis',
            f'"{title_clean}" "{author_clean}" artículo',
            f'"{title_clean}" "{author_clean}" entrevista',
            f'"{title_clean}" "{author_clean}" libro',
            
            # Negatives to bypass commercial sites in some queries
            f'"{title_clean}" "{author_clean}" reseña -comprar -amazon -fnac -casadellibro -iberlibro',
            f'"{title_clean}" "{author_clean}" crítica -comprar -amazon -fnac -casadellibro -iberlibro',
            f'"{title_clean}" "{author_clean}" comentario -comprar -amazon -fnac -casadellibro -iberlibro',
            
            # International terms
            f'"{title_clean}" "{author_clean}" review',
            f'"{title_clean}" "{author_clean}" critique',
            f'"{title_clean}" "{author_clean}" recensione',
            f'"{title_clean}" "{author_clean}" recension',
            f'"{title_clean}" "{author_clean}" crítica literaria',
            
            # Simple title searches
            f'"{title_clean}" review',
            f'"{title_clean}" recensión',
        ]

        if isbn_clean:
            queries.extend([
                f'"{isbn_clean}" "{title_clean}"',
                f'"{isbn_clean}" reseña',
                f'"{isbn_clean}" crítica'
            ])

        # Add site specific queries if domains are configured
        if review_domains:
            for domain in review_domains:
                domain_clean = domain.strip().lower()
                if domain_clean:
                    queries.extend([
                        f'site:{domain_clean} "{title_clean}" "{author_clean}"',
                        f'site:{domain_clean} "{title_clean}"',
                        f'site:{domain_clean} "{title_clean}" reseña'
                    ])

        # Deduplicate list preserving order
        seen = set()
        unique_queries = []
        for q in queries:
            q_strip = q.strip()
            if q_strip not in seen:
                seen.add(q_strip)
                unique_queries.append(q_strip)

        return unique_queries

query_builder = QueryBuilder()
