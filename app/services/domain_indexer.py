"""
domain_indexer.py — Crawls cultural/literary domains via sitemap and RSS feeds,
storing discovered URLs into the local SQLite cache.

Per-domain flow:
    1. Try sitemap.xml → sitemap_index.xml → child sitemaps
    2. If RSS URL configured, parse RSS feed
    3. Filter URLs to only cultural/review paths
    4. Upsert into cache_service (SQLite)
    5. Return stats dict
"""
import logging
import datetime
import re
from typing import List, Dict, Any, Callable, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.cache_service import cache_service
from app.config import settings

logger = logging.getLogger("encuentro-noticias")

# ---------------------------------------------------------------------------
# URL cultural path filter
# ---------------------------------------------------------------------------

CULTURAL_PATH_PATTERNS = [
    r"/libro[s]?/",
    r"/rese[nñ]a[s]?/",
    r"/critica[s]?/",
    r"/cr[ií]tica[s]?/",
    r"/cultura/",
    r"/articulo[s]?/",
    r"/art[ií]culo[s]?/",
    r"/literatura/",
    r"/opinion[es]?/",
    r"/opini[oó]n[es]?/",
    r"/lectura[s]?/",
    r"/ensayo[s]?/",
    r"/publicaci[oó]n[es]?/",
    r"/novedades/",
    r"/leer/",
    r"/editorial/",
    r"/comentario[s]?/",
    r"/review[s]?/",
]

EXCLUDE_PATH_PATTERNS = [
    r"/tag[s]?",
    r"/category",
    r"/autor[es]?",
    r"/author[s]?",
    r"/page/\d",
    r"/wp-content",
    r"/wp-admin",
    r"/feed",
    r"/cart",
    r"/tienda",
    r"/shop",
    r"/publicidad",
    r"/anuncio[s]?",
    r"/contacto",
    r"/contact",
    r"/aviso[s]?",
    r"/privacidad",
    r"/politica",
    r"/legal",
    r"/wp-json",
    r"/login",
    r"/search",
    r"/xmlrpc.php",
    r"/wp-includes",
    r"/archive",
]

_cultural_re = [re.compile(p, re.IGNORECASE) for p in CULTURAL_PATH_PATTERNS]
_exclude_re = [re.compile(p, re.IGNORECASE) for p in EXCLUDE_PATH_PATTERNS]


def _is_cultural_url(url: str) -> bool:
    """Return True if the URL looks like an article/post (relaxed filter)."""
    p = urlparse(url)
    path = p.path
    if not path or path == "/":
        return False
    # Exclude obvious static files
    if any(path.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".pdf", ".css", ".js", ".xml"]):
        return False
    if any(rx.search(path) for rx in _exclude_re):
        return False
    return True



def _normalize_url(url: str) -> str:
    """Remove query string and fragment, lowercase scheme+host."""
    p = urlparse(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path}".rstrip("/")


def _title_from_slug(url: str) -> str:
    """Generate a fallback title from the URL slug."""
    try:
        path = urlparse(url).path.rstrip("/")
        if not path:
            return ""
        slug = path.split("/")[-1]
        t = slug.replace("-", " ").replace("_", " ")
        t = " ".join(t.split())
        return t.capitalize()
    except Exception:
        return ""


def _is_index_page(url: str, title: str) -> bool:
    """Return True if the URL or title indicates an index/archive/blog root/category page."""
    path = urlparse(url).path.lower()
    title_lower = title.lower()
    
    # Check URL patterns
    index_patterns = [
        r"/numero[s]?[-\d/]*$",
        r"/archivo[s]?[-\w/]*$",
        r"/blog[s]?[-\w/]*$",
        r"/categoria[s]?/",
        r"/category/",
        r"/seccion[es]?/",
        r"/section[s]?/",
    ]
    if any(re.search(p, path) for p in index_patterns):
        return True
        
    # Check title keywords
    index_keywords = ["numero", "archivo", "blog", "categoria", "category", "seccion", "secciones", "independiente", "archivo general", "suscri"]
    if any(kw in title_lower for kw in index_keywords):
        return True
        
    return False



def _enrich_page_metadata(url: str, timeout: int, discover_links: bool = False, max_links: int = 50) -> Dict[str, Any]:
    """Download page and extract title/snippet/internal links based on priority rules."""
    res = {"title": "", "snippet": "", "discovered_links": []}
    try:
        r = _get(url, timeout=timeout)
        if not r:
            return res
        
        soup = BeautifulSoup(r.text, "html.parser")
        
        # 1. Title Priority: og:title -> h1 -> <title>
        title = ""
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title.get("content").strip()
            
        if not title:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)
                
        if not title:
            html_title = soup.find("title")
            if html_title:
                title = html_title.get_text(strip=True)
                
        # 2. Snippet Priority: og:description -> meta description -> first 300-500 chars of paragraphs
        snippet = ""
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            snippet = og_desc.get("content").strip()
            
        if not snippet:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                snippet = meta_desc.get("content").strip()
                
        if not snippet:
            paragraphs = []
            for p in soup.find_all("p"):
                txt = p.get_text(strip=True)
                if len(txt) > 40:
                    paragraphs.append(txt)
                    if len(paragraphs) >= 3:
                        break
            if paragraphs:
                snippet = " ".join(paragraphs)[:500]
                
        # 3. Discover links from index page
        discovered_links = []
        if discover_links and _is_index_page(url, title):
            a_tags = soup.find_all("a", href=True)
            for a in a_tags:
                if len(discovered_links) >= max_links:
                    break
                href = a["href"]
                resolved_url = urljoin(url, href)
                
                # Normalise and filter domain
                if urlparse(resolved_url).netloc != urlparse(url).netloc:
                    continue
                resolved_url = _normalize_url(resolved_url)
                
                # Exclude root/empty path
                if urlparse(resolved_url).path in ("", "/"):
                    continue
                
                # Exclude non-article URLs using _is_cultural_url
                if not _is_cultural_url(resolved_url):
                    continue
                    
                # Filter anchor text
                anchor = a.get_text(strip=True)
                if len(anchor) < 6:
                    continue
                if any(nav in anchor.lower() for nav in ["leer mas", "ver mas", "read more", "continuar", "enlace"]):
                    continue
                    
                discovered_links.append({
                    "url": resolved_url,
                    "title": anchor.capitalize()
                })
        
        res["title"] = title
        res["snippet"] = snippet
        res["discovered_links"] = discovered_links
    except Exception as e:
        logger.debug(f"Failed to enrich metadata for {url}: {e}")
    return res



# ---------------------------------------------------------------------------

# HTTP helper
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; encuentro-noticias-indexer/1.0; +https://github.com/doobot-ai/encuentro-noticias)"
}


def _get(url: str, timeout: int = 20) -> Optional[httpx.Response]:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        if r.status_code == 200:
            return r
    except Exception as e:
        logger.debug(f"HTTP GET failed for {url}: {e}")
    return None


# ---------------------------------------------------------------------------
# Sitemap parser
# ---------------------------------------------------------------------------

def _parse_sitemap_xml(content: str, base_domain: str, max_urls: int) -> List[Dict[str, Any]]:
    """Parse a sitemap XML (plain or index) and return URL dicts."""
    found: List[Dict[str, Any]] = []
    try:
        soup = BeautifulSoup(content, "xml")
    except Exception:
        soup = BeautifulSoup(content, "html.parser")

    # Sitemap index: contains <sitemap> elements with <loc>
    sitemaps = soup.find_all("sitemap")
    if sitemaps:
        child_locs = [s.find("loc") for s in sitemaps if s.find("loc")]
        for loc_tag in child_locs[:30]:  # limit child sitemaps
            child_url = loc_tag.get_text(strip=True)
            r = _get(child_url)
            if r:
                child_items = _parse_sitemap_xml(r.text, base_domain, max_urls - len(found))
                found.extend(child_items)
                if len(found) >= max_urls:
                    break
        return found

    # Regular sitemap: contains <url> elements with <loc>, optional <title>/<description>
    for url_tag in soup.find_all("url"):
        if len(found) >= max_urls:
            break
        loc = url_tag.find("loc")
        if not loc:
            continue
        url = loc.get_text(strip=True)
        if not _is_cultural_url(url):
            continue
        title_tag = url_tag.find("news:title") or url_tag.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        lastmod_tag = url_tag.find("lastmod") or url_tag.find("news:publication_date")
        pub_date = lastmod_tag.get_text(strip=True) if lastmod_tag else ""
        found.append({
            "url": url,
            "url_normalized": _normalize_url(url),
            "title": title,
            "snippet": "",
            "pub_date": pub_date,
            "source_type": "sitemap",
        })

    return found


# ---------------------------------------------------------------------------
# RSS parser
# ---------------------------------------------------------------------------

def _parse_rss(content: str, max_urls: int) -> List[Dict[str, Any]]:
    """Parse RSS/Atom feed and return URL dicts."""
    found: List[Dict[str, Any]] = []
    try:
        soup = BeautifulSoup(content, "xml")
    except Exception:
        soup = BeautifulSoup(content, "html.parser")

    items = soup.find_all("item") or soup.find_all("entry")
    for item in items:
        if len(found) >= max_urls:
            break
        link = item.find("link")
        url = ""
        if link:
            # Atom: <link href="..."/>; RSS: text content
            url = link.get("href") or link.get_text(strip=True)
        if not url:
            continue
        title_tag = item.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        desc_tag = item.find("description") or item.find("summary")
        snippet_raw = desc_tag.get_text(strip=True) if desc_tag else ""
        # Strip HTML tags from snippet
        snippet = re.sub(r"<[^>]+>", "", snippet_raw)[:300]
        pub_tag = item.find("pubDate") or item.find("published") or item.find("updated")
        pub_date = pub_tag.get_text(strip=True) if pub_tag else ""
        found.append({
            "url": url,
            "url_normalized": _normalize_url(url),
            "title": title,
            "snippet": snippet,
            "pub_date": pub_date,
            "source_type": "rss",
        })

    return found


# ---------------------------------------------------------------------------
# Domain indexer
# ---------------------------------------------------------------------------

class DomainIndexer:

    def index_domain(
        self,
        domain_config: Dict[str, Any],
        config: Dict[str, Any],
        log_fn: Optional[Callable] = None,
        force_refresh: bool = False,
        on_progress: Optional[Callable[[int, int, int], None]] = None,
        sheet_id: str = "",
        run_id: str = ""
    ) -> Dict[str, Any]:
        """
        Index a single domain. Returns stats dict.
        domain_config keys: domain, sitemap_url, rss_url
        """
        import json
        domain = domain_config.get("domain", "").strip()
        if not domain:
            return {"domain": "", "urls_found": 0, "urls_stored": 0, "errors": [], "skipped": True}

        db_path = config.get("DOMAIN_INDEX_DB_PATH", settings.DOMAIN_INDEX_DB_PATH)
        max_urls = int(config.get("DOMAIN_INDEX_MAX_URLS_PER_DOMAIN", settings.DOMAIN_INDEX_MAX_URLS_PER_DOMAIN))
        refresh_days = int(config.get("DOMAIN_INDEX_REFRESH_DAYS", settings.DOMAIN_INDEX_REFRESH_DAYS))
        timeout = int(getattr(settings, "REQUEST_TIMEOUT_SECONDS", 20))

        cache_service.init_db(db_path)

        method = domain_config.get("discovery_method", "auto") or "auto"
        method = method.lower().strip()
        if method == "disabled":
            logger.info(f"DOMAIN_INDEX: {domain} discovery_method is disabled, skipping.")
            return {"domain": domain, "urls_found": 0, "urls_stored": 0, "errors": [], "skipped": True}

        if not force_refresh and not cache_service.needs_refresh(domain, refresh_days):
            logger.info(f"DOMAIN_INDEX: {domain} up-to-date, skipping.")
            return {"domain": domain, "urls_found": 0, "urls_stored": 0, "errors": [], "skipped": True}

        def _log(msg: str):
            logger.info(f"DOMAIN_INDEX [{domain}] {msg}")
            if log_fn:
                log_fn(msg)

        _log("Starting indexation")
        errors: List[str] = []
        all_items: List[Dict[str, Any]] = []

        # 1. Try robots.txt sitemaps extraction
        robots_url = f"https://{domain}/robots.txt"
        robots_found = False
        sitemaps_found_in_robots = []
        
        r_robots = _get(robots_url, timeout=timeout)
        if r_robots:
            robots_found = True
            for line in r_robots.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        sitemaps_found_in_robots.append(parts[1].strip())
                        
        _log(f"robots.txt status: found={robots_found}, sitemaps={sitemaps_found_in_robots}")

        sitemaps_contacted = []
        rss_contacted = []
        discovery_method = "none"
        last_error_msg = ""
        
        # Log SOURCE_DISCOVERY_STARTED
        from app.services.logger_service import logger_service
        logger_service.log(
            "INFO", "SOURCE_DISCOVERY_STARTED",
            f"Iniciando descubrimiento de URLs para {domain} usando método: {method}",
            sheet_id=sheet_id, run_id=run_id
        )

        # Phase A: Sitemap Crawling
        sitemaps_to_try = []
        if method in ("auto", "sitemap"):
            sitemaps_to_try = list(sitemaps_found_in_robots)
            config_sitemap = domain_config.get("sitemap_url") or ""
            if config_sitemap:
                if config_sitemap not in sitemaps_to_try:
                    sitemaps_to_try.insert(0, config_sitemap)
                    
            common_sitemap_urls = [
                f"https://{domain}/sitemap.xml",
                f"https://{domain}/sitemap_index.xml",
                f"https://{domain}/wp-sitemap.xml",
                f"https://{domain}/post-sitemap.xml",
                f"https://{domain}/page-sitemap.xml",
                f"https://{domain}/category-sitemap.xml",
                f"https://{domain}/sitemap-posts.xml",
                f"https://{domain}/news-sitemap.xml",
            ]
            if not sitemaps_to_try:
                sitemaps_to_try = list(common_sitemap_urls)
                is_common_fallback = True
            else:
                is_common_fallback = False
        else:
            is_common_fallback = False
            
        sitemap_items = []
        if method in ("auto", "sitemap"):
            for s_url in sitemaps_to_try:
                _log(f"Trying sitemap: {s_url}")
                r = _get(s_url, timeout=timeout)
                if r:
                    text_stripped = r.text.strip()
                    if "xml" in r.headers.get("content-type", "") or text_stripped.startswith("<"):
                        try:
                            items = _parse_sitemap_xml(r.text, domain, max_urls)
                            if items:
                                sitemap_items.extend(items)
                                sitemaps_contacted.append(s_url)
                                if not is_common_fallback and s_url in sitemaps_found_in_robots:
                                    discovery_method = "robots_txt"
                                else:
                                    discovery_method = "common_sitemap"
                                _log(f"Sitemap {s_url} succeeded: found {len(items)} URLs")
                                
                                # Log SOURCE_SITEMAP_DISCOVERED
                                logger_service.log(
                                    "INFO", "SOURCE_SITEMAP_DISCOVERED",
                                    f"Sitemap descubierta e indexada para {domain}: {s_url} ({len(items)} URLs encontradas)",
                                    sheet_id=sheet_id, run_id=run_id
                                )
                                break
                        except Exception as e:
                            last_error_msg = f"Sitemap parse error on {s_url}: {e}"
                            errors.append(f"sitemap_parse_error_{s_url}")
                    else:
                        last_error_msg = f"Sitemap {s_url} returned non-XML content"
                        errors.append(f"sitemap_invalid_content_{s_url}")
                else:
                    last_error_msg = f"Sitemap {s_url} could not be retrieved"
                    errors.append(f"sitemap_not_retrieved_{s_url}")
                    
            # If sitemap crawling yielded no URLs, try common sitemap URLs if we haven't already
            if not sitemap_items and not is_common_fallback:
                _log("Robots.txt sitemaps yielded 0 URLs. Trying common sitemap paths...")
                for s_url in common_sitemap_urls:
                    if s_url in sitemaps_to_try:
                        continue
                    _log(f"Trying common sitemap: {s_url}")
                    r = _get(s_url, timeout=timeout)
                    if r:
                        text_stripped = r.text.strip()
                        if "xml" in r.headers.get("content-type", "") or text_stripped.startswith("<"):
                            try:
                                items = _parse_sitemap_xml(r.text, domain, max_urls)
                                if items:
                                    sitemap_items.extend(items)
                                    sitemaps_contacted.append(s_url)
                                    discovery_method = "common_sitemap"
                                    _log(f"Common sitemap {s_url} succeeded: found {len(items)} URLs")
                                    
                                    # Log SOURCE_SITEMAP_DISCOVERED
                                    logger_service.log(
                                        "INFO", "SOURCE_SITEMAP_DISCOVERED",
                                        f"Sitemap descubierta e indexada para {domain}: {s_url} ({len(items)} URLs encontradas)",
                                        sheet_id=sheet_id, run_id=run_id
                                    )
                                    break
                            except Exception as e:
                                last_error_msg = f"Sitemap parse error on {s_url}: {e}"
                                errors.append(f"sitemap_parse_error_{s_url}")

        if method in ("auto", "sitemap") and not sitemap_items:
            logger_service.log(
                "WARNING", "SOURCE_DISCOVERY_SITEMAP_FAILED",
                f"Fallo en descubrimiento por Sitemap para {domain}.",
                sheet_id=sheet_id, run_id=run_id
            )

        all_items.extend(sitemap_items)

        # Phase B: RSS fallback
        if len(all_items) == 0 and method in ("auto", "rss"):
            _log("Sitemaps yielded 0 URLs. Trying RSS paths...")
            rss_paths_to_try = []
            config_rss = domain_config.get("rss_url") or ""
            if config_rss:
                rss_paths_to_try.append(config_rss)
            
            common_rss = [
                f"https://{domain}/feed/",
                f"https://{domain}/rss/",
                f"https://{domain}/feed.xml",
                f"https://{domain}/rss.xml",
                f"https://{domain}/atom.xml",
            ]
            for path in common_rss:
                if path not in rss_paths_to_try:
                    rss_paths_to_try.append(path)
                    
            for r_url in rss_paths_to_try:
                _log(f"Trying RSS feed: {r_url}")
                r_rss = _get(r_url, timeout=timeout)
                if r_rss:
                    try:
                        rss_items = _parse_rss(r_rss.text, max_urls)
                        if rss_items:
                            all_items.extend(rss_items)
                            rss_contacted.append(r_url)
                            discovery_method = "rss"
                            _log(f"RSS feed {r_url} succeeded: found {len(rss_items)} entries")
                            
                            # Log SOURCE_RSS_DISCOVERED
                            logger_service.log(
                                "INFO", "SOURCE_RSS_DISCOVERED",
                                f"RSS descubierta e indexada para {domain}: {r_url} ({len(rss_items)} URLs encontradas)",
                                sheet_id=sheet_id, run_id=run_id
                            )
                            break
                    except Exception as e:
                        last_error_msg = f"RSS parse error on {r_url}: {e}"
                        errors.append(f"rss_parse_error_{r_url}")
                else:
                    errors.append(f"rss_not_retrieved_{r_url}")

        if method in ("auto", "rss") and method != "auto" and len(all_items) == 0:
            logger_service.log(
                "WARNING", "SOURCE_DISCOVERY_RSS_FAILED",
                f"Fallo en descubrimiento por RSS para {domain}.",
                sheet_id=sheet_id, run_id=run_id
            )
        elif method == "auto" and len(sitemap_items) == 0 and len(all_items) == 0:
            logger_service.log(
                "WARNING", "SOURCE_DISCOVERY_RSS_FAILED",
                f"Fallo en descubrimiento por RSS para {domain}.",
                sheet_id=sheet_id, run_id=run_id
            )

        # Phase C: WordPress REST API fallback
        if len(all_items) == 0 and method in ("auto", "wordpress"):
            _log("Sitemaps and RSS yielded 0 URLs. Trying WordPress REST API...")
            try:
                wp_url = f"https://{domain}/wp-json/wp/v2/posts?per_page=100"
                r_wp = _get(wp_url, timeout=timeout)
                if r_wp and r_wp.status_code == 200:
                    data = r_wp.json()
                    if isinstance(data, list):
                        wp_items = []
                        for post in data:
                            item_url = post.get("link") or post.get("url")
                            if not item_url:
                                continue
                            title_obj = post.get("title")
                            title_str = title_obj.get("rendered") if isinstance(title_obj, dict) else title_obj
                            
                            wp_items.append({
                                "url": _normalize_url(item_url),
                                "title": title_str or "",
                                "pub_date": post.get("date", ""),
                                "provider": "wordpress_rest"
                            })
                        if wp_items:
                            all_items.extend(wp_items)
                            discovery_method = "wordpress_rest"
                            _log(f"WordPress REST API succeeded: found {len(wp_items)} posts")
            except Exception as e:
                errors.append(f"wordpress_rest_failed: {e}")

        if method in ("auto", "wordpress") and len(all_items) == 0:
            logger_service.log(
                "WARNING", "SOURCE_DISCOVERY_WORDPRESS_FAILED",
                f"Fallo en descubrimiento por WordPress REST para {domain}.",
                sheet_id=sheet_id, run_id=run_id
            )

        # Phase D: crawl_seed
        if len(all_items) == 0 and method in ("auto", "crawl_seed"):
            seed_url = domain_config.get("seed_url") or f"https://{domain}"
            _log(f"Trying crawl_seed with URL: {seed_url}")
            r_seed = _get(seed_url, timeout=timeout)
            if r_seed:
                try:
                    from bs4 import BeautifulSoup
                    from urllib.parse import urljoin, urlparse
                    soup = BeautifulSoup(r_seed.text, "html.parser")
                    links_found = []
                    seed_parsed = urlparse(seed_url)
                    
                    for a_tag in soup.find_all("a", href=True):
                        href = a_tag["href"].strip()
                        if not href:
                            continue
                        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                            continue
                            
                        resolved_url = urljoin(seed_url, href)
                        resolved_parsed = urlparse(resolved_url)
                        
                        # Verify same domain
                        res_domain = resolved_parsed.netloc.lower().replace("www.", "")
                        seed_domain = seed_parsed.netloc.lower().replace("www.", "")
                        if res_domain != seed_domain:
                            continue
                            
                        # Exclude obvious non-article pages/assets
                        exclude_paths = ["/tag/", "/category/", "/login", "/privacy", "/cookies", "/search", "/feed", "/rss", "/wp-admin", "/contacto", "/sobre-nosotros", "/about"]
                        if any(exp in resolved_parsed.path.lower() for exp in exclude_paths):
                            continue
                            
                        # Verify it's a cultural/article URL using standard filters
                        if not _is_cultural_url(resolved_url):
                            continue
                            
                        normalized = _normalize_url(resolved_url)
                        # Avoid duplicates
                        if normalized not in [x["url"] for x in links_found]:
                            link_title = a_tag.get_text().strip() or _title_from_slug(normalized)
                            links_found.append({
                                "url": normalized,
                                "title": link_title,
                                "pub_date": "",
                                "provider": "crawl_seed"
                            })
                            
                    if links_found:
                        all_items.extend(links_found[:max_urls])
                        discovery_method = "crawl_seed"
                        _log(f"crawl_seed succeeded: found {len(links_found)} links")
                        
                        logger_service.log(
                            "INFO", "SOURCE_CRAWL_SEED_DISCOVERED",
                            f"crawl_seed exitoso para {domain} usando {seed_url} ({len(links_found)} URLs encontradas)",
                            sheet_id=sheet_id, run_id=run_id
                        )
                except Exception as e:
                    last_error_msg = f"crawl_seed parse error on {seed_url}: {e}"
                    errors.append(f"crawl_seed_parse_error_{seed_url}")
            else:
                last_error_msg = f"crawl_seed {seed_url} could not be retrieved"
                errors.append(f"crawl_seed_not_retrieved_{seed_url}")

        if method == "crawl_seed" and len(all_items) == 0:
            logger_service.log(
                "WARNING", "SOURCE_DISCOVERY_CRAWL_SEED_FAILED",
                f"Fallo en descubrimiento por crawl_seed para {domain}.",
                sheet_id=sheet_id, run_id=run_id
            )

        # Deduplicate by URL
        seen: set = set()
        unique_items: List[Dict[str, Any]] = []
        for item in all_items:
            u = item["url"]
            if u not in seen:
                seen.add(u)
                unique_items.append(item)

        # 3. Store in SQLite
        enrich_enabled = config.get("ENRICH_INDEXED_URLS", settings.ENRICH_INDEXED_URLS)
        if isinstance(enrich_enabled, str):
            enrich_enabled = enrich_enabled.lower() == "true"
        else:
            enrich_enabled = bool(enrich_enabled)

        enrich_max = int(config.get("DOMAIN_INDEX_ENRICH_MAX_PER_DOMAIN", settings.DOMAIN_INDEX_ENRICH_MAX_PER_DOMAIN))
        enrich_timeout = int(config.get("DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS", settings.DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS))
        
        discover_links_enabled = config.get("DISCOVER_INTERNAL_ARTICLE_LINKS", settings.DISCOVER_INTERNAL_ARTICLE_LINKS)
        if isinstance(discover_links_enabled, str):
            discover_links_enabled = discover_links_enabled.lower() == "true"
        else:
            discover_links_enabled = bool(discover_links_enabled)
            
        max_links_per_page = int(config.get("DOMAIN_INDEX_MAX_INTERNAL_LINKS_PER_PAGE", settings.DOMAIN_INDEX_MAX_INTERNAL_LINKS_PER_PAGE))
        
        _log(f"Metadata enrichment configuration: enabled={enrich_enabled}, max={enrich_max}, timeout={enrich_timeout}")
        _log(f"Link discovery configuration: enabled={discover_links_enabled}, max_per_page={max_links_per_page}")

        stored = 0
        enrich_count = 0
        
        # Populate seen set with initial crawled URLs
        seen_urls = set(x["url"] for x in unique_items)
        
        # Report initially found URLs
        if on_progress:
            on_progress(len(unique_items), 0, 0)
            
        loop_idx = 0
        while loop_idx < len(unique_items) and loop_idx < max_urls:
            item = unique_items[loop_idx]
            loop_idx += 1
            url = item["url"]
            
            # Check if it has a title. If not, generate fallback title from slug
            if not item.get("title"):
                item["title"] = _title_from_slug(url)
                
            # Check if existing URL in SQLite already has full metadata
            existing = None
            try:
                existing = cache_service.get_url_by_url(url)
            except Exception as e:
                logger.debug(f"Error querying existing URL {url}: {e}")

            needs_enrichment = False
            if enrich_enabled and enrich_count < enrich_max:
                if not existing:
                    needs_enrichment = True
                else:
                    ext_title = existing.get("title") or ""
                    ext_snippet = existing.get("snippet") or ""
                    fallback_title = _title_from_slug(url)
                    if not ext_title or not ext_snippet or ext_title == fallback_title:
                        needs_enrichment = True

            if needs_enrichment:
                _log(f"Enriching metadata for: {url} ({enrich_count+1}/{enrich_max})")
                meta = _enrich_page_metadata(
                    url,
                    timeout=enrich_timeout,
                    discover_links=discover_links_enabled,
                    max_links=max_links_per_page
                )
                if meta.get("title"):
                    item["title"] = meta["title"]
                if meta.get("snippet"):
                    item["snippet"] = meta["snippet"]
                enrich_count += 1
                
                # Append discovered links to the unique_items list
                for dl in meta.get("discovered_links", []):
                    dl_url = dl["url"]
                    if dl_url not in seen_urls:
                        seen_urls.add(dl_url)
                        try:
                            # Avoid appending if it's already in SQLite
                            if cache_service.get_url_by_url(dl_url):
                                continue
                        except Exception:
                            pass
                        
                        unique_items.append({
                            "url": dl_url,
                            "title": dl["title"],
                            "snippet": "",
                            "url_normalized": dl_url,
                            "pub_date": "",
                            "source_type": "internal_link"
                        })
                        if on_progress:
                            on_progress(1, 0, 0)

            try:
                is_new = cache_service.upsert_url(
                    domain=domain,
                    url=url,
                    url_normalized=item.get("url_normalized", ""),
                    title=item.get("title", ""),
                    snippet=item.get("snippet", ""),
                    pub_date=item.get("pub_date", ""),
                    source_type=item.get("source_type", "sitemap"),
                )
                is_new_flag = bool(is_new)
                if is_new_flag:
                    stored += 1
                if on_progress:
                    on_progress(0, 1 if is_new_flag else 0, 1 if needs_enrichment else 0)
            except Exception as e:
                logger.warning(f"DOMAIN_INDEX upsert error for {url}: {e}")

        # Write DOMAIN_SOURCE_DISCOVERY log
        from app.services.logger_service import logger_service
        logger_service.log(
            level="INFO",
            action="DOMAIN_SOURCE_DISCOVERY",
            message=f"Discovery completed for domain {domain} via {discovery_method}",
            isbn="",
            detail=json.dumps({
                "domain": domain,
                "robots_found": robots_found,
                "sitemaps_found": sitemaps_contacted,
                "rss_found": rss_contacted,
                "discovery_method": discovery_method,
                "last_error": last_error_msg
            }),
            sheet_id=sheet_id,
            run_id=run_id
        )

        # Store domain status in SQLite domain_status table
        if len(unique_items) == 0:
            if "no_urls_found" not in errors:
                errors.append("no_urls_found")

        # Store domain status in SQLite domain_status table
        if len(unique_items) == 0:
            logger_service.log(
                "WARNING", "SOURCE_DISCOVERY_COMPLETED_WITH_ZERO_URLS",
                f"Descubrimiento completado con 0 URLs para el dominio {domain}",
                sheet_id=sheet_id, run_id=run_id
            )
            logger_service.log(
                "WARNING", "SOURCE_DISCOVERY_NO_URLS",
                f"No se encontraron URLs para el dominio {domain}: sitemap no disponible, RSS no disponible, WordPress REST falló.",
                sheet_id=sheet_id, run_id=run_id
            )
            if "no_urls_found" not in errors:
                errors.append("no_urls_found")

        try:
            cache_service.upsert_domain_status(
                domain=domain,
                urls_count=len(unique_items),
                errors_count=len(errors),
                last_discovery_method=discovery_method,
                last_error=last_error_msg
            )
        except Exception as db_err:
            logger.warning(f"Could not upsert domain status to SQLite for {domain}: {db_err}")

        # Log SOURCE_INDEX_DOMAIN_SUMMARY
        logger_service.log(
            level="INFO",
            action="SOURCE_INDEX_DOMAIN_SUMMARY",
            message=f"Resumen de indexación para {domain}: encontradas={len(unique_items)}, almacenadas={stored}, enriquecidas={enrich_count}",
            isbn="",
            detail=json.dumps({
                "domain": domain,
                "found_count": len(unique_items),
                "stored_count": stored,
                "enriched_count": enrich_count
            }),
            sheet_id=sheet_id,
            run_id=run_id
        )

        from app.services.sheets_service import get_now_madrid_str
        last_activity_str = get_now_madrid_str()

        _log(f"Done: {len(unique_items)} URLs found, {stored} new stored, {len(errors)} errors")
        return {
            "domain": domain,
            "row_index": domain_config.get("row_index"),
            "urls_found": len(unique_items),
            "urls_stored": stored,
            "urls_enriched": enrich_count,
            "errors": errors,
            "skipped": False,
            "last_discovery_method": discovery_method,
            "last_error": last_error_msg,
            "last_activity": last_activity_str
        }

    def index_all(
        self,
        sources: List[Dict[str, Any]],
        config: Dict[str, Any],
        force_refresh: bool = False,
        log_fn: Optional[Callable] = None,
        run_id: str = "",
        sheet_id: str = "",
        on_domain_start: Optional[Callable[[str], None]] = None,
        on_domain_complete: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_progress: Optional[Callable[[int, int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Index all active sources. Returns list of per-domain stats."""
        results = []
        for source in sources:
            if not source.get("active", True):
                continue
            domain = source.get("domain", "")
            if on_domain_start and domain:
                on_domain_start(domain)
            try:
                stats = self.index_domain(
                    source,
                    config,
                    log_fn=log_fn,
                    force_refresh=force_refresh,
                    on_progress=on_progress,
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                stats["last_indexed"] = datetime.datetime.utcnow().isoformat()
                results.append(stats)
                if on_domain_complete:
                    on_domain_complete(stats)
            except Exception as e:
                logger.error(f"DOMAIN_INDEX error indexing {domain}: {e}")
                err_stats = {
                    "domain": domain,
                    "row_index": source.get("row_index"),
                    "urls_found": 0,
                    "urls_stored": 0,
                    "urls_enriched": 0,
                    "errors": [str(e)],
                    "skipped": False,
                    "last_activity": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                }
                results.append(err_stats)
                if on_domain_complete:
                    on_domain_complete(err_stats)
        return results



domain_indexer = DomainIndexer()
