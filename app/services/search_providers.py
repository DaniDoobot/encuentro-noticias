import httpx
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs, unquote, quote
from typing import List
import logging

logger = logging.getLogger("encuentro-noticias")

class SearchProviderError(Exception):
    def __init__(self, provider_name: str, message: str, status_code: int = None):
        self.provider_name = provider_name
        self.message = message
        self.status_code = status_code
        super().__init__(f"[{provider_name}] {message} (status: {status_code})")

class SearchProviderRateLimitError(SearchProviderError):
    pass

class SearchProviderHttpError(SearchProviderError):
    pass

class SearchProviderConnectionError(SearchProviderError):
    pass


class SearchProvider:
    def name(self) -> str:
        raise NotImplementedError

    def search(self, query: str, max_pages: int = 1, timeout: int = 20) -> List[str]:
        raise NotImplementedError


class DuckDuckGoSearchProvider(SearchProvider):
    def name(self) -> str:
        return "DuckDuckGo"

    def _extract_real_url(self, href: str) -> str:
        if not href:
            return ""
        if "uddg=" in href:
            try:
                parsed = urlparse(href)
                qs = parse_qs(parsed.query)
                if "uddg" in qs:
                    return unquote(qs["uddg"][0])
            except Exception:
                pass
        if href.startswith("//"):
            return "https:" + href
        return href

    def search(self, query: str, max_pages: int = 1, timeout: int = 20) -> List[str]:
        base_url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
        }
        urls = []
        
        try:
            client = httpx.Client(timeout=timeout)
            response = client.get(base_url, params={"q": query}, headers=headers)
            
            # Rate limit or block detection
            if response.status_code in (202, 403, 429):
                raise SearchProviderRateLimitError(self.name(), "DuckDuckGo has blocked/rate-limited the request.", response.status_code)
            elif response.status_code != 200:
                raise SearchProviderHttpError(self.name(), f"HTTP error code", response.status_code)

            soup = BeautifulSoup(response.text, "html.parser")
            for a_tag in soup.find_all("a", class_="result__a"):
                href = a_tag.get("href")
                real_url = self._extract_real_url(href)
                if real_url and (real_url.startswith("http://") or real_url.startswith("https://")):
                    if "duckduckgo.com" not in real_url:
                        urls.append(real_url)
            
            client.close()
        except SearchProviderError:
            raise
        except Exception as e:
            raise SearchProviderConnectionError(self.name(), f"Connection failed: {str(e)}")

        return urls


class BingHtmlSearchProvider(SearchProvider):
    def name(self) -> str:
        return "BingHtml"

    def search(self, query: str, max_pages: int = 1, timeout: int = 20) -> List[str]:
        base_url = "https://www.bing.com/search"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Referer": "https://www.bing.com/"
        }
        urls = []
        
        try:
            client = httpx.Client(timeout=timeout)
            response = client.get(base_url, params={"q": query}, headers=headers)
            
            if response.status_code in (403, 429):
                raise SearchProviderRateLimitError(self.name(), "Bing has blocked/rate-limited the request.", response.status_code)
            elif response.status_code != 200:
                raise SearchProviderHttpError(self.name(), "HTTP error code", response.status_code)

            soup = BeautifulSoup(response.text, "html.parser")
            
            # Bing search result elements are li with class 'b_algo'
            for li in soup.find_all("li", class_="b_algo"):
                h2 = li.find("h2")
                if h2:
                    a_tag = h2.find("a")
                    if a_tag:
                        href = a_tag.get("href")
                        if href and (href.startswith("http://") or href.startswith("https://")):
                            # Filter out internal/commercial tracking links of Microsoft
                            parsed = urlparse(href)
                            domain = parsed.netloc.lower()
                            if not any(d in domain for d in ["bing.com", "microsoft.com", "msn.com", "live.com"]):
                                urls.append(href)
                                
            client.close()
        except SearchProviderError:
            raise
        except Exception as e:
            raise SearchProviderConnectionError(self.name(), f"Connection failed: {str(e)}")

        return urls


class GoogleNewsRssSearchProvider(SearchProvider):
    def name(self) -> str:
        return "GoogleNewsRss"

    def search(self, query: str, max_pages: int = 1, timeout: int = 20) -> List[str]:
        # URL encode query properly
        encoded_query = quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=es&gl=ES&ceid=ES:es"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/xml,text/xml,*/*;q=0.8"
        }
        urls = []
        
        try:
            client = httpx.Client(timeout=timeout)
            response = client.get(url, headers=headers)
            
            if response.status_code in (403, 429):
                raise SearchProviderRateLimitError(self.name(), "Google News RSS blocked/rate-limited the request.", response.status_code)
            elif response.status_code != 200:
                raise SearchProviderHttpError(self.name(), "HTTP error code", response.status_code)

            # Parse XML
            root = ET.fromstring(response.content)
            for item in root.findall(".//item"):
                link = item.find("link")
                if link is not None and link.text:
                    link_text = link.text.strip()
                    if link_text.startswith("http://") or link_text.startswith("https://"):
                        urls.append(link_text)
                        
            client.close()
        except SearchProviderError:
            raise
        except Exception as e:
            raise SearchProviderConnectionError(self.name(), f"Connection failed: {str(e)}")

        return urls
