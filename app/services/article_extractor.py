import httpx
import trafilatura
import json
import re
import unicodedata
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

def normalize_date(date_str: str) -> Optional[str]:
    if not date_str:
        return None
    s = date_str.strip()
    
    # 1. Matches YYYY-MM-DD
    m1 = re.search(r"\b(\d{4})[-/](\d{2})[-/](\d{2})\b", s)
    if m1:
        return f"{m1.group(1)}-{m1.group(2)}-{m1.group(3)}"
        
    # 2. Matches DD-MM-YYYY or DD/MM/YYYY
    m2 = re.search(r"\b(\d{2})[-/](\d{2})[-/](\d{4})\b", s)
    if m2:
        return f"{m2.group(3)}-{m2.group(2)}-{m2.group(1)}"
        
    # 3. ISO-8601 like 2024-07-23T14:30:00Z
    if len(s) >= 10 and s[0:4].isdigit() and s[4] in ('-', '/') and s[5:7].isdigit() and s[7] in ('-', '/') and s[8:10].isdigit():
        return f"{s[0:4]}-{s[5:7]}-{s[8:10]}"
        
    # 4. Text dates in Spanish/English (e.g. 23 de julio de 2024)
    months_es = {
        "enero": "01", "febrero": "02", "marzo": "03", "abril": "04", "mayo": "05", "junio": "06",
        "julio": "07", "agosto": "08", "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
        "ene": "01", "feb": "02", "mar": "03", "abr": "04", "may": "05", "jun": "06",
        "jul": "07", "ago": "08", "sep": "09", "oct": "10", "nov": "11", "dic": "12"
    }
    s_lower = s.lower()
    m_text = re.search(r"\b(\d{1,2})\s+(?:de\s+)?([a-zñáéíóú]{3,10})\s+(?:de\s+)?(\d{4})\b", s_lower)
    if m_text:
        day_str = m_text.group(1).zfill(2)
        month_name = m_text.group(2)
        year_str = m_text.group(3)
        month_name_norm = "".join(c for c in unicodedata.normalize('NFD', month_name) if unicodedata.category(c) != 'Mn')
        if month_name_norm in months_es:
            return f"{year_str}-{months_es[month_name_norm]}-{day_str}"
            
    return None

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

    def extract_article_metadata(self, url: str, html: str, provider_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Parses HTML to extract article metadata (author, date, publication name)
        using precise priority rules and sources.
        """
        soup = BeautifulSoup(html, "html.parser")
        domain_name = self._get_domain(url).lower()
        
        # 1. AUTHOR EXTRACTION
        author = ""
        author_source = "empty"
        
        def parse_author_node(node):
            if not node:
                return ""
            if isinstance(node, dict):
                return node.get("name") or node.get("@value") or ""
            if isinstance(node, list):
                for item in node:
                    res = parse_author_node(item)
                    if res:
                        return res
            if isinstance(node, str):
                return node
            return ""

        # JSON-LD Author
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                if script.string:
                    ld = json.loads(script.string)
                    items = ld if isinstance(ld, list) else [ld]
                    
                    def find_in_graph(graph_items):
                        for item in graph_items:
                            if not isinstance(item, dict):
                                continue
                            itype = item.get("@type", "")
                            itypes = [itype] if isinstance(itype, str) else itype
                            if any(t in ("Article", "NewsArticle", "BlogPosting", "TechArticle", "Report", "OpinionNewsArticle") for t in itypes):
                                if "author" in item:
                                    auth_val = parse_author_node(item["author"])
                                    if auth_val:
                                        return auth_val
                            for k, v in item.items():
                                if isinstance(v, dict):
                                    res = find_in_graph([v])
                                    if res:
                                        return res
                                elif isinstance(v, list):
                                    res = find_in_graph(v)
                                    if res:
                                        return res
                        return ""
                    
                    for item in items:
                        if isinstance(item, dict) and "@graph" in item:
                            res = find_in_graph(item["@graph"])
                            if res:
                                author = res
                                author_source = "jsonld"
                                break
                    if author:
                        break
                    res = find_in_graph(items)
                    if res:
                        author = res
                        author_source = "jsonld"
                        break
            except Exception:
                pass

        # Meta tags Author
        if not author:
            meta_attrs = [
                {"name": "author"},
                {"property": "article:author"},
                {"name": "byl"},
                {"name": "parsely-author"},
                {"name": "sailthru.author"},
                {"name": "dc.creator"},
                {"name": "dcterms.creator"}
            ]
            for attr in meta_attrs:
                meta_tag = soup.find("meta", attrs=attr)
                if meta_tag:
                    val = meta_tag.get("content", "").strip()
                    if val:
                        author = val
                        author_source = "meta"
                        break

        # Visible selectors Author
        if not author:
            selectors = [
                "[rel='author']",
                ".author",
                ".byline",
                ".article-author",
                ".post-author",
                ".entry-author",
                ".td-post-author-name",
                ".jeg_meta_author",
                ".vcard.author"
            ]
            for selector in selectors:
                found_element = soup.select_one(selector)
                if found_element:
                    val = found_element.get_text(strip=True)
                    if val:
                        author = val
                        author_source = "selector"
                        break

        if author:
            # Strip "por", "by", "autor:", etc. case-insensitively
            prefix_match = re.match(r"(?i)^(?:por|by|autor\s*:\s*)\s+(.*)$", author.strip())
            if prefix_match:
                author = prefix_match.group(1).strip()

        # Patterns near start of text
        clean_text_temp = ""
        body = soup.find("body")
        if body:
            # Create a copy so we do not deform main parsed HTML
            body_copy = BeautifulSoup(str(body), "html.parser")
            for tag in body_copy.find_all(["script", "style", "header", "footer", "nav", "aside"]):
                tag.decompose()
            clean_text_temp = body_copy.get_text()
        else:
            clean_text_temp = soup.get_text()
        clean_text_temp = " ".join(clean_text_temp.split())
        first_1200 = clean_text_temp[:1200]

        if not author:
            pattern_regex = r"\b(?:[pP]or|[bB]y|[aA]utor\s*:\s*)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñíóúáéé]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñíóúáéé]+){1,2})"
            match = re.search(pattern_regex, first_1200)
            if match:
                author = match.group(1).strip()
                author_source = "pattern"

        if not author:
            if "redacción" in first_1200.lower() or "redaccion" in first_1200.lower():
                author = "Redacción"
                author_source = "pattern"

        # Verify not duplicating domain as author
        if author:
            author_clean = author.strip()
            author_lower = author_clean.lower()
            if author_lower in ("redacción", "redaccion"):
                author = "Redacción"
            else:
                sld = domain_name.split(".")[0] if "." in domain_name else domain_name
                if (author_lower == domain_name or 
                    author_lower == f"www.{domain_name}" or
                    author_lower.replace(" ", "") == sld or
                    author_lower.replace(" ", "") == domain_name.replace(".", "") or
                    ("news" in author_lower and domain_name in author_lower) or
                    author_lower == sld):
                    author = ""
                    author_source = "empty"

        # 2. DATE EXTRACTION
        pub_date = ""
        date_source = "empty"
        
        def parse_date_fields(item_dict):
            for f in ["datePublished", "dateCreated", "uploadDate"]:
                if f in item_dict and item_dict[f]:
                    return str(item_dict[f]).strip(), "jsonld"
            if "dateModified" in item_dict and item_dict["dateModified"]:
                return str(item_dict["dateModified"]).strip(), "jsonld"
            return "", "empty"

        def find_date_in_graph(graph_items):
            for item in graph_items:
                if not isinstance(item, dict):
                    continue
                itype = item.get("@type", "")
                itypes = [itype] if isinstance(itype, str) else itype
                if any(t in ("Article", "NewsArticle", "BlogPosting", "TechArticle", "Report", "OpinionNewsArticle") for t in itypes):
                    res, src = parse_date_fields(item)
                    if res:
                        return res, src
                for k, v in item.items():
                    if isinstance(v, dict):
                        res, src = find_date_in_graph([v])
                        if res:
                            return res, src
                    elif isinstance(v, list):
                        res, src = find_date_in_graph(v)
                        if res:
                            return res, src
            return "", "empty"

        # JSON-LD Date
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                if script.string:
                    ld = json.loads(script.string)
                    items = ld if isinstance(ld, list) else [ld]
                    for item in items:
                        if isinstance(item, dict) and "@graph" in item:
                            res, src = find_date_in_graph(item["@graph"])
                            if res:
                                pub_date, date_source = res, src
                                break
                    if pub_date:
                        break
                    res, src = find_date_in_graph(items)
                    if res:
                        pub_date, date_source = res, src
                        break
            except Exception:
                pass

        # Meta tags Date
        if not pub_date:
            meta_date_attrs = [
                {"property": "article:published_time"},
                {"name": "pubdate"},
                {"property": "datePublished"},
                {"name": "date"},
                {"itemprop": "datePublished"},
                {"name": "publishdate"},
                {"name": "publication_date"},
                {"name": "dc.date"},
                {"name": "dcterms.date"},
                {"name": "dcterms.created"},
                {"name": "parsely-pub-date"},
                {"name": "sailthru.date"},
                {"name": "bt:pubDate"},
                {"property": "article:modified_time"}
            ]
            for attr in meta_date_attrs:
                meta_tag = soup.find("meta", attrs=attr)
                if meta_tag:
                    val = meta_tag.get("content", "").strip()
                    if val:
                        pub_date = val
                        date_source = "meta"
                        break

        # HTML time / published tags Date
        if not pub_date:
            time_tag = soup.find("time", attrs={"datetime": True})
            if time_tag:
                pub_date = time_tag.get("datetime", "").strip()
                date_source = "time_tag"
                
        if not pub_date:
            selectors = [
                "time.published",
                "time.entry-date",
                ".published",
                ".date",
                ".post-date"
            ]
            for sel in selectors:
                el = soup.select_one(sel)
                if el:
                    val = el.get("datetime", "").strip() if el.has_attr("datetime") else el.get_text(strip=True)
                    if val:
                        pub_date = val
                        date_source = "time_tag"
                        break

        # Provider Item pub_date fallback
        if not pub_date and provider_item:
            val = provider_item.get("pub_date") or provider_item.get("published_date")
            if val:
                pub_date = str(val).strip()
                date_source = "provider_pub_date"

        # URL inferred date
        if not pub_date:
            m1 = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
            if m1:
                pub_date = f"{m1.group(1)}-{m1.group(2)}-{m1.group(3)}"
                date_source = "url"
            else:
                m2 = re.search(r"/(\d{4})-(\d{2})-(\d{2})/", url)
                if m2:
                    pub_date = f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
                    date_source = "url"
                else:
                    m3 = re.search(r"/(\d{4})(\d{2})(\d{2})/", url)
                    if m3:
                        pub_date = f"{m3.group(1)}-{m3.group(2)}-{m3.group(3)}"
                        date_source = "url"

        # 3. CONFIDENCE AND NORMALIZATION
        metadata_confidence = "high"
        normalized_pub_date = ""
        if pub_date:
            norm = normalize_date(pub_date)
            if norm:
                normalized_pub_date = norm
            else:
                normalized_pub_date = pub_date
                metadata_confidence = "low"
        else:
            metadata_confidence = "low"

        if not author:
            if metadata_confidence == "high":
                metadata_confidence = "medium"
        elif author_source in ("pattern", "fallback"):
            metadata_confidence = "medium"

        return {
            "article_author": author.strip() if author else "",
            "publication_name": self._get_domain(url),
            "published_date": normalized_pub_date,
            "author_source": author_source,
            "date_source": date_source,
            "metadata_confidence": metadata_confidence
        }

    def extract(self, url: str, provider_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Downloads a URL and extracts article title, main text, author, publish date, and publication name.
        Uses trafilatura as primary and BeautifulSoup as fallback.
        """
        logger.info(f"Extracting content from: {url}")
        
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

        extracted_data = {
            "title": "",
            "text": "",
            "author": "",
            "date": "",
            "publication_name": self._get_domain(url),
            "url": url,
            "author_source": "empty",
            "date_source": "empty",
            "metadata_confidence": "low"
        }

        try:
            result = trafilatura.bare_extraction(html_content, url=url)
            if result:
                if isinstance(result, dict):
                    extracted_data["title"] = result.get("title") or ""
                    extracted_data["text"] = result.get("text") or ""
                else:
                    extracted_data["title"] = getattr(result, "title", "") or ""
                    extracted_data["text"] = getattr(result, "text", "") or ""
        except Exception as e:
            logger.warning(f"Trafilatura extraction failed for {url}: {e}")

        # Fallback to BeautifulSoup for main text/title if empty
        if not extracted_data["text"] or len(extracted_data["text"].strip()) < 150:
            logger.debug(f"Trafilatura returned insufficient text. Falling back to BeautifulSoup.")
            try:
                soup = BeautifulSoup(html_content, "html.parser")
                if not extracted_data["title"]:
                    h1 = soup.find("h1")
                    extracted_data["title"] = h1.get_text().strip() if h1 else (soup.find("title").get_text().strip() if soup.find("title") else "")
                
                # Exclude standard headers/footers
                for element in soup.find_all(["header", "footer", "nav", "aside", "script", "style"]):
                    element.decompose()
                
                paragraphs = []
                for p in soup.find_all("p"):
                    p_text = p.get_text().strip()
                    if len(p_text) > 30:
                        paragraphs.append(p_text)
                extracted_data["text"] = "\n\n".join(paragraphs)
            except Exception as e:
                logger.error(f"BS4 fallback extraction failed for {url}: {e}")

        # Clean/Extract Metadata using advanced deterministic parser
        try:
            metadata = self.extract_article_metadata(url, html_content, provider_item=provider_item)
            extracted_data["author"] = metadata["article_author"]
            extracted_data["date"] = metadata["published_date"]
            extracted_data["publication_name"] = metadata["publication_name"]
            extracted_data["author_source"] = metadata["author_source"]
            extracted_data["date_source"] = metadata["date_source"]
            extracted_data["metadata_confidence"] = metadata["metadata_confidence"]
        except Exception as e:
            logger.warning(f"Deterministic metadata extraction failed for {url}: {e}")

        # Clean final text/title
        for k in ["title", "text"]:
            if extracted_data[k]:
                extracted_data[k] = str(extracted_data[k]).strip()

        if not extracted_data["text"] or len(extracted_data["text"].strip()) < 100:
            raise RuntimeError("texto insuficiente")

        return extracted_data

article_extractor = ArticleExtractor()
