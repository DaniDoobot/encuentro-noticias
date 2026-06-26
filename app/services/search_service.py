import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from app.config import settings
import time
import logging

logger = logging.getLogger("encuentro-noticias")

class SearchService:
    def __init__(self):
        self.base_url = "https://html.duckduckgo.com/html/"
        self.headers = {
            "User-Agent": settings.SCRAPER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3"
        }

    def _extract_real_url(self, href: str) -> str:
        """
        Extracts the destination URL from DuckDuckGo's redirection link format.
        DuckDuckGo HTML link: /l/?uddg=https%3A%2F%2Fwww.example.com%2Fpath&rut=...
        """
        if not href:
            return ""
        
        # If it's a redirect link
        if "uddg=" in href:
            try:
                parsed = urlparse(href)
                qs = parse_qs(parsed.query)
                if "uddg" in qs:
                    return unquote(qs["uddg"][0])
            except Exception as e:
                logger.debug(f"Failed to parse DDG redirect URL {href}: {e}")
        
        # If it is a relative url starting with //
        if href.startswith("//"):
            return "https:" + href
            
        # Absolute URL
        if href.startswith("http://") or href.startswith("https://"):
            return href
            
        return href

    def search(self, query: str, max_pages: int = 1) -> List[str]:
        """
        Searches for a query on DuckDuckGo HTML and retrieves candidate URLs.
        Follows pagination forms to retrieve up to max_pages of results.
        """
        urls = []
        client = httpx.Client(timeout=settings.REQUEST_TIMEOUT_SECONDS)
        
        try:
            # First Page is a GET request
            params = {"q": query}
            logger.info(f"Searching: {query} (GET page 1)")
            response = client.get(self.base_url, params=params, headers=self.headers)
            
            if response.status_code != 200:
                logger.error(f"DuckDuckGo search error {response.status_code} for query: {query}")
                return []

            page_num = 1
            while True:
                soup = BeautifulSoup(response.text, "html.parser")
                
                # Extract all results
                results_found_on_page = 0
                for a_tag in soup.find_all("a", class_="result__a"):
                    href = a_tag.get("href")
                    real_url = self._extract_real_url(href)
                    if real_url and (real_url.startswith("http://") or real_url.startswith("https://")):
                        # Avoid adding duckduckgo itself or system links
                        if "duckduckgo.com" not in real_url:
                            urls.append(real_url)
                            results_found_on_page += 1

                logger.debug(f"Page {page_num}: Found {results_found_on_page} URLs")

                if page_num >= max_pages:
                    break

                # Look for the pagination form
                # DuckDuckGo HTML uses a form with submit button 'Next' for pagination
                form = soup.find("form", action="/html/")
                if not form:
                    # No more pages
                    break

                # Gather form input elements
                form_data = {}
                for input_tag in form.find_all("input"):
                    name = input_tag.get("name")
                    val = input_tag.get("value")
                    if name:
                        form_data[name] = val

                # Submit POST for next page
                page_num += 1
                logger.info(f"Searching: {query} (POST page {page_num})")
                time.sleep(1.0) # Polite delay
                
                post_url = urljoin(self.base_url, form.get("action", "/html/"))
                response = client.post(post_url, data=form_data, headers=self.headers)
                
                if response.status_code != 200:
                    logger.warning(f"Failed to fetch page {page_num} for query: {query}. Code: {response.status_code}")
                    break

        except Exception as e:
            logger.error(f"Search exception for query '{query}': {e}", exc_info=True)
        finally:
            client.close()

        # Deduplicate preserving order
        seen = set()
        unique_urls = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique_urls.append(u)

        return unique_urls

search_service = SearchService()
