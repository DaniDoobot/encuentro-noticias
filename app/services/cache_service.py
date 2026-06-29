"""
cache_service.py — SQLite-backed cache for indexed domain URLs.

Schema:
    indexed_urls(id, domain, url, url_normalized, title, snippet,
                 pub_date, source_type, indexed_at, content_hash, last_checked_at)
"""
import sqlite3
import datetime
import hashlib
import os
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("encuentro-noticias")


class CacheService:
    def __init__(self):
        self._db_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def init_db(self, db_path: str) -> None:
        """Create DB and tables if they don't exist yet."""
        self._db_path = db_path
        dir_name = os.path.dirname(db_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS indexed_urls (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain           TEXT NOT NULL,
                    url              TEXT NOT NULL UNIQUE,
                    url_normalized   TEXT,
                    title            TEXT,
                    snippet          TEXT,
                    pub_date         TEXT,
                    source_type      TEXT,
                    indexed_at       TEXT,
                    content_hash     TEXT,
                    last_checked_at  TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_iu_domain ON indexed_urls(domain);
                CREATE INDEX IF NOT EXISTS idx_iu_url    ON indexed_urls(url);

                CREATE TABLE IF NOT EXISTS domain_status (
                    domain                  TEXT PRIMARY KEY,
                    urls_count              INTEGER DEFAULT 0,
                    last_indexed            TEXT,
                    errors_count            INTEGER DEFAULT 0,
                    last_discovery_method   TEXT,
                    last_error              TEXT
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        if not self._db_path:
            raise RuntimeError("CacheService not initialised — call init_db() first.")
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert_url(
        self,
        domain: str,
        url: str,
        url_normalized: str = "",
        title: str = "",
        snippet: str = "",
        pub_date: str = "",
        source_type: str = "sitemap",
    ) -> bool:
        """Insert or update a URL. Returns True if it was a new insertion."""
        now = datetime.datetime.utcnow().isoformat()
        content_hash = hashlib.md5((title + snippet).encode()).hexdigest()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM indexed_urls WHERE url = ?", (url,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE indexed_urls
                       SET title=?, snippet=?, pub_date=?, source_type=?,
                           content_hash=?, last_checked_at=?
                       WHERE url=?""",
                    (title, snippet, pub_date, source_type, content_hash, now, url),
                )
                return False
            else:
                conn.execute(
                    """INSERT INTO indexed_urls
                       (domain, url, url_normalized, title, snippet, pub_date,
                        source_type, indexed_at, content_hash, last_checked_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (domain, url, url_normalized, title, snippet, pub_date,
                     source_type, now, content_hash, now),
                )
                return True

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_url_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Fetch a single indexed URL from SQLite."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM indexed_urls WHERE url = ?", (url,)
            ).fetchone()
        return dict(row) if row else None

    def search_by_text(
        self,
        terms: List[str],
        domain_filter: Optional[List[str]] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Search indexed_urls where title OR snippet OR url contains ANY of the
        given terms (case-insensitive LIKE).  Optionally restrict to domains.
        Returns list of dicts.
        """
        if not terms:
            return []
        conditions = []
        params: List[Any] = []
        for term in terms:
            conditions.append("(LOWER(title) LIKE ? OR LOWER(snippet) LIKE ? OR LOWER(url) LIKE ?)")
            like = f"%{term.lower()}%"
            params.extend([like, like, like])

        where = " OR ".join(conditions)

        if domain_filter:
            placeholders = ",".join("?" * len(domain_filter))
            where = f"({where}) AND domain IN ({placeholders})"
            params.extend(domain_filter)

        sql = f"SELECT * FROM indexed_urls WHERE {where} LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_domain_stats(self, domain: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt, MAX(indexed_at) as last_indexed "
                "FROM indexed_urls WHERE domain=?",
                (domain,),
            ).fetchone()
        return {
            "domain": domain,
            "urls": row["cnt"] if row else 0,
            "last_indexed": row["last_indexed"] if row else None,
        }

    def get_all_domains_stats(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT domain, COUNT(*) as cnt, MAX(indexed_at) as last_indexed "
                "FROM indexed_urls GROUP BY domain ORDER BY cnt DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_total_urls(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM indexed_urls").fetchone()
        return row["cnt"] if row else 0

    def needs_refresh(self, domain: str, refresh_days: int) -> bool:
        """True if domain has never been indexed or was last indexed > refresh_days ago."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(indexed_at) as last FROM indexed_urls WHERE domain=?",
                (domain,),
            ).fetchone()
        if not row or not row["last"]:
            return True
        try:
            last_dt = datetime.datetime.fromisoformat(row["last"])
            return (datetime.datetime.utcnow() - last_dt).days >= refresh_days
        except Exception:
            return True

    def upsert_domain_status(
        self,
        domain: str,
        urls_count: int,
        errors_count: int,
        last_discovery_method: str,
        last_error: str
    ) -> None:
        """Insert or update discovery and status info for a domain."""
        now = datetime.datetime.utcnow().isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT domain FROM domain_status WHERE domain = ?", (domain,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE domain_status
                       SET urls_count=?, last_indexed=?, errors_count=?,
                           last_discovery_method=?, last_error=?
                       WHERE domain=?""",
                    (urls_count, now, errors_count, last_discovery_method, last_error, domain),
                )
            else:
                conn.execute(
                    """INSERT INTO domain_status
                       (domain, urls_count, last_indexed, errors_count, last_discovery_method, last_error)
                       VALUES (?,?,?,?,?,?)""",
                    (domain, urls_count, now, errors_count, last_discovery_method, last_error),
                )

    def get_domain_status(self, domain: str) -> Optional[Dict[str, Any]]:
        """Get discovery status for a single domain."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM domain_status WHERE domain=?", (domain,)
            ).fetchone()
        return dict(row) if row else None

    def get_all_domain_statuses(self) -> List[Dict[str, Any]]:
        """Get discovery statuses for all domains."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM domain_status").fetchall()
        return [dict(r) for r in rows]


cache_service = CacheService()

