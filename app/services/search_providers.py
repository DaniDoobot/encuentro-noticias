import httpx
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs, unquote, quote
from typing import List, Dict, Any, Tuple, Optional
import logging
import base64
from pydantic import BaseModel
from app.config import settings

logger = logging.getLogger("encuentro-noticias")

class SearchProviderError(Exception):
    def __init__(self, provider_name: str, message: str, status_code: int = None, debug_dict: Dict[str, Any] = None):
        self.provider_name = provider_name
        self.message = message
        self.status_code = status_code
        self.debug_dict = debug_dict or {}
        super().__init__(f"[{provider_name}] {message} (status: {status_code})")

class SearchProviderRateLimitError(SearchProviderError):
    pass

class SearchProviderHttpError(SearchProviderError):
    pass

class SearchProviderConnectionError(SearchProviderError):
    pass


class SearchProviderResult(BaseModel):
    provider: str
    query: str
    status: str  # "ok", "rate_limited", "error"
    status_code: Optional[int] = None
    urls: List[str]
    debug: Dict[str, Any] = {}


class SearchProvider:
    def name(self) -> str:
        raise NotImplementedError

    def search(self, query: str, max_pages: int = 1, timeout: int = 20, **kwargs) -> SearchProviderResult:
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

    def search(self, query: str, max_pages: int = 1, timeout: int = 20, **kwargs) -> SearchProviderResult:
        base_url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
        }
        urls = []
        debug_dict = {
            "provider": self.name(),
            "query": query,
            "status_code": None,
            "response_url": "",
            "content_type": "",
            "html_length": 0,
            "parsed_results_count": 0,
            "filtered_results_count": 0,
            "final_results_count": 0,
            "page_title": ""
        }
        
        try:
            client = httpx.Client(timeout=timeout)
            response = client.get(base_url, params={"q": query}, headers=headers)
            
            debug_dict["status_code"] = response.status_code
            debug_dict["response_url"] = str(response.url)
            debug_dict["content_type"] = response.headers.get("content-type", "")
            debug_dict["html_length"] = len(response.text)
            
            # Rate limit or block detection
            if response.status_code in (202, 403, 429):
                raise SearchProviderRateLimitError(self.name(), "DuckDuckGo blocked/rate-limited the request.", response.status_code, debug_dict)
            elif response.status_code != 200:
                raise SearchProviderHttpError(self.name(), f"HTTP error code", response.status_code, debug_dict)

            soup = BeautifulSoup(response.text, "html.parser")
            page_title = soup.title.string.strip() if soup.title else ""
            debug_dict["page_title"] = page_title[:200]
            
            raw_links = []
            for body in soup.find_all("div", class_="result__body"):
                a_tag = body.find("a", class_="result__a")
                if a_tag:
                    href = a_tag.get("href")
                    title = a_tag.get_text().strip()
                    snippet_a = body.find("a", class_="result__snippet")
                    snippet = snippet_a.get_text().strip() if snippet_a else ""
                    if href:
                        raw_links.append((href, title, snippet))
            
            if not raw_links:
                for a_tag in soup.find_all("a", class_="result__a"):
                    href = a_tag.get("href")
                    title = a_tag.get_text().strip()
                    if href:
                        raw_links.append((href, title, ""))
            
            debug_dict["parsed_results_count"] = len(raw_links)
            filtered = 0
            parsed_items = []
            
            for href, title, snippet in raw_links:
                real_url = self._extract_real_url(href)
                if real_url and (real_url.startswith("http://") or real_url.startswith("https://")):
                    if "duckduckgo.com" not in real_url:
                        urls.append(real_url)
                        parsed_items.append({
                            "url": real_url,
                            "title": title,
                            "snippet": snippet,
                            "position": len(urls)
                        })
                    else:
                        filtered += 1
                else:
                    filtered += 1
            
            debug_dict["filtered_results_count"] = filtered
            debug_dict["final_results_count"] = len(urls)
            debug_dict["organic_results_parsed"] = parsed_items
            client.close()
            
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="ok",
                status_code=200,
                urls=urls,
                debug=debug_dict
            )
        except SearchProviderRateLimitError as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="rate_limited",
                status_code=e.status_code,
                urls=[],
                debug=e.debug_dict
            )
        except SearchProviderError as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="error",
                status_code=e.status_code,
                urls=[],
                debug={"error": e.message, **e.debug_dict}
            )
        except Exception as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="error",
                status_code=None,
                urls=[],
                debug={"error": f"Connection failed: {str(e)}", **debug_dict}
            )


class BingHtmlSearchProvider(SearchProvider):
    def name(self) -> str:
        return "BingHtml"

    def _decode_bing_url(self, url: str) -> str:
        if "bing.com/ck/a?!" not in url:
            return url
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            u_vals = qs.get("u")
            if not u_vals:
                return url
            u_val = u_vals[0]
            if len(u_val) > 2 and u_val.startswith("a"):
                b64_part = u_val[2:]
                missing_padding = len(b64_part) % 4
                if missing_padding:
                    b64_part += '=' * (4 - missing_padding)
                decoded = base64.b64decode(b64_part).decode("utf-8", errors="ignore")
                if decoded.startswith("http://") or decoded.startswith("https://"):
                    return decoded
        except Exception:
            pass
        return url

    def search(self, query: str, max_pages: int = 1, timeout: int = 20, **kwargs) -> SearchProviderResult:
        base_url = "https://www.bing.com/search"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Referer": "https://www.bing.com/"
        }
        urls = []
        debug_dict = {
            "provider": self.name(),
            "query": query,
            "status_code": None,
            "response_url": "",
            "content_type": "",
            "html_length": 0,
            "parsed_results_count": 0,
            "filtered_results_count": 0,
            "final_results_count": 0,
            "page_title": ""
        }
        
        try:
            client = httpx.Client(timeout=timeout)
            response = client.get(base_url, params={"q": query}, headers=headers)
            
            debug_dict["status_code"] = response.status_code
            debug_dict["response_url"] = str(response.url)
            debug_dict["content_type"] = response.headers.get("content-type", "")
            debug_dict["html_length"] = len(response.text)
            
            if response.status_code in (403, 429):
                raise SearchProviderRateLimitError(self.name(), "Bing blocked/rate-limited the request.", response.status_code, debug_dict)
            elif response.status_code != 200:
                raise SearchProviderHttpError(self.name(), "HTTP error code", response.status_code, debug_dict)

            if "challenges.cloudflare.com" in response.text or "challenge/verify" in response.text:
                raise SearchProviderRateLimitError(self.name(), "Bing returned a Cloudflare Turnstile bot challenge.", response.status_code, debug_dict)

            soup = BeautifulSoup(response.text, "html.parser")
            page_title = soup.title.string.strip() if soup.title else ""
            debug_dict["page_title"] = page_title[:200]
            
            # --- Robust multi-level parsing selectors ---
            all_links = []
            
            # 1. Main selector: li.b_algo h2 a
            for li in soup.find_all("li", class_="b_algo"):
                h2 = li.find("h2")
                if h2:
                    a_tag = h2.find("a")
                    if a_tag and a_tag.get("href"):
                        href = a_tag.get("href")
                        title = a_tag.get_text().strip()
                        caption = li.find("div", class_="b_caption") or li.find("p")
                        snippet = caption.get_text().strip() if caption else ""
                        all_links.append((href, title, snippet))
            
            # 2. Fallback 1: links within id="b_results"
            if not all_links:
                b_results = soup.find(id="b_results")
                if b_results:
                    for a_tag in b_results.find_all("a"):
                        href = a_tag.get("href")
                        if href:
                            title = a_tag.get_text().strip()
                            all_links.append((href, title, ""))
                            
            # 3. Fallback 2: all h2 a tags
            if not all_links:
                for h2 in soup.find_all("h2"):
                    a_tag = h2.find("a")
                    if a_tag and a_tag.get("href"):
                        href = a_tag.get("href")
                        title = a_tag.get_text().strip()
                        all_links.append((href, title, ""))
                        
            # 4. Fallback 3: all a[href] tags
            if not all_links:
                for a_tag in soup.find_all("a"):
                    href = a_tag.get("href")
                    if href:
                        title = a_tag.get_text().strip()
                        all_links.append((href, title, ""))

            debug_dict["parsed_results_count"] = len(all_links)
            filtered = 0
            parsed_items = []
            
            # Excluded internal domains
            excluded_domains = [
                "bing.com", "microsoft.com", "go.microsoft.com", 
                "login.live.com", "account.microsoft.com"
            ]
            
            for href, title, snippet in all_links:
                href = href.strip()
                href = self._decode_bing_url(href)
                
                if href.startswith("javascript:") or href.startswith("mailto:"):
                    filtered += 1
                    continue
                if not href.startswith("http://") and not href.startswith("https://"):
                    filtered += 1
                    continue
                try:
                    parsed = urlparse(href)
                    domain = parsed.netloc.lower()
                    if any(ex == domain or domain.endswith("." + ex) for ex in excluded_domains):
                        filtered += 1
                        continue
                    # Deduplicate within this single query run
                    if href not in urls:
                        urls.append(href)
                        parsed_items.append({
                            "url": href,
                            "title": title,
                            "snippet": snippet,
                            "position": len(urls)
                        })
                except Exception:
                    filtered += 1
                    continue
            
            debug_dict["filtered_results_count"] = filtered
            debug_dict["final_results_count"] = len(urls)
            debug_dict["organic_results_parsed"] = parsed_items
            client.close()
            
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="ok",
                status_code=200,
                urls=urls,
                debug=debug_dict
            )
        except SearchProviderRateLimitError as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="rate_limited",
                status_code=e.status_code,
                urls=[],
                debug=e.debug_dict
            )
        except SearchProviderError as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="error",
                status_code=e.status_code,
                urls=[],
                debug={"error": e.message, **e.debug_dict}
            )
        except Exception as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="error",
                status_code=None,
                urls=[],
                debug={"error": f"Connection failed: {str(e)}", **debug_dict}
            )


class GoogleNewsRssSearchProvider(SearchProvider):
    def name(self) -> str:
        return "GoogleNewsRss"

    def search(self, query: str, max_pages: int = 1, timeout: int = 20, **kwargs) -> SearchProviderResult:
        encoded_query = quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=es&gl=ES&ceid=ES:es"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/xml,text/xml,*/*;q=0.8"
        }
        urls = []
        debug_dict = {
            "provider": self.name(),
            "query": query,
            "status_code": None,
            "response_url": "",
            "content_type": "",
            "html_length": 0,
            "parsed_results_count": 0,
            "filtered_results_count": 0,
            "final_results_count": 0,
            "page_title": "Google News RSS Feed"
        }
        
        try:
            client = httpx.Client(timeout=timeout)
            response = client.get(url, headers=headers)
            
            debug_dict["status_code"] = response.status_code
            debug_dict["response_url"] = str(response.url)
            debug_dict["content_type"] = response.headers.get("content-type", "")
            debug_dict["html_length"] = len(response.content)
            
            if response.status_code in (403, 429):
                raise SearchProviderRateLimitError(self.name(), "Google News RSS blocked the request.", response.status_code, debug_dict)
            elif response.status_code != 200:
                raise SearchProviderHttpError(self.name(), "HTTP error code", response.status_code, debug_dict)

            # Parse XML
            root = ET.fromstring(response.content)
            raw_links = []
            for item in root.findall(".//item"):
                link = item.find("link")
                title_elem = item.find("title")
                title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                desc_elem = item.find("description")
                snippet = desc_elem.text.strip() if desc_elem is not None and desc_elem.text else ""
                pub_date_elem = item.find("pubDate")
                pub_date_str = pub_date_elem.text.strip() if pub_date_elem is not None and pub_date_elem.text else ""
                
                # Parse RSS date to YYYY-MM-DD
                pub_date_iso = ""
                if pub_date_str:
                    try:
                        parsed_dt = email.utils.parsedate_to_datetime(pub_date_str)
                        pub_date_iso = parsed_dt.strftime("%Y-%m-%d")
                    except Exception:
                        pass
                
                if link is not None and link.text:
                    raw_links.append((link.text.strip(), title, snippet, pub_date_iso))
            
            debug_dict["parsed_results_count"] = len(raw_links)
            filtered = 0
            parsed_items = []
            
            for link_text, title, snippet, pub_date_iso in raw_links:
                if link_text.startswith("http://") or link_text.startswith("https://"):
                    urls.append(link_text)
                    parsed_items.append({
                        "url": link_text,
                        "title": title,
                        "snippet": snippet,
                        "pub_date": pub_date_iso,
                        "position": len(urls)
                    })
                else:
                    filtered += 1
            
            debug_dict["filtered_results_count"] = filtered
            debug_dict["final_results_count"] = len(urls)
            debug_dict["organic_results_parsed"] = parsed_items
            client.close()
            
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="ok",
                status_code=200,
                urls=urls,
                debug=debug_dict
            )
        except SearchProviderRateLimitError as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="rate_limited",
                status_code=e.status_code,
                urls=[],
                debug=e.debug_dict
            )
        except SearchProviderError as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="error",
                status_code=e.status_code,
                urls=[],
                debug={"error": e.message, **e.debug_dict}
            )
        except Exception as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="error",
                status_code=None,
                urls=[],
                debug={"error": f"Connection failed: {str(e)}", **debug_dict}
            )


class SerpApiSearchProvider(SearchProvider):
    def name(self) -> str:
        return "SerpAPI"

    def search(self, query: str, max_pages: int = 1, timeout: int = 20, api_key: Optional[str] = None, **kwargs) -> SearchProviderResult:
        key = api_key or settings.SERPAPI_API_KEY
        debug_dict = {
            "provider": self.name(),
            "query": query,
            "status_code": None,
            "response_url": "",
            "content_type": "",
            "html_length": 0,
            "parsed_results_count": 0,
            "filtered_results_count": 0,
            "final_results_count": 0,
            "page_title": "SerpAPI Results"
        }
        
        if not key:
            debug_dict["page_title"] = "Error: SerpAPI API Key is missing."
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="error",
                status_code=400,
                urls=[],
                debug={"error": "SerpAPI API Key is missing in configuration.", **debug_dict}
            )
            
        url = "https://serpapi.com/search.json"
        params = {
            "engine": "google",
            "q": query,
            "api_key": key,
            "hl": "es",
            "gl": "es",
            "num": 20
        }
        
        try:
            client = httpx.Client(timeout=timeout)
            response = client.get(url, params=params)
            debug_dict["status_code"] = response.status_code
            debug_dict["response_url"] = str(response.url)
            debug_dict["content_type"] = response.headers.get("content-type", "")
            debug_dict["html_length"] = len(response.text)
            
            if response.status_code in (401, 403):
                return SearchProviderResult(
                    provider=self.name(),
                    query=query,
                    status="rate_limited",
                    status_code=response.status_code,
                    urls=[],
                    debug={"error": "SerpAPI credentials invalid or rate limited.", **debug_dict}
                )
            elif response.status_code != 200:
                return SearchProviderResult(
                    provider=self.name(),
                    query=query,
                    status="error",
                    status_code=response.status_code,
                    urls=[],
                    debug={"error": f"SerpAPI returned HTTP {response.status_code}", **debug_dict}
                )
                
            data = response.json()
            organic_results = data.get("organic_results", [])
            debug_dict["parsed_results_count"] = len(organic_results)
            
            urls = []
            parsed_items = []
            for item in organic_results:
                link = item.get("link")
                if link and (link.startswith("http://") or link.startswith("https://")):
                    urls.append(link)
                    parsed_items.append({
                        "url": link,
                        "title": item.get("title"),
                        "snippet": item.get("snippet"),
                        "position": item.get("position")
                    })
                    
            debug_dict["final_results_count"] = len(urls)
            debug_dict["organic_results_parsed"] = parsed_items
            client.close()
            
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="ok",
                status_code=200,
                urls=urls,
                debug=debug_dict
            )
        except Exception as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="error",
                status_code=None,
                urls=[],
                debug={"error": f"Connection failed: {str(e)}", **debug_dict}
            )


class DataForSeoSearchProvider(SearchProvider):
    def name(self) -> str:
        return "DataForSEO"

    def search(self, query: str, max_pages: int = 1, timeout: int = 20, login: Optional[str] = None, password: Optional[str] = None, **kwargs) -> SearchProviderResult:
        usr = login or settings.DATAFORSEO_LOGIN
        pwd = password or settings.DATAFORSEO_PASSWORD
        debug_dict = {
            "provider": self.name(),
            "query": query,
            "status_code": None,
            "response_url": "",
            "content_type": "",
            "html_length": 0,
            "parsed_results_count": 0,
            "filtered_results_count": 0,
            "final_results_count": 0,
            "page_title": "DataForSEO Results"
        }
        
        if not usr or not pwd:
            debug_dict["page_title"] = "Error: DataForSEO credentials missing."
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="error",
                status_code=400,
                urls=[],
                debug={"error": "DataForSEO credentials missing in configuration.", **debug_dict}
            )
            
        url = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
        headers = {
            "Content-Type": "application/json"
        }
        post_data = [
            {
                "keyword": query,
                "language_code": "es",
                "location_code": 2724, # Spain
                "limit": 20
            }
        ]
        
        try:
            client = httpx.Client(timeout=timeout, auth=(usr, pwd))
            response = client.post(url, headers=headers, json=post_data)
            
            debug_dict["status_code"] = response.status_code
            debug_dict["response_url"] = str(response.url)
            debug_dict["content_type"] = response.headers.get("content-type", "")
            debug_dict["html_length"] = len(response.text)
            
            if response.status_code in (401, 403):
                return SearchProviderResult(
                    provider=self.name(),
                    query=query,
                    status="rate_limited",
                    status_code=response.status_code,
                    urls=[],
                    debug={"error": "DataForSEO authentication failed or rate limited.", **debug_dict}
                )
            elif response.status_code != 200:
                return SearchProviderResult(
                    provider=self.name(),
                    query=query,
                    status="error",
                    status_code=response.status_code,
                    urls=[],
                    debug={"error": f"DataForSEO returned HTTP {response.status_code}", **debug_dict}
                )
                
            data = response.json()
            tasks = data.get("tasks", [])
            if not tasks:
                return SearchProviderResult(
                    provider=self.name(),
                    query=query,
                    status="error",
                    status_code=200,
                    urls=[],
                    debug={"error": "DataForSEO returned empty tasks.", **debug_dict}
                )
                
            task_result = tasks[0].get("result", [])
            if not task_result:
                return SearchProviderResult(
                    provider=self.name(),
                    query=query,
                    status="error",
                    status_code=200,
                    urls=[],
                    debug={"error": "DataForSEO task result is empty.", **debug_dict}
                )
                
            items = task_result[0].get("items", [])
            debug_dict["parsed_results_count"] = len(items)
            
            urls = []
            parsed_items = []
            for item in items:
                if item.get("type") == "organic":
                    link = item.get("url")
                    if link and (link.startswith("http://") or link.startswith("https://")):
                        urls.append(link)
                        parsed_items.append({
                            "url": link,
                            "title": item.get("title"),
                            "snippet": item.get("description"),
                            "position": item.get("rank_group")
                        })
                        
            debug_dict["final_results_count"] = len(urls)
            debug_dict["organic_results_parsed"] = parsed_items
            client.close()
            
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="ok",
                status_code=200,
                urls=urls,
                debug=debug_dict
            )
        except Exception as e:
            return SearchProviderResult(
                provider=self.name(),
                query=query,
                status="error",
                status_code=None,
                urls=[],
                debug={"error": f"Connection failed: {str(e)}", **debug_dict}
            )
