import httpx
import trafilatura
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from app.config import settings
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("encuentro-noticias")

class ArticleExtractor:
    def __init__(self):
        self.headers = {
            "User-Agent": settings.SCRAPER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3"
        }

    def _get_domain(self, url: str) -> str:
        try:
            parsed = urlparse(url)
            domain = parsed.netloc
            if domain.startswith("www."):
                domain = domain[4:]
            return domain
        except Exception:
            return ""

    def extract(self, url: str) -> Dict[str, Any]:
        """
        Downloads a URL and extracts article title, main text, author, publish date, and publication name.
        Uses trafilatura as primary and BeautifulSoup as fallback.
        """
        logger.info(f"Extracting content from: {url}")
        
        # 1. Download page content
        try:
            with httpx.Client(timeout=settings.REQUEST_TIMEOUT_SECONDS, follow_redirects=True) as client:
                response = client.get(url, headers=self.headers)
            
            if response.status_code != 200:
                raise ValueError(f"HTTP error status: {response.status_code}")
                
            html_content = response.text
        except Exception as e:
            logger.warning(f"Failed to download {url}: {e}")
            raise RuntimeError(f"error HTTP: {str(e)}")

        if not html_content or len(html_content.strip()) == 0:
            raise RuntimeError("texto insuficiente")

        # 2. Main extraction: Trafilatura
        extracted_data = {
            "title": "",
            "text": "",
            "author": "",
            "date": "",
            "publication_name": self._get_domain(url),
            "url": url
        }

        try:
            # bare_extraction returns a dictionary or an object depending on version
            result = trafilatura.bare_extraction(html_content, url=url)
            if result:
                # If result is a dict (common in modern trafilatura versions)
                if isinstance(result, dict):
                    extracted_data["title"] = result.get("title") or ""
                    extracted_data["text"] = result.get("text") or ""
                    extracted_data["author"] = result.get("author") or ""
                    extracted_data["date"] = result.get("date") or ""
                    extracted_data["publication_name"] = result.get("sitename") or result.get("hostname") or self._get_domain(url)
                else:
                    # If it's a document object
                    extracted_data["title"] = getattr(result, "title", "") or ""
                    extracted_data["text"] = getattr(result, "text", "") or ""
                    extracted_data["author"] = getattr(result, "author", "") or ""
                    extracted_data["date"] = getattr(result, "date", "") or ""
                    extracted_data["publication_name"] = getattr(result, "sitename", "") or getattr(result, "hostname", "") or self._get_domain(url)
        except Exception as e:
            logger.warning(f"Trafilatura extraction failed for {url}: {e}")

        # 3. Fallback: BeautifulSoup
        # If text is empty or too short, we fall back to a simple BeautifulSoup parser
        if not extracted_data["text"] or len(extracted_data["text"].strip()) < 150:
            logger.debug(f"Trafilatura returned insufficient text ({len(extracted_data['text'] or '')} chars). Falling back to BeautifulSoup.")
            try:
                soup = BeautifulSoup(html_content, "html.parser")
                
                # Title
                if not extracted_data["title"]:
                    h1 = soup.find("h1")
                    if h1:
                        extracted_data["title"] = h1.get_text().strip()
                    else:
                        title_tag = soup.find("title")
                        if title_tag:
                            extracted_data["title"] = title_tag.get_text().strip()
                
                # Text: compile all paragraphs
                paragraphs = []
                # Exclude header, footer, nav, aside elements
                for element in soup.find_all(["header", "footer", "nav", "aside", "script", "style"]):
                    element.decompose()
                
                for p in soup.find_all("p"):
                    p_text = p.get_text().strip()
                    if len(p_text) > 30: # Filter out tiny links/utility texts
                        paragraphs.append(p_text)
                
                extracted_data["text"] = "\n\n".join(paragraphs)

                # Author search in meta tags
                if not extracted_data["author"]:
                    meta_author = soup.find("meta", attrs={"name": "author"}) or soup.find("meta", attrs={"property": "article:author"})
                    if meta_author:
                        extracted_data["author"] = meta_author.get("content", "").strip()

                # Date search in meta, time tags, and JSON-LD
                if not extracted_data["date"]:
                    meta_date = (
                        soup.find("meta", attrs={"property": "article:published_time"}) or
                        soup.find("meta", attrs={"name": "pubdate"}) or
                        soup.find("meta", attrs={"property": "datePublished"}) or
                        soup.find("meta", attrs={"name": "date"}) or
                        soup.find("meta", attrs={"itemprop": "datePublished"})
                    )
                    if meta_date:
                        extracted_data["date"] = meta_date.get("content", "").strip()

                if not extracted_data["date"]:
                    time_tag = soup.find("time", attrs={"datetime": True})
                    if time_tag:
                        extracted_data["date"] = time_tag.get("datetime", "").strip()

                if not extracted_data["date"]:
                    import json
                    for script in soup.find_all("script", type="application/ld+json"):
                        try:
                            if script.string:
                                ld = json.loads(script.string)
                                items = ld if isinstance(ld, list) else [ld]
                                for item in items:
                                    if not isinstance(item, dict):
                                        continue
                                    for f in ["datePublished", "dateCreated", "uploadDate"]:
                                        if f in item and item[f]:
                                            extracted_data["date"] = str(item[f]).strip()
                                            break
                                    if extracted_data["date"]:
                                        break
                                if extracted_data["date"]:
                                    break
                        except Exception:
                            pass
                        
            except Exception as e:
                logger.error(f"BS4 fallback extraction failed for {url}: {e}")

        # Clean fields
        for k in ["title", "text", "author", "date", "publication_name"]:
            if extracted_data[k]:
                extracted_data[k] = str(extracted_data[k]).strip()

        # Validation of text presence
        if not extracted_data["text"] or len(extracted_data["text"].strip()) < 100:
            raise RuntimeError("texto insuficiente")

        return extracted_data

article_extractor = ArticleExtractor()
