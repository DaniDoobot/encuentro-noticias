import urllib.parse
from typing import List

class QueryBuilder:
    @staticmethod
    def build_queries(title: str, author: str, isbn: str) -> List[str]:
        # Clean inputs: remove trailing/leading spaces, replace double quotes with single quotes inside text
        title_clean = title.replace('"', "'").strip()
        author_clean = author.replace('"', "'").strip()
        isbn_clean = isbn.replace('"', "").replace('-', "").strip()

        queries = [
            f'"{title_clean}" "{author_clean}"',
            f'"{title_clean}" "{author_clean}" reseña',
            f'"{title_clean}" "{author_clean}" crítica',
            f'"{title_clean}" "{author_clean}" libro',
            f'"{title_clean}" review',
            f'"{title_clean}" recension',
        ]

        if isbn_clean:
            queries.append(f'"{isbn_clean}" "{title_clean}"')

        # International and specialized queries
        queries.extend([
            f'"{title_clean}" "{author_clean}" review',
            f'"{title_clean}" "{author_clean}" critique',
            f'"{title_clean}" "{author_clean}" recensione',
            f'"{title_clean}" "{author_clean}" crítica literaria'
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
