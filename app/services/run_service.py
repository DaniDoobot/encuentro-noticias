import uuid
import datetime
import json
import time
from typing import Dict, Any, List, Optional, Set, Tuple
import threading
from urllib.parse import urlparse

from app.config import settings
from app.services.sheets_service import sheets_service
from app.services.query_builder import query_builder
from app.services.search_service import search_service, is_true
from app.services.article_extractor import article_extractor
from app.services.openai_analyzer import openai_analyzer
from app.services.deduplicator import deduplicator
from app.services.logger_service import logger_service
from app.services.source_discovery import source_discovery
from app.services.cache_service import cache_service

# In-memory storage for runs
current_runs: Dict[str, Dict[str, Any]] = {}

class RunService:
    def get_run_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        return current_runs.get(run_id)

    def trigger_run(self, limit_books: int = 10, dry_run: bool = False, date_min: Optional[str] = None, date_max: Optional[str] = None, include_unknown_dates: Optional[bool] = None) -> str:
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        current_runs[run_id] = {
            "run_id": run_id,
            "status": "pending",
            "books_total": 0,
            "books_processed": 0,
            "books_completed": 0,
            "books_failed": 0,
            "books_no_results": 0,
            "message": "Iniciando ejecución...",
            "logs": [],
            "books_details": []
        }
        
        thread = threading.Thread(
            target=self.execute_run,
            args=(run_id, limit_books, dry_run, date_min, date_max, include_unknown_dates)
        )
        thread.daemon = True
        thread.start()
        
        return run_id

    def trigger_single_book_run(self, isbn: str, dry_run: bool = False, date_min: Optional[str] = None, date_max: Optional[str] = None, include_unknown_dates: Optional[bool] = None) -> str:
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        current_runs[run_id] = {
            "run_id": run_id,
            "status": "pending",
            "books_total": 1,
            "books_processed": 0,
            "books_completed": 0,
            "books_failed": 0,
            "books_no_results": 0,
            "message": f"Iniciando ejecución para ISBN {isbn}...",
            "logs": [],
            "books_details": []
        }
        
        thread = threading.Thread(
            target=self.execute_single_book,
            args=(run_id, isbn, dry_run, date_min, date_max, include_unknown_dates)
        )
        thread.daemon = True
        thread.start()
        
        return run_id

    def rebuild_dedupe_hashes(self, sheet_id: str) -> int:
        reviews = sheets_service.get_all_reviews(sheet_id)
        updates = []
        
        for idx, row in enumerate(reviews, start=2):
            isbn = str(row.get("ISBN", "")).strip()
            url = str(row.get("URL", "")).strip()
            existing_hash = str(row.get("Hash deduplicación", "")).strip()
            
            if isbn and url:
                calculated_hash = deduplicator.get_primary_hash(isbn, url)
                if existing_hash != calculated_hash:
                    updates.append((idx, calculated_hash))
                    
        if updates:
            sheets_service.update_reviews_hashes(sheet_id, updates)
            
        return len(updates)

    def _add_in_memory_log(self, run_id: str, level: str, action: str, message: str, isbn: str = "", detail: str = ""):
        if run_id in current_runs:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            current_runs[run_id]["logs"].append({
                "timestamp": timestamp,
                "level": level,
                "action": action,
                "message": message,
                "isbn": isbn,
                "detail": detail
            })

    def execute_run(self, run_id: str, limit_books: int, dry_run: bool, date_min: Optional[str] = None, date_max: Optional[str] = None, include_unknown_dates: Optional[bool] = None):
        sheet_id = settings.GOOGLE_SHEET_ID
        log_prefix = "[PRUEBA] " if dry_run else ""
        self._add_in_memory_log(run_id, "INFO", "RUN_START", f"{log_prefix}Iniciando run global (limit_books={limit_books}, dry_run={dry_run})")
        logger_service.log("INFO", "RUN_START", f"{log_prefix}Iniciando ejecución {run_id}", sheet_id=sheet_id, run_id=run_id)

        try:
            current_runs[run_id]["status"] = "running"
            search_service.reset_blocked_providers()
            
            # 1. Fetch configs from Google Sheets Config sheet
            run_config = sheets_service.get_config_dict(sheet_id)
            max_books = min(limit_books, run_config["MAX_BOOKS_PER_RUN"])
            max_pages = run_config["MAX_SEARCH_PAGES_PER_QUERY"]
            max_candidates = run_config["MAX_CANDIDATES_PER_BOOK"]
            min_score = run_config["MIN_MATCH_SCORE"]
            openai_model = run_config["OPENAI_MODEL"]
            
            review_domains_str = run_config.get("REVIEW_DOMAINS", "")
            review_domains = [d.strip() for d in review_domains_str.split(",") if d.strip()]
            
            self._add_in_memory_log(run_id, "INFO", "CONFIG_LOADED", f"Configuraciones leídas: Max libros={max_books}, Min score={min_score}, Dominios específicos={len(review_domains)}")
            
            # 2. Get pending books
            pending_books = sheets_service.get_pending_books(sheet_id, limit=max_books)
            total_books = len(pending_books)
            current_runs[run_id]["books_total"] = total_books
            
            if total_books == 0:
                msg = "No se encontraron libros con estado 'pendiente' en la pestaña 'Libros'."
                current_runs[run_id]["status"] = "completed"
                current_runs[run_id]["message"] = msg
                self._add_in_memory_log(run_id, "INFO", "RUN_END", msg)
                logger_service.log("INFO", "RUN_END", msg, sheet_id=sheet_id, run_id=run_id)
                return

            # 3. Load all existing reviews for deduplication
            self._add_in_memory_log(run_id, "INFO", "DEDUPE_INIT", "Cargando reseñas existentes para deduplicación...")
            existing_reviews = sheets_service.get_all_reviews(sheet_id)
            existing_hashes = deduplicator.extract_hashes_from_reviews(existing_reviews)
            existing_secondary_keys = deduplicator.extract_secondary_keys_from_reviews(existing_reviews)
            
            self._add_in_memory_log(run_id, "INFO", "DEDUPE_INIT", f"Reseñas cargadas: {len(existing_reviews)}. Hashes únicos: {len(existing_hashes)}")

            for book in pending_books:
                isbn = book["isbn"]
                title = book["title"]
                author = book["author"]
                row_index = book["row_index"]

                try:
                    final_status = self._process_book(
                        run_id=run_id,
                        sheet_id=sheet_id,
                        row_index=row_index,
                        isbn=isbn,
                        title=title,
                        author=author,
                        max_pages=max_pages,
                        max_candidates=max_candidates,
                        min_score=min_score,
                        openai_model=openai_model,
                        existing_hashes=existing_hashes,
                        existing_secondary_keys=existing_secondary_keys,
                        review_domains=review_domains,
                        run_config=run_config,
                        dry_run=dry_run,
                        date_min=date_min,
                        date_max=date_max,
                        include_unknown_dates=include_unknown_dates
                    )
                    
                    if final_status == "completado":
                        current_runs[run_id]["books_completed"] += 1
                    elif final_status == "sin_resultados":
                        current_runs[run_id]["books_no_results"] += 1
                    elif final_status == "error":
                        current_runs[run_id]["books_failed"] += 1
                except Exception as e:
                    logger_service.log("ERROR", "BOOK_PROCESS_FAIL", f"Error procesando libro '{title}': {e}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                    self._add_in_memory_log(run_id, "ERROR", "BOOK_PROCESS_FAIL", str(e), isbn=isbn)
                    current_runs[run_id]["books_failed"] += 1
                    
                    book_detail = {
                        "isbn": isbn,
                        "title": title,
                        "domain_index_candidates": 0,
                        "google_news_candidates": 0,
                        "internal_search_candidates": 0,
                        "total_before_dedup": 0,
                        "total_after_dedup": 0,
                        "accepted_by_ai": 0,
                        "final_status": "error"
                    }
                    if run_id in current_runs:
                        if "books_details" not in current_runs[run_id]:
                            current_runs[run_id]["books_details"] = []
                        current_runs[run_id]["books_details"].append(book_detail)
                    
                    if not dry_run:
                        try:
                            sheets_service.update_book_status(
                                sheet_id=sheet_id,
                                row_index=row_index,
                                status="error",
                                last_run=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                reviews_found=0,
                                observations=f"Error de proceso: {str(e)}"
                            )
                        except Exception as e_sheet:
                            logger_service.log("ERROR", "SHEET_UPDATE_FAIL", f"Fallo al marcar estado 'error' en Libros: {e_sheet}", isbn=isbn, sheet_id=sheet_id)
                finally:
                    current_runs[run_id]["books_processed"] += 1
            
            current_runs[run_id]["status"] = "completed"
            current_runs[run_id]["message"] = f"Ejecución completada. Procesados {current_runs[run_id]['books_processed']} libros."
            self._add_in_memory_log(run_id, "INFO", "RUN_END", f"Ejecución global completada. Completados={current_runs[run_id]['books_completed']}, Sin resultados={current_runs[run_id]['books_no_results']}, Fallidos={current_runs[run_id]['books_failed']}")
            logger_service.log("INFO", "RUN_END", f"Ejecución global completada. Completados={current_runs[run_id]['books_completed']}, Sin resultados={current_runs[run_id]['books_no_results']}, Fallidos={current_runs[run_id]['books_failed']}", sheet_id=sheet_id, run_id=run_id)

        except Exception as e:
            current_runs[run_id]["status"] = "failed"
            current_runs[run_id]["message"] = f"Error en la ejecución: {str(e)}"
            self._add_in_memory_log(run_id, "ERROR", "RUN_ERROR", str(e))
            logger_service.log("ERROR", "RUN_ERROR", f"Error general: {e}", sheet_id=sheet_id, run_id=run_id)

    def execute_single_book(self, run_id: str, isbn: str, dry_run: bool, date_min: Optional[str] = None, date_max: Optional[str] = None, include_unknown_dates: Optional[bool] = None):
        sheet_id = settings.GOOGLE_SHEET_ID
        log_prefix = "[PRUEBA] " if dry_run else ""
        self._add_in_memory_log(run_id, "INFO", "RUN_START", f"{log_prefix}Iniciando run individual para ISBN {isbn} (dry_run={dry_run})")
        logger_service.log("INFO", "RUN_START", f"{log_prefix}Iniciando ejecución individual {run_id} para ISBN {isbn}", sheet_id=sheet_id, run_id=run_id)

        try:
            current_runs[run_id]["status"] = "running"
            search_service.reset_blocked_providers()
            
            # Fetch config
            run_config = sheets_service.get_config_dict(sheet_id)
            max_pages = run_config["MAX_SEARCH_PAGES_PER_QUERY"]
            max_candidates = run_config["MAX_CANDIDATES_PER_BOOK"]
            min_score = run_config["MIN_MATCH_SCORE"]
            openai_model = run_config["OPENAI_MODEL"]
            
            review_domains_str = run_config.get("REVIEW_DOMAINS", "")
            review_domains = [d.strip() for d in review_domains_str.split(",") if d.strip()]

            # Get book by ISBN
            book = sheets_service.get_book_by_isbn(sheet_id, isbn)
            if not book:
                msg = f"No se encontró ningún libro con ISBN {isbn} en la pestaña 'Libros'."
                current_runs[run_id]["status"] = "completed"
                current_runs[run_id]["message"] = msg
                self._add_in_memory_log(run_id, "WARNING", "BOOK_NOT_FOUND", msg)
                logger_service.log("WARNING", "BOOK_NOT_FOUND", msg, isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                return

            existing_reviews = sheets_service.get_all_reviews(sheet_id)
            existing_hashes = deduplicator.extract_hashes_from_reviews(existing_reviews)
            existing_secondary_keys = deduplicator.extract_secondary_keys_from_reviews(existing_reviews)

            try:
                final_status = self._process_book(
                    run_id=run_id,
                    sheet_id=sheet_id,
                    row_index=book["row_index"],
                    isbn=book["isbn"],
                    title=book["title"],
                    author=book["author"],
                    max_pages=max_pages,
                    max_candidates=max_candidates,
                    min_score=min_score,
                    openai_model=openai_model,
                    existing_hashes=existing_hashes,
                    existing_secondary_keys=existing_secondary_keys,
                    review_domains=review_domains,
                    run_config=run_config,
                    dry_run=dry_run,
                    date_min=date_min,
                    date_max=date_max,
                    include_unknown_dates=include_unknown_dates
                )
                
                if final_status == "completado":
                    current_runs[run_id]["books_completed"] += 1
                elif final_status == "sin_resultados":
                    current_runs[run_id]["books_no_results"] += 1
                elif final_status == "error":
                    current_runs[run_id]["books_failed"] += 1
            except Exception as e:
                logger_service.log("ERROR", "BOOK_PROCESS_FAIL", f"Error procesando libro '{book['title']}': {e}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                self._add_in_memory_log(run_id, "ERROR", "BOOK_PROCESS_FAIL", str(e), isbn=isbn)
                current_runs[run_id]["books_failed"] += 1
                
                book_detail = {
                    "isbn": isbn,
                    "title": book["title"] if book else "",
                    "domain_index_candidates": 0,
                    "google_news_candidates": 0,
                    "internal_search_candidates": 0,
                    "total_before_dedup": 0,
                    "total_after_dedup": 0,
                    "accepted_by_ai": 0,
                    "final_status": "error"
                }
                if run_id in current_runs:
                    if "books_details" not in current_runs[run_id]:
                        current_runs[run_id]["books_details"] = []
                    current_runs[run_id]["books_details"].append(book_detail)
                
                if not dry_run:
                    try:
                        sheets_service.update_book_status(
                            sheet_id=sheet_id,
                            row_index=book["row_index"],
                            status="error",
                            last_run=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            reviews_found=0,
                            observations=f"Error de proceso: {str(e)}"
                        )
                    except Exception as e_sheet:
                        logger_service.log("ERROR", "SHEET_UPDATE_FAIL", f"Fallo al marcar estado 'error' en Libros: {e_sheet}", isbn=isbn, sheet_id=sheet_id)
            finally:
                current_runs[run_id]["books_processed"] = 1

            current_runs[run_id]["status"] = "completed"
            current_runs[run_id]["message"] = f"Ejecución completada para ISBN {isbn}."
            self._add_in_memory_log(run_id, "INFO", "RUN_END", f"Ejecución individual para ISBN {isbn} completada.")
            logger_service.log("INFO", "RUN_END", f"Ejecución individual para ISBN {isbn} completada.", isbn=isbn, sheet_id=sheet_id, run_id=run_id)

        except Exception as e:
            current_runs[run_id]["status"] = "failed"
            current_runs[run_id]["message"] = f"Error en la ejecución: {str(e)}"
            self._add_in_memory_log(run_id, "ERROR", "RUN_ERROR", str(e))
            logger_service.log("ERROR", "RUN_ERROR", f"Error general: {e}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)

    def _process_book(
        self,
        run_id: str,
        sheet_id: str,
        row_index: int,
        isbn: str,
        title: str,
        author: str,
        max_pages: int,
        max_candidates: int,
        min_score: int,
        openai_model: str,
        existing_hashes: Set[str],
        existing_secondary_keys: Set[str],
        review_domains: List[str] = None,
        run_config: Dict[str, Any] = None,
        dry_run: bool = False,
        date_min: Optional[str] = None,
        date_max: Optional[str] = None,
        include_unknown_dates: Optional[bool] = None
    ) -> str:
        """
        Runs the extraction and validation pipeline for a single book.
        """
        log_prefix = "[PRUEBA] " if dry_run else ""
        
        # Check if title is missing
        if not title or not title.strip():
            err_msg = "Falta título del libro"
            logger_service.log("ERROR", "BOOK_MISSING_TITLE", f"{log_prefix}{err_msg}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
            self._add_in_memory_log(run_id, "ERROR", "BOOK_MISSING_TITLE", f"{log_prefix}{err_msg}", isbn=isbn)
            if not dry_run:
                try:
                    sheets_service.update_book_status(
                        sheet_id=sheet_id,
                        row_index=row_index,
                        status="error",
                        last_run=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        reviews_found=0,
                        observations=err_msg
                    )
                except Exception as e_sheet:
                    logger_service.log("ERROR", "SHEET_UPDATE_FAIL", f"Fallo al marcar estado 'error' por falta de título: {e_sheet}", isbn=isbn, sheet_id=sheet_id)
            logger_service.flush_log_batch(sheet_id, run_id)
            return "error"

        author_str = f" por {author}" if author.strip() else ""
        logger_service.log("INFO", "BOOK_PROCESS_START", f"{log_prefix}Procesando libro: '{title}'{author_str}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
        self._add_in_memory_log(run_id, "INFO", "BOOK_PROCESS_START", f"{log_prefix}Procesando: '{title}'{author_str}", isbn=isbn)

        # Clear tracked providers used for this book
        search_service.providers_used_count.clear()

        # Load configs
        config = run_config or {}
        search_delay = float(config.get("SEARCH_DELAY_SECONDS", settings.SEARCH_DELAY_SECONDS))
        max_queries = int(config.get("MAX_QUERIES_PER_BOOK", settings.MAX_QUERIES_PER_BOOK))

        # Resolve date filter parameters
        def parse_iso_date(d_str: Optional[str]) -> Optional[datetime.date]:
            if not d_str:
                return None
            try:
                return datetime.datetime.strptime(str(d_str).strip()[:10], "%Y-%m-%d").date()
            except Exception:
                return None

        default_min_str = config.get("DEFAULT_DATE_MIN", settings.DEFAULT_DATE_MIN)
        default_max_str = config.get("DEFAULT_DATE_MAX", settings.DEFAULT_DATE_MAX)
        
        if "DEFAULT_INCLUDE_UNKNOWN_DATES" in config:
            default_include_unknown = is_true(config["DEFAULT_INCLUDE_UNKNOWN_DATES"])
        else:
            default_include_unknown = settings.DEFAULT_INCLUDE_UNKNOWN_DATES
            
        final_min_str = date_min if date_min is not None else default_min_str
        final_max_str = date_max if date_max is not None else default_max_str
        final_include_unknown = include_unknown_dates if include_unknown_dates is not None else default_include_unknown
        
        parsed_min = parse_iso_date(final_min_str)
        parsed_max = parse_iso_date(final_max_str)

        # Clear blocked providers if BLOCK_PROVIDER_FOR_FULL_RUN is false
        block_provider_val = config.get("BLOCK_PROVIDER_FOR_FULL_RUN", settings.BLOCK_PROVIDER_FOR_FULL_RUN)
        if not is_true(block_provider_val):
            search_service.blocked_providers.clear()

        # Mark as processing in sheets if not dry run
        if not dry_run:
            try:
                sheets_service.update_book_status(
                    sheet_id=sheet_id,
                    row_index=row_index,
                    status="procesando",
                    last_run=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    reviews_found=0,
                    observations="Procesando..."
                )
            except Exception as e:
                logger_service.log("ERROR", "BOOK_STATUS_UPDATE_FAIL", f"No se pudo actualizar el estado del libro a procesando: {e}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                self._add_in_memory_log(run_id, "ERROR", "BOOK_STATUS_UPDATE_FAIL", f"Fallo al marcar procesando: {e}", isbn=isbn)

        # 1. Generate queries categorized into 3 levels
        queries_dict = query_builder.build_queries(title, author, isbn, review_domains=review_domains)
        prioritarias = queries_dict["prioritarias"]
        apoyo = queries_dict["apoyo"]
        dominios = queries_dict["dominios"]
        
        self._add_in_memory_log(
            run_id, "INFO", "QUERIES_GENERATED", 
            f"{log_prefix}Generadas queries por nivel: Prioritarias={len(prioritarias)}, Apoyo={len(apoyo)}, Dominios={len(dominios)}", 
            isbn=isbn
        )

        # 2. Gather candidate URLs executing queries level by level
        domain_index_candidates_count = 0
        google_news_candidates_count = 0
        internal_search_candidates_count = 0

        candidate_origin: Dict[str, Dict[str, Any]] = {} # URL -> metadata dict
        queries_executed = 0
        search_mode = config.get("SEARCH_PROVIDER_MODE", settings.SEARCH_PROVIDER_MODE)

        # Check cascade search flag (from config or fallback to settings)
        cascade_search = is_true(config.get("ENABLE_CASCADE_SEARCH", settings.ENABLE_CASCADE_SEARCH))
        
        # If SEARCH_PROVIDER_MODE is domain_index_plus_news, force cascade search
        if search_mode == "domain_index_plus_news":
            cascade_search = True

        if cascade_search:
            # PHASE 1 — Domain Index
            local_matches = source_discovery.find_candidates(
                title=title,
                author=author,
                isbn=isbn,
                config=config,
            )
            for match in local_matches:
                url = match["url"]
                if url not in candidate_origin and len(candidate_origin) < max_candidates:
                    candidate_origin[url] = {
                        "query": "local_index",
                        "provider": "DomainIndex",
                        "title": match.get("title") or "",
                        "snippet": match.get("snippet") or "",
                        "position": match.get("score"),
                        "pub_date": match.get("pub_date")
                    }
                    logger_service.log(
                        level="INFO",
                        action="DOMAIN_SEARCH_MATCH",
                        message=f"{log_prefix}Match local (score={match['score']}): {url}",
                        isbn=isbn,
                        detail=f"domain={match.get('domain')} | matched={match.get('matched_fields')} | title={match.get('title','')[:80]}",
                        sheet_id=sheet_id,
                        run_id=run_id
                    )
            domain_index_candidates_count = len(candidate_origin)
            
            # PHASE 2 — Google News RSS / Búsqueda externa ligera
            rss_max = int(config.get("DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES", settings.DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES))
            rss_queries_done = 0
            google_news_candidates = 0
            for q in prioritarias:
                if rss_queries_done >= rss_max or queries_executed >= max_queries:
                    break
                if len(candidate_origin) >= max_candidates:
                    break
                if queries_executed > 0:
                    time.sleep(search_delay)
                rss_results = search_service.search_with_fallback(
                    query=q,
                    max_pages=max_pages,
                    sheet_id=sheet_id,
                    run_id=run_id,
                    isbn=isbn,
                    config={**config, "SEARCH_PROVIDER_MODE": "google_news_only"},
                    log_callback=self._add_in_memory_log
                )
                queries_executed += 1
                rss_queries_done += 1
                for item in rss_results:
                    url = item["url"]
                    if url not in candidate_origin and len(candidate_origin) < max_candidates:
                        candidate_origin[url] = {
                            "query": item.get("query") or q,
                            "provider": item.get("provider"),
                            "title": item.get("title") or "",
                            "snippet": item.get("snippet") or "",
                            "position": item.get("position"),
                            "pub_date": item.get("pub_date")
                        }
                        google_news_candidates += 1
            google_news_candidates_count = google_news_candidates

            # PHASE 3 — Internal Domain Search si hay pocos candidatos
            min_candidates_internal = int(config.get("MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH", settings.MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH))
            enable_deep_search = is_true(config.get("ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS", settings.ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS))
            enable_internal_search = is_true(config.get("ENABLE_INTERNAL_DOMAIN_SEARCH", settings.ENABLE_INTERNAL_DOMAIN_SEARCH))

            total_before_internal = len(candidate_origin)
            
            if total_before_internal >= min_candidates_internal or not enable_internal_search or not enable_deep_search:
                # Skip internal search
                logger_service.log(
                    level="INFO",
                    action="INTERNAL_SEARCH_SKIPPED_ENOUGH_CANDIDATES",
                    message=f"{log_prefix}Búsqueda interna omitida (candidatos actuales: {total_before_internal} >= {min_candidates_internal})",
                    isbn=isbn,
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                self._add_in_memory_log(
                    run_id, "INFO", "INTERNAL_SEARCH_SKIPPED_ENOUGH_CANDIDATES",
                    f"{log_prefix}Búsqueda interna omitida: candidatos={total_before_internal} >= {min_candidates_internal}",
                    isbn=isbn
                )
            else:
                # Execute Internal Domain Search
                logger_service.log(
                    level="INFO",
                    action="INTERNAL_SEARCH_STARTED_LOW_CANDIDATES",
                    message=f"{log_prefix}Iniciando búsqueda interna dedicada (candidatos actuales: {total_before_internal} < {min_candidates_internal})",
                    isbn=isbn,
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                self._add_in_memory_log(
                    run_id, "INFO", "INTERNAL_SEARCH_STARTED_LOW_CANDIDATES",
                    f"{log_prefix}Iniciando búsqueda interna: candidatos={total_before_internal} < {min_candidates_internal}",
                    isbn=isbn
                )
                
                from app.services.internal_search_provider import internal_search_provider
                from app.services.domain_indexer import _enrich_page_metadata
                
                # Fetch active sources
                try:
                    sources = sheets_service.get_active_sources(sheet_id)
                except Exception:
                    sources = []
                    
                domains_limit = int(config.get("INTERNAL_SEARCH_DOMAINS_LIMIT", settings.INTERNAL_SEARCH_DOMAINS_LIMIT))
                domains = [s["domain"] for s in sources if s.get("active", True) and s.get("domain")]
                domains = domains[:domains_limit]

                if domains:
                    new_urls_found = 0
                    for domain in domains:
                        try:
                            items = internal_search_provider.search_domain_for_book(
                                domain=domain,
                                title=title,
                                author=author,
                                isbn=isbn,
                                config=config
                            )
                            for item in items:
                                url = item["url"]
                                title_found = item.get("title") or ""
                                snippet_found = item.get("snippet") or ""
                                
                                # Store basic record in SQLite
                                cache_service.upsert_url(
                                    domain=domain,
                                    url=url,
                                    url_normalized=url,
                                    title=title_found,
                                    snippet=snippet_found,
                                    source_type="internal_search"
                                )
                                
                                # Enrich page metadata if enabled
                                enrich_enabled_val = config.get("ENRICH_INDEXED_URLS", settings.ENRICH_INDEXED_URLS)
                                enrich_enabled = is_true(enrich_enabled_val)
                                
                                if enrich_enabled:
                                    enrich_timeout = int(config.get("DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS", settings.DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS))
                                    meta = _enrich_page_metadata(url, timeout=enrich_timeout)
                                    if meta.get("title") or meta.get("snippet"):
                                        cache_service.upsert_url(
                                            domain=domain,
                                            url=url,
                                            url_normalized=url,
                                            title=meta.get("title") or title_found,
                                            snippet=meta.get("snippet") or snippet_found,
                                            source_type="internal_search"
                                        )
                                new_urls_found += 1
                        except Exception as e:
                            logger_service.log(
                                level="WARNING",
                                action="SEARCH_PROVIDER_ERROR",
                                message=f"Error en búsqueda interna del dominio {domain}: {e}",
                                isbn=isbn,
                                sheet_id=sheet_id,
                                run_id=run_id
                            )
                            
                    # Re-run SourceDiscovery to retrieve new candidates
                    if new_urls_found > 0:
                        local_matches = source_discovery.find_candidates(
                            title=title,
                            author=author,
                            isbn=isbn,
                            config=config
                        )
                        internal_candidates = 0
                        for match in local_matches:
                            url = match["url"]
                            if url not in candidate_origin and len(candidate_origin) < max_candidates:
                                candidate_origin[url] = {
                                    "query": "local_index",
                                    "provider": "DomainIndex",
                                    "title": match.get("title") or "",
                                    "snippet": match.get("snippet") or "",
                                    "position": match.get("score"),
                                    "pub_date": match.get("pub_date")
                                }
                                internal_candidates += 1
                                logger_service.log(
                                    level="INFO",
                                    action="DOMAIN_SEARCH_MATCH",
                                    message=f"{log_prefix}Match local (score={match['score']}): {url}",
                                    isbn=isbn,
                                    detail=f"domain={match.get('domain')} | matched={match.get('matched_fields')} | title={match.get('title','')[:80]}",
                                    sheet_id=sheet_id,
                                    run_id=run_id
                                )
                        internal_search_candidates_count = internal_candidates
                
                logger_service.log(
                    level="INFO",
                    action="INTERNAL_SEARCH_COMPLETED",
                    message=f"{log_prefix}Búsqueda interna finalizada. Candidatas extraídas: {internal_search_candidates_count}",
                    isbn=isbn,
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                self._add_in_memory_log(
                    run_id, "INFO", "INTERNAL_SEARCH_COMPLETED",
                    f"{log_prefix}Búsqueda interna finalizada: candidatos={internal_search_candidates_count}",
                    isbn=isbn
                )
            
            candidate_urls = list(candidate_origin.keys())

        else:
            # Normal query-loop modes (google_news_only, free_only, serpapi, dataforseo, auto)
            # LEVEL 1
            for q in prioritarias:
                if queries_executed >= max_queries or len(candidate_origin) >= max_candidates:
                    break
                if queries_executed > 0:
                    time.sleep(search_delay)
                found_items = search_service.search_with_fallback(
                    query=q, max_pages=max_pages, sheet_id=sheet_id, run_id=run_id, isbn=isbn, config=config, log_callback=self._add_in_memory_log
                )
                queries_executed += 1
                for item in found_items:
                    url = item["url"]
                    if url not in candidate_origin and len(candidate_origin) < max_candidates:
                        candidate_origin[url] = {
                            "query": item.get("query") or q, "provider": item.get("provider"), "title": item.get("title") or "", "snippet": item.get("snippet") or "", "position": item.get("position"), "pub_date": item.get("pub_date")
                        }
            google_news_candidates_count = len(candidate_origin)

            # LEVEL 2
            if len(candidate_origin) == 0:
                for q in apoyo:
                    if queries_executed >= max_queries or len(candidate_origin) >= max_candidates:
                        break
                    time.sleep(search_delay)
                    found_items = search_service.search_with_fallback(
                        query=q, max_pages=max_pages, sheet_id=sheet_id, run_id=run_id, isbn=isbn, config=config, log_callback=self._add_in_memory_log
                    )
                    queries_executed += 1
                    for item in found_items:
                        url = item["url"]
                        if url not in candidate_origin and len(candidate_origin) < max_candidates:
                            candidate_origin[url] = {
                                "query": item.get("query") or q, "provider": item.get("provider"), "title": item.get("title") or "", "snippet": item.get("snippet") or "", "position": item.get("position"), "pub_date": item.get("pub_date")
                            }
                google_news_candidates_count = len(candidate_origin)

            # LEVEL 3
            if len(candidate_origin) < 5:
                for q in dominios:
                    if queries_executed >= max_queries or len(candidate_origin) >= max_candidates:
                        break
                    time.sleep(search_delay)
                    found_items = search_service.search_with_fallback(
                        query=q, max_pages=max_pages, sheet_id=sheet_id, run_id=run_id, isbn=isbn, config=config, log_callback=self._add_in_memory_log
                    )
                    queries_executed += 1
                    for item in found_items:
                        url = item["url"]
                        if url not in candidate_origin and len(candidate_origin) < max_candidates:
                            candidate_origin[url] = {
                                "query": item.get("query") or q, "provider": item.get("provider"), "title": item.get("title") or "", "snippet": item.get("snippet") or "", "position": item.get("position"), "pub_date": item.get("pub_date")
                            }
                internal_search_candidates_count = len(candidate_origin) - google_news_candidates_count

            candidate_urls = list(candidate_origin.keys())

        # Log each candidate found with its originating query and provider
        for url, item in candidate_origin.items():
            provider_name = item["provider"]
            origin_query = item["query"]
            title = item.get("title") or ""
            snippet = item.get("snippet") or ""
            pos = item.get("position") or ""
            
            logger_service.log(
                level="INFO",
                action="CANDIDATE_FOUND",
                message=f"{log_prefix}Candidato de {provider_name}: {url}",
                isbn=isbn,
                detail=f"provider={provider_name} | query={origin_query} | url={url} | title={title} | snippet={snippet} | position={pos}",
                sheet_id=sheet_id,
                run_id=run_id
            )
            self._add_in_memory_log(
                run_id=run_id,
                level="INFO",
                action="CANDIDATE_FOUND",
                message=f"{log_prefix}Candidato de {provider_name}: {url}",
                isbn=isbn,
                detail=f"provider={provider_name} | query={origin_query} | url={url} | title={title} | snippet={snippet} | position={pos}"
            )

        # Compile and Log Search Summary
        providers_used = search_service.get_providers_used()
        errors_count = search_service.get_and_reset_errors_count()
        
        search_summary = {
            "queries_executed": queries_executed,
            "providers_used": providers_used,
            "provider_errors": errors_count,
            "candidate_urls": len(candidate_urls),
            "domain_index_candidates_count": domain_index_candidates_count,
            "google_news_candidates_count": google_news_candidates_count,
            "internal_search_candidates_count": internal_search_candidates_count,
            "total_candidates_before_dedup": domain_index_candidates_count + google_news_candidates_count + internal_search_candidates_count,
            "total_candidates_after_dedup": len(candidate_urls)
        }
        
        summary_msg = (
            f"Resumen búsqueda: {queries_executed} queries, proveedores={providers_used}, "
            f"errores={errors_count}, candidatos={len(candidate_urls)} (Index={domain_index_candidates_count}, "
            f"News={google_news_candidates_count}, Interna={internal_search_candidates_count})"
        )
        logger_service.log(
            level="INFO",
            action="BOOK_SEARCH_SUMMARY",
            message=f"{log_prefix}{summary_msg}",
            isbn=isbn,
            detail=json.dumps(search_summary),
            sheet_id=sheet_id,
            run_id=run_id
        )
        self._add_in_memory_log(
            run_id=run_id,
            level="INFO",
            action="BOOK_SEARCH_SUMMARY",
            message=f"{log_prefix}{summary_msg}",
            isbn=isbn,
            detail=json.dumps(search_summary)
        )

        reviews_added = 0
        descartes_added = 0
        failed_extractions = 0
        extracted_ok_count = 0
        openai_accepted_count = 0
        openai_rejected_count = 0
        observation = ""

        # 3. Process candidate URLs
        for url in candidate_urls:
            item = candidate_origin[url]
            origin_query = item["query"]
            provider_name = item["provider"]
            
            # Phase 1: Filter candidates with known date (e.g. from Google News RSS pub_date)
            cand_pub_date = parse_iso_date(item.get("pub_date"))
            if cand_pub_date is not None:
                in_range = True
                if parsed_min and cand_pub_date < parsed_min:
                    in_range = False
                if parsed_max and cand_pub_date > parsed_max:
                    in_range = False
                    
                if not in_range:
                    logger_service.log(
                        "INFO", "ARTICLE_DISCARDED_DATE_FILTER", 
                        f"{log_prefix}Descarte de candidato por fecha ({cand_pub_date}): {url}", 
                        isbn=isbn, sheet_id=sheet_id, run_id=run_id
                    )
                    self._add_in_memory_log(
                        run_id, "INFO", "ARTICLE_DISCARDED_DATE_FILTER", 
                        f"{log_prefix}Descarte de candidato por fecha ({cand_pub_date}): {url}", 
                        isbn=isbn, 
                        detail=json.dumps({
                            "publication_date": str(cand_pub_date),
                            "date_min": final_min_str or "",
                            "date_max": final_max_str or "",
                            "stage": "candidate"
                        })
                    )
                    if not dry_run:
                        sheets_service.add_descarte(sheet_id, [
                            isbn, title, author, origin_query, url, "", "fuera de rango de fechas", 0, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        ])
                    descartes_added += 1
                    continue

            # Check primary duplicate
            norm_url = deduplicator.normalize_url(url)
            prim_hash = deduplicator.get_primary_hash(isbn, url)
            
            if prim_hash in existing_hashes:
                logger_service.log("DEBUG", "DEDUPLICATE_SKIP", f"{log_prefix}Saltando URL duplicada: {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                if not dry_run:
                    sheets_service.add_descarte(sheet_id, [
                        isbn, title, author, origin_query, url, "", "duplicado", 0, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ])
                descartes_added += 1
                continue

            # Extract article content
            article_data = {}
            try:
                article_data = article_extractor.extract(url)
                extracted_ok_count += 1
            except Exception as e:
                err_msg = str(e)
                reason = "error HTTP" if "error HTTP" in err_msg else "extracción fallida"
                if "texto insuficiente" in err_msg:
                    reason = "texto insuficiente"

                logger_service.log("WARNING", "EXTRACTION_FAILED", f"{log_prefix}Error extrayendo {url}: {err_msg}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                if not dry_run:
                    sheets_service.add_descarte(sheet_id, [
                        isbn, title, author, origin_query, url, "", reason, 0, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ])
                descartes_added += 1
                failed_extractions += 1
                continue

            art_title = article_data.get("title") or ""

            # Phase 2: Filter extracted articles with known date
            ext_pub_date = parse_iso_date(article_data.get("date"))
            if ext_pub_date is not None:
                in_range = True
                if parsed_min and ext_pub_date < parsed_min:
                    in_range = False
                if parsed_max and ext_pub_date > parsed_max:
                    in_range = False
                    
                if not in_range:
                    logger_service.log(
                        "INFO", "ARTICLE_DISCARDED_DATE_FILTER", 
                        f"{log_prefix}Descarte de artículo extraído por fecha ({ext_pub_date}): {url}", 
                        isbn=isbn, sheet_id=sheet_id, run_id=run_id
                    )
                    self._add_in_memory_log(
                        run_id, "INFO", "ARTICLE_DISCARDED_DATE_FILTER", 
                        f"{log_prefix}Descarte de artículo por fecha ({ext_pub_date}): {url}", 
                        isbn=isbn,
                        detail=json.dumps({
                            "publication_date": str(ext_pub_date),
                            "date_min": final_min_str or "",
                            "date_max": final_max_str or "",
                            "stage": "extraction"
                        })
                    )
                    if not dry_run:
                        sheets_service.add_descarte(sheet_id, [
                            isbn, title, author, origin_query, url, art_title, "fuera de rango de fechas", 0, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        ])
                    descartes_added += 1
                    continue

            # Validate secondary key
            art_domain = urlparse(url).netloc
            sec_key = deduplicator.get_secondary_key(isbn, art_domain, art_title)
            if sec_key in existing_secondary_keys:
                logger_service.log("WARNING", "DEDUPLICATE_SECONDARY_WARN", f"{log_prefix}Posible duplicado secundario detectado para URL: {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                self._add_in_memory_log(run_id, "WARNING", "DEDUPLICATE_SECONDARY_WARN", f"Posible duplicado secundario: {url}", isbn=isbn)

            # Analyze content with OpenAI
            try:
                analysis = openai_analyzer.analyze_article(
                    isbn=isbn,
                    book_title=title,
                    book_author=author,
                    query=origin_query,
                    url=url,
                    article_title=art_title,
                    article_text=article_data.get("text") or "",
                    detected_date=article_data.get("date") or "",
                    detected_author=article_data.get("author") or "",
                    detected_medium=article_data.get("publication_name") or "",
                    model_override=openai_model
                )
            except Exception as e:
                logger_service.log("ERROR", "OPENAI_FAILED", f"{log_prefix}Error OpenAI para {url}: {e}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                if not dry_run:
                    sheets_service.add_descarte(sheet_id, [
                        isbn, title, author, origin_query, url, art_title, "error OpenAI", 0, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ])
                descartes_added += 1
                continue

            # Phase 3: Filter after OpenAI returns / confirms the publication date
            ai_pub_date_str = analysis.get("publication_date", "")
            ai_pub_date = parse_iso_date(ai_pub_date_str)
            
            # If the date is known, check range
            if ai_pub_date is not None:
                in_range = True
                if parsed_min and ai_pub_date < parsed_min:
                    in_range = False
                if parsed_max and ai_pub_date > parsed_max:
                    in_range = False
                if not in_range:
                    logger_service.log(
                        "INFO", "ARTICLE_DISCARDED_DATE_FILTER", 
                        f"{log_prefix}Descarte OpenAI por fecha ({ai_pub_date}): {url}", 
                        isbn=isbn, sheet_id=sheet_id, run_id=run_id
                    )
                    self._add_in_memory_log(
                        run_id, "INFO", "ARTICLE_DISCARDED_DATE_FILTER", 
                        f"{log_prefix}Descarte de artículo por fecha ({ai_pub_date}): {url}", 
                        isbn=isbn,
                        detail=json.dumps({
                            "publication_date": str(ai_pub_date),
                            "date_min": final_min_str or "",
                            "date_max": final_max_str or "",
                            "stage": "openai"
                        })
                    )
                    if not dry_run:
                        sheets_service.add_descarte(sheet_id, [
                            isbn, title, author, origin_query, url, art_title, "fuera de rango de fechas", 0, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        ])
                    descartes_added += 1
                    continue
            else:
                # The date is unknown (ai_pub_date is None)
                if not final_include_unknown:
                    logger_service.log(
                        "INFO", "ARTICLE_DISCARDED_DATE_FILTER", 
                        f"{log_prefix}Descarte OpenAI por fecha desconocida: {url}", 
                        isbn=isbn, sheet_id=sheet_id, run_id=run_id
                    )
                    self._add_in_memory_log(
                        run_id, "INFO", "ARTICLE_DISCARDED_DATE_FILTER", 
                        f"{log_prefix}Descarte por fecha desconocida: {url}", 
                        isbn=isbn,
                        detail=json.dumps({
                            "publication_date": "",
                            "date_min": final_min_str or "",
                            "date_max": final_max_str or "",
                            "stage": "openai"
                        })
                    )
                    if not dry_run:
                        sheets_service.add_descarte(sheet_id, [
                            isbn, title, author, origin_query, url, art_title, "fuera de rango de fechas", 0, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        ])
                    descartes_added += 1
                    continue

            is_valid = analysis.get("is_valid", False)
            score = analysis.get("match_score", 0)
            openai_reason = analysis.get("reason", "")
            
            # Map OpenAI reason or scores to descarte categories
            descarte_reason = ""
            if not is_valid:
                openai_reason_lower = openai_reason.lower()
                if "autor" in openai_reason_lower and ("sólo" in openai_reason_lower or "solo" in openai_reason_lower or "no habla" in openai_reason_lower):
                    descarte_reason = "habla solo del autor"
                else:
                    descarte_reason = "no menciona el libro"
            elif score < min_score:
                descarte_reason = "score bajo"

            if descarte_reason:
                openai_rejected_count += 1
                logger_service.log("INFO", "ARTICLE_DISCARDED", f"{log_prefix}URL descartada ({descarte_reason}, score: {score}): {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                self._add_in_memory_log(run_id, "INFO", "ARTICLE_DISCARDED", f"{log_prefix}Descarte ({descarte_reason}, score={score}): {url}", isbn=isbn)
                
                if not dry_run:
                    sheets_service.add_descarte(sheet_id, [
                        isbn, title, author, origin_query, url, art_title, descarte_reason, score, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ])
                descartes_added += 1
            else:
                # Valid Review!
                openai_accepted_count += 1
                logger_service.log("INFO", "ARTICLE_ACCEPTED", f"{log_prefix}Reseña válida aceptada (score: {score}): {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                self._add_in_memory_log(run_id, "INFO", "ARTICLE_ACCEPTED", f"{log_prefix}Aceptada (score={score}): {url}", isbn=isbn)

                if not dry_run:
                    sheets_service.add_review(sheet_id, [
                        isbn,
                        title,
                        author,
                        origin_query,
                        url,
                        norm_url,
                        art_title,
                        analysis.get("detected_book_title", ""),
                        analysis.get("detected_book_author", ""),
                        analysis.get("publication_name", ""),
                        analysis.get("publication_author", ""),
                        analysis.get("publication_date", ""),
                        analysis.get("language", ""),
                        analysis.get("category", ""),
                        analysis.get("summary", ""),
                        score,
                        analysis.get("content_type", ""),
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        prim_hash,
                        "pendiente"
                    ])
                    existing_hashes.add(prim_hash)
                    existing_secondary_keys.add(sec_key)
                
                reviews_added += 1

        # Determine specific prefix observation depending on missing values
        spec_obs = ""
        has_isbn = bool(isbn and isbn.strip())
        has_author = bool(author and author.strip())
        if not has_isbn and not has_author:
            spec_obs = "Búsqueda realizada solo por título. "
        elif not has_isbn:
            spec_obs = "Búsqueda realizada sin ISBN. "
        elif not has_author:
            spec_obs = "Búsqueda realizada sin autor. "

        # Determine final status
        final_status = "completado"
        if reviews_added == 0:
            final_status = "sin_resultados"
            observation = f"Búsqueda finalizada. 0 reseñas aceptadas. {descartes_added} descartes."
            if failed_extractions > 0:
                observation += f" ({failed_extractions} fallos de red/extracción)."
        else:
            observation = f"Proceso finalizado. Encontradas y guardadas {reviews_added} reseñas. {descartes_added} descartes."

        # Prepend the missing-data observation prefix
        observation = spec_obs + observation

        # Log detailed counters
        logger_service.log(
            level="INFO",
            action="BOOK_PROCESS_SUMMARY_COUNTERS",
            message=(
                f"{log_prefix}Contadores de libro: "
                f"domain_index_candidates_count={domain_index_candidates_count} | "
                f"google_news_candidates_count={google_news_candidates_count} | "
                f"internal_search_candidates_count={internal_search_candidates_count} | "
                f"total_candidates_before_dedup={domain_index_candidates_count + google_news_candidates_count + internal_search_candidates_count} | "
                f"total_candidates_after_dedup={len(candidate_urls)} | "
                f"extracted_ok_count={extracted_ok_count} | "
                f"openai_accepted_count={openai_accepted_count} | "
                f"openai_rejected_count={openai_rejected_count} | "
                f"final_reviews_count={reviews_added}"
            ),
            isbn=isbn,
            sheet_id=sheet_id,
            run_id=run_id
        )

        book_detail = {
            "isbn": isbn,
            "title": title,
            "domain_index_candidates": domain_index_candidates_count,
            "google_news_candidates": google_news_candidates_count,
            "internal_search_candidates": internal_search_candidates_count,
            "total_before_dedup": domain_index_candidates_count + google_news_candidates_count + internal_search_candidates_count,
            "total_after_dedup": len(candidate_urls),
            "accepted_by_ai": reviews_added,
            "final_status": final_status
        }
        if run_id in current_runs:
            if "books_details" not in current_runs[run_id]:
                current_runs[run_id]["books_details"] = []
            current_runs[run_id]["books_details"].append(book_detail)

        logger_service.log("INFO", "BOOK_PROCESS_END", f"{log_prefix}Libro finalizado con estado '{final_status}'. {observation}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
        self._add_in_memory_log(run_id, "INFO", "BOOK_PROCESS_END", f"{log_prefix}Finalizado: {final_status}. {observation}", isbn=isbn)

        if dry_run:
            # Update row in Libros to show proof run, but KEEP status as 'pendiente'
            try:
                sheets_service.update_book_status(
                    sheet_id=sheet_id,
                    row_index=row_index,
                    status="pendiente", # Keep pending!
                    last_run=f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (Prueba)",
                    reviews_found=reviews_added,
                    observations=f"[PRUEBA] {observation}"
                )
            except Exception as e:
                logger_service.log("WARNING", "BOOK_STATUS_UPDATE_FAIL", f"No se pudo actualizar logs de prueba en Libros: {e}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
            # Flush any buffered log rows to Sheets
            logger_service.flush_log_batch(sheet_id, run_id)
            return final_status

        # Update book status in Sheets
        try:
            sheets_service.update_book_status(
                sheet_id=sheet_id,
                row_index=row_index,
                status=final_status,
                last_run=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                reviews_found=reviews_added,
                observations=observation
            )
        except Exception as e:
            logger_service.log("ERROR", "BOOK_STATUS_UPDATE_FAIL", f"No se pudo actualizar el estado final: {e}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
            self._add_in_memory_log(run_id, "ERROR", "BOOK_STATUS_UPDATE_FAIL", f"Fallo al actualizar estado final: {e}", isbn=isbn)
            logger_service.flush_log_batch(sheet_id, run_id)
            return "error"

        # Flush any buffered log rows to Sheets
        logger_service.flush_log_batch(sheet_id, run_id)
        return final_status

run_service = RunService()
