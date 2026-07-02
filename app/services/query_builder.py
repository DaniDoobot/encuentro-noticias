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
    def clean_punctuation(text: str) -> str:
        # Replaces common punctuation with space, then reduces multiple spaces
        cleaned = re.sub(r'[.,;:!\?¿¡\(\)\[\]"\'«»“”]', ' ', text)
        return re.sub(r'\s+', ' ', cleaned).strip()

    @staticmethod
    def remove_volume(text: str) -> str:
        # Matches patterns like "Vol. I", "Vol I", "Vol. 1", "Vol 1", "Tomo I", "Tomo 1" (case-insensitive)
        pattern = r'\s+(vol\.?|tomo\.?|v\.?|t\.?)\s+(i+x*|v*i*|x*|v*|\d+)\b'
        cleaned = re.sub(pattern, '', text, flags=re.IGNORECASE)
        return cleaned.strip().rstrip(',.-:; ')

    @staticmethod
    def clean_tokens_punctuation(query: str) -> str:
        if '"' in query:
            return query
        # Cleans punctuation at the end of individual words/tokens in unquoted query
        words = query.split()
        cleaned_words = [w.rstrip(',.-:;!?¿¡') for w in words]
        return " ".join([w for w in cleaned_words if w])

    @staticmethod
    def get_author_spelling_variations(author: str) -> List[str]:
        norm = author.strip()
        if not norm:
            return []
        variations = [norm]
        lower = norm.lower()
        # Check for Alexéi Navalni variations
        if "navalni" in lower or "navalny" in lower:
            variations.extend([
                "Alexéi Navalni",
                "Alexei Navalny",
                "Alexei Navalni",
                "Alexéi Navalny"
            ])
        # Deduplicate preserving order
        seen = set()
        cleaned = []
        for v in variations:
            if v not in seen:
                seen.add(v)
                cleaned.append(v)
        return cleaned

    @staticmethod
    def is_safe_broad_query(title: str, query: str, author: str) -> bool:
        # 1. Always safe if the query contains any spelling variations of the author
        author_vars = QueryBuilder.get_author_spelling_variations(author)
        query_lower = query.lower()
        for av in author_vars:
            if av.lower() in query_lower:
                return True

        # 2. Always safe if it contains a valid ISBN
        cleaned_digits = re.sub(r'[^0-9]', '', query)
        if len(cleaned_digits) >= 9:
            return True

        # 3. Always unsafe if the title contains a comma (and has no author)
        if "," in title:
            return False

        # 4. Normalize title and generic phrases to strip accents for comparison
        def strip_accents(text: str) -> str:
            return "".join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')

        title_flat = strip_accents(QueryBuilder.clean_punctuation(title)).lower()

        generic_phrases = {
            "no tengo miedo no lo tengais vosotros",
            "vivir en el asombro",
            "el sacrificio",
            "el sentido religioso",
            "europa la via romana",
            "cristina hija de lavrans"
        }

        # If the title contains or is one of the generic phrases
        for gp in generic_phrases:
            if gp in title_flat:
                return False

        # 5. Check if the query itself is composed only of generic/unsafe words
        query_flat = strip_accents(QueryBuilder.clean_punctuation(query)).lower()
        for gp in generic_phrases:
            if gp in query_flat:
                return False

        # 6. Must contain at least 3 significant tokens non-generic
        stop_words = {"del", "las", "los", "con", "para", "por", "una", "unos", "unas", "como", "de", "el", "la", "en", "y", "o"}
        cleaned_q = re.sub(r'[^a-z0-9\s]', ' ', strip_accents(query_lower))
        tokens = [t for t in cleaned_q.split() if len(t) >= 3 and t not in stop_words]

        if len(tokens) < 3:
            return False

        return True

    @staticmethod
    def is_query_allowed(query: str, title: str, author: str) -> bool:
        query_lower = query.lower()
        
        # Always allow if it contains the exact author name (any of spelling variations)
        # Check against author variations
        author_vars = QueryBuilder.get_author_spelling_variations(author)
        for av in author_vars:
            if av.lower() in query_lower:
                return True

        # Always allow ISBN queries (numeric digits string of length >= 9)
        cleaned_digits = re.sub(r'[^0-9]', '', query)
        if len(cleaned_digits) >= 9:
            return True

        # Always allow if it contains the exact full title enclosed in quotes
        title_clean = title.strip().lower()
        if f'"{title_clean}"' in query_lower:
            return True
            
        # Allow if the query is a permuted word query of the title's first two words,
        # but ONLY if the title does not contain a comma (Condition 5)
        if "," not in title:
            title_words = title.strip().lower().split()
            if len(title_words) >= 2:
                w1 = re.sub(r'[^a-z0-9áéíóúñü]', '', title_words[0])
                w2 = re.sub(r'[^a-z0-9áéíóúñü]', '', title_words[1])
                if w1 and w2:
                    q_clean = re.sub(r'[^a-z0-9áéíóúñü\s]', ' ', query_lower)
                    q_words = q_clean.split()
                    if w1 in q_words and w2 in q_words:
                        return True

        # Count significant tokens (alphanumeric, len >= 3, excluding common stop words)
        stop_words = {"del", "las", "los", "con", "para", "por", "una", "unos", "unas", "como", "los", "las"}
        # Remove quotes/punctuation
        cleaned_q = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑüÜ\s]', ' ', query_lower)
        tokens = [t for t in cleaned_q.split() if len(t) >= 3 and t not in stop_words]
        
        if len(tokens) < 3:
            return False
            
        # Check for combinations composed ONLY of generic words/common names without author
        generic_combinations = {
            ("hija", "cristina"), ("cristina", "hija"),
            ("no", "tengo", "miedo"), ("no", "tengais", "vosotros"),
            ("no", "lo", "tengais", "vosotros")
        }
        tokens_set = set(tokens)
        for gen in generic_combinations:
            if all(g in tokens_set for g in gen):
                return False
                
        return True

    @staticmethod
    def build_broad_queries(title: str, author: str, isbn: str) -> List[str]:
        title_clean = title.replace('"', "'").strip()
        isbn_clean = isbn.replace('"', "").replace('-', "").strip()
        
        queries = []
        
        # Basic full title variations
        title_lower = title_clean.lower()
        queries.append(title_lower)
        queries.append(title_clean)
        queries.append(f'"{title_clean}"')

        # Clean punctuation variant
        title_no_punct = QueryBuilder.clean_punctuation(title_clean)
        if title_no_punct != title_clean:
            queries.append(title_no_punct.lower())
            queries.append(title_no_punct)
            queries.append(f'"{title_no_punct}"')

        # Volume removed variant
        title_no_vol = QueryBuilder.remove_volume(title_clean)
        if title_no_vol != title_clean:
            queries.append(title_no_vol.lower())
            queries.append(title_no_vol)
            queries.append(f'"{title_no_vol}"')
            
            title_no_vol_punct = QueryBuilder.clean_punctuation(title_no_vol)
            if title_no_vol_punct != title_no_vol:
                queries.append(title_no_vol_punct.lower())
                queries.append(title_no_vol_punct)
                queries.append(f'"{title_no_vol_punct}"')

        # Skip first two words permutation if title contains a comma to prevent bad fragments (Condition 5)
        if "," not in title_clean:
            words = title_clean.split()
            if len(words) >= 2:
                w1 = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑüÜ]', '', words[0])
                w2 = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑüÜ]', '', words[1])
                if w1 and w2:
                    w1_lower, w2_lower = w1.lower(), w2.lower()
                    queries.extend([
                        f'{w2_lower} {w1_lower}',
                        f'{w2} {w1}',
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

        # Clean punctuation at end of tokens and filter allowed queries
        final_queries = []
        seen = set()
        for q in queries:
            q_clean = QueryBuilder.clean_tokens_punctuation(q)
            q_strip = q_clean.strip()
            if q_strip and q_strip not in seen:
                if QueryBuilder.is_query_allowed(q_strip, title, author):
                    author_vars = QueryBuilder.get_author_spelling_variations(author)
                    has_auth_var = any(av.lower() in q_strip.lower() for av in author_vars)
                    if not has_auth_var and not QueryBuilder.is_safe_broad_query(title, q_strip, author):
                        continue
                    seen.add(q_strip)
                    final_queries.append(q_strip)
        return final_queries

    @staticmethod
    def build_queries(title: str, author: str, isbn: str, review_domains: List[str] = None) -> Dict[str, List[str]]:
        title_clean = title.replace('"', "'").strip()
        author_clean = author.replace('"', "'").strip()
        isbn_clean = isbn.replace('"', "").replace('-', "").strip()

        has_author = bool(author_clean) and not QueryBuilder.is_generic_author(author_clean)
        has_isbn = bool(isbn_clean)

        prioritarias = []
        apoyo = []
        dominios = []

        # Spelling variations for author
        authors = QueryBuilder.get_author_spelling_variations(author_clean) if has_author else [author_clean]

        # Base combinations
        title_variants = [title_clean]
        
        # Punctuation cleaned variation
        title_no_punct = QueryBuilder.clean_punctuation(title_clean)
        if title_no_punct != title_clean:
            title_variants.append(title_no_punct)

        # Volume removed variations
        title_no_vol = QueryBuilder.remove_volume(title_clean)
        if title_no_vol != title_clean:
            title_variants.append(title_no_vol)
            title_no_vol_punct = QueryBuilder.clean_punctuation(title_no_vol)
            if title_no_vol_punct != title_no_vol:
                title_variants.append(title_no_vol_punct)

        # First part before first comma variation
        if "," in title_clean:
            parts = [p.strip() for p in title_clean.split(",", 1)]
            if parts and parts[0]:
                title_variants.append(parts[0])

        # International variants for Kristin Lavransdatter / Sigrid Undset
        if "lavrans" in title_clean.lower() and "undset" in author_clean.lower():
            title_variants.extend(["Kristin Lavransdatter", "Kristin Lavransdotter"])

        # Build prioritarias & apoyo for all title/author combinations
        if has_author:
            for t_var in title_variants:
                for a_var in authors:
                    prioritarias.extend([
                        f'"{t_var}" "{a_var}" reseña',
                        f'"{t_var}" "{a_var}" crítica',
                        f'"{t_var}" "{a_var}"',
                        f'"{t_var}" "{a_var}" libro'
                    ])
                    apoyo.extend([
                        f'"{t_var}" "{a_var}" comentario',
                        f'"{t_var}" "{a_var}" opinión',
                        f'"{t_var}" "{a_var}" análisis',
                        f'"{t_var}" "{a_var}" artículo',
                        f'"{t_var}" "{a_var}" reseña -comprar -amazon -fnac -casadellibro -iberlibro',
                        f'"{t_var}" "{a_var}" crítica -comprar -amazon -fnac -casadellibro -iberlibro',
                        f'"{t_var}" "{a_var}" review'
                    ])
            # General author independent variants in prioritarias / apoyo
            prioritarias.extend([
                f'"{title_clean}" review',
                f'"{title_clean}" crítica literaria'
            ])
            apoyo.append(f'"{title_clean}" recensión')
            if has_isbn:
                prioritarias.append(f'"{isbn_clean}"')
                prioritarias.append(f'"{isbn_clean}" "{title_clean}"')

            if review_domains:
                for domain in review_domains:
                    domain_clean = domain.strip().lower()
                    if domain_clean:
                        for t_var in title_variants:
                            for a_var in authors:
                                dominios.append(f'site:{domain_clean} "{t_var}" "{a_var}"')
                            dominios.append(f'site:{domain_clean} "{t_var}"')
                            dominios.append(f'site:{domain_clean} "{t_var}" reseña')
        else:
            # Only Title combinations
            for t_var in title_variants:
                prioritarias.extend([
                    f'"{t_var}"',
                    f'"{t_var}" reseña libro',
                    f'"{t_var}" crítica libro',
                    f'"{t_var}" ediciones encuentro'
                ])
                apoyo.extend([
                    f'"{t_var}" comentario libro',
                    f'"{t_var}" reseña',
                    f'"{t_var}" crítica',
                    f'"{t_var}" opinión libro',
                    f'"{t_var}" reseña -comprar -amazon -fnac -casadellibro -iberlibro',
                    f'{t_var} reseña',
                    f'{t_var} artículo',
                    f'{t_var} crítica'
                ])

            if has_isbn:
                prioritarias.append(f'"{isbn_clean}"')
                prioritarias.append(f'"{isbn_clean}" "{title_clean}"')

            if "," not in title_clean:
                words = title_clean.split()
                if len(words) >= 2:
                    w1 = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑüÜ]', '', words[0])
                    w2 = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑüÜ]', '', words[1])
                    if w1 and w2:
                        prioritarias.extend([
                            f'"{w2} {w1}"',
                            f'"{w2} de {w1}"',
                            f'"la nueva {w2} de {w1}"'
                        ])
            
            if review_domains:
                for domain in review_domains:
                    domain_clean = domain.strip().lower()
                    if domain_clean:
                        for t_var in title_variants:
                            dominios.extend([
                                f'site:{domain_clean} "{t_var}" reseña',
                                f'site:{domain_clean} "{t_var}"'
                            ])

        # Deduplicate, clean token punctuation and filter by allowed rules
        def clean_list(lst: List[str]) -> List[str]:
            seen = set()
            cleaned = []
            for item in lst:
                item_clean = QueryBuilder.clean_tokens_punctuation(item)
                item_strip = item_clean.strip()
                if item_strip and item_strip not in seen:
                    if QueryBuilder.is_query_allowed(item_strip, title, author):
                        seen.add(item_strip)
                        cleaned.append(item_strip)
            return cleaned

        return {
            "prioritarias": clean_list(prioritarias),
            "apoyo": clean_list(apoyo),
            "dominios": clean_list(dominios)
        }

query_builder = QueryBuilder()
