import logging
import re
import html
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, quote
from typing import List, Dict, Any, Optional

from app.config import settings
from app.services.domain_indexer import _is_cultural_url, _normalize_url
from app.services.source_discovery import _filter_stopwords

logger = logging.getLogger("encuentro-noticias")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
}

def clean_html(html_str: str) -> str:
    if not html_str:
        return ""
    # Strip HTML tags
    clean = re.sub(r"<[^>]+>", "", html_str)
    # Decode HTML entities
    clean = html.unescape(clean)
    return clean.strip()

def generate_internal_queries(title: str, author: str, isbn: str) -> List[str]:
    queries = []
    
    # 1. "título completo"
    if title:
        queries.append(f'"{title}"')
        
    # 2. "título completo" "autor"
    if title and author:
        queries.append(f'"{title}" "{author}"')
        
    # 3. ISBN sin guiones
    if isbn:
        clean_isbn = isbn.replace("-", "").strip()
        if clean_isbn:
            queries.append(clean_isbn)
            
    # 4. autor + palabra significativa del título
    if author and title:
        title_terms = _filter_stopwords(title).split()
        # Find a long/significant word in the title terms
        sig_words = [t for t in title_terms if len(t) > 3]
        if sig_words:
            # Sort by length descending to get the most specific word
            sig_words.sort(key=len, reverse=True)
            queries.append(f"{author} {sig_words[0]}")
            
    # Limit queries list to unique and maximum of 4
    seen = set()
    unique_queries = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique_queries.append(q)
            
    return unique_queries[:4]

class InternalDomainSearchProvider:
    
    def search_wordpress_api(self, domain: str, query: str, timeout: int = 5) -> List[Dict[str, Any]]:
        results = []
        # Try endpoint 1: search
        url1 = f"https://{domain}/wp-json/wp/v2/search?search={quote(query)}"
        r = httpx.get(url1, headers=HEADERS, timeout=timeout, follow_redirects=True)
        if r.status_code in (401, 403, 404):
            r.raise_for_status()
            
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                for item in data:
                    item_url = item.get("url") or item.get("link")
                    if not item_url:
                        continue
                    title_obj = item.get("title")
                    title = title_obj.get("rendered") if isinstance(title_obj, dict) else title_obj
                    if not title:
                        title = ""
                    snippet_obj = item.get("excerpt") or item.get("description")
                    snippet = snippet_obj.get("rendered") if isinstance(snippet_obj, dict) else snippet_obj
                    if not snippet:
                        snippet = ""
                    results.append({
                        "url": item_url,
                        "title": title,
                        "snippet": snippet,
                        "provider": "wordpress_rest",
                        "status": "ok",
                        "error": ""
                    })

        # If endpoint 1 returned 0 results, try endpoint 2: posts
        if not results:
            url2 = f"https://{domain}/wp-json/wp/v2/posts?search={quote(query)}"
            r = httpx.get(url2, headers=HEADERS, timeout=timeout, follow_redirects=True)
            if r.status_code in (401, 403, 404):
                r.raise_for_status()
                
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    for item in data:
                        item_url = item.get("link") or item.get("url")
                        if not item_url:
                            continue
                        title_obj = item.get("title")
                        title = title_obj.get("rendered") if isinstance(title_obj, dict) else title_obj
                        if not title:
                            title = ""
                        excerpt_obj = item.get("excerpt")
                        snippet = excerpt_obj.get("rendered") if isinstance(excerpt_obj, dict) else excerpt_obj
                        if not snippet:
                            snippet = ""
                        results.append({
                            "url": item_url,
                            "title": title,
                            "snippet": snippet,
                            "provider": "wordpress_rest",
                            "status": "ok",
                            "error": ""
                        })
                
        # Clean HTML from snippets and titles
        for res in results:
            res["title"] = clean_html(res["title"])
            res["snippet"] = clean_html(res["snippet"])
            
        return results

    def search_html(self, domain: str, query: str, timeout: int = 5) -> List[Dict[str, Any]]:
        url = f"https://{domain}/?s={quote(query)}"
        results = []
        r = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
            
        soup = BeautifulSoup(r.text, "html.parser")
        # Extract terms of the query for relevance check
        query_terms = [t.lower() for t in _filter_stopwords(query).split() if len(t) > 2]
        
        a_tags = soup.find_all("a", href=True)
        seen_urls = set()
        
        for a in a_tags:
            href = a["href"]
            resolved_url = urljoin(url, href)
            
            # Filter domain
            if urlparse(resolved_url).netloc != domain:
                continue
            resolved_url = _normalize_url(resolved_url)
            
            # Exclude root/empty path
            if urlparse(resolved_url).path in ("", "/"):
                continue
                
            # Exclude search/tags/legal
            if not _is_cultural_url(resolved_url):
                continue
                
            # Check anchor text
            anchor = a.get_text(strip=True)
            if len(anchor) < 6:
                continue
                
            # Check if anchor or URL contains query terms
            anchor_lower = anchor.lower()
            url_lower = resolved_url.lower()
            if query_terms and not any(t in anchor_lower or t in url_lower for t in query_terms):
                continue
                
            if resolved_url in seen_urls:
                continue
            seen_urls.add(resolved_url)
            
            # Find snippet
            snippet = ""
            parent = a.find_parent(["article", "div", "li", "section"])
            if parent:
                p_tag = parent.find("p")
                if p_tag:
                    snippet = p_tag.get_text(strip=True)
                else:
                    snippet = parent.get_text(strip=True)
                    if snippet.startswith(anchor):
                        snippet = snippet[len(anchor):].strip()
                        
            # Cap snippet length
            snippet = snippet[:300]
            
            results.append({
                "url": resolved_url,
                "title": anchor.capitalize(),
                "snippet": snippet,
                "provider": "html_search",
                "status": "ok",
                "error": ""
            })
            
        return results

    def search_domain_for_book(
        self,
        domain: str,
        title: str,
        author: str,
        isbn: str,
        config: Dict[str, Any] = None
    ) -> List[Dict[str, Any]]:
        if not config:
            config = {}
            
        timeout = int(config.get("INTERNAL_SEARCH_TIMEOUT_SECONDS", settings.INTERNAL_SEARCH_TIMEOUT_SECONDS))
        max_results = int(config.get("INTERNAL_SEARCH_MAX_RESULTS_PER_DOMAIN", settings.INTERNAL_SEARCH_MAX_RESULTS_PER_DOMAIN))
        
        queries = generate_internal_queries(title, author, isbn)
        domain_results = []
        seen_urls = set()
        
        wp_supported = True
        html_supported = True
        
        for q in queries:
            res = []
            if wp_supported:
                try:
                    res = self.search_wordpress_api(domain, q, timeout=timeout)
                except httpx.HTTPStatusError as e:
                    # If REST API returns 401, 403, 404, etc., it is unsupported on this domain
                    if e.response.status_code in (401, 403, 404):
                        wp_supported = False
                    logger.debug(f"WP REST API unsupported for {domain}: {e}")
                except Exception as e:
                    # Connection error or timeout -> disable for this domain
                    wp_supported = False
                    logger.debug(f"WP REST API failed for {domain}: {e}")
                    
            if not res and html_supported:
                try:
                    res = self.search_html(domain, q, timeout=timeout)
                except Exception as e:
                    # Connection error or timeout -> disable HTML search for this domain
                    html_supported = False
                    logger.debug(f"HTML search failed for {domain}: {e}")
                    
            for item in res:
                url = item["url"]
                if url not in seen_urls:
                    seen_urls.add(url)
                    item["query"] = q
                    item["domain"] = domain
                    domain_results.append(item)
                    
            if len(domain_results) >= max_results:
                break
                
        return domain_results[:max_results]

internal_search_provider = InternalDomainSearchProvider()
