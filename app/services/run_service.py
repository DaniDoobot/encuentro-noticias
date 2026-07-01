import uuid
import datetime
import json
import time
import logging
from typing import Dict, Any, List, Optional, Set, Tuple
import threading
from urllib.parse import urlparse

from app.config import settings
from app.services.sheets_service import sheets_service, get_now_madrid_str
from app.services.query_builder import query_builder
from app.services.search_service import search_service, is_true
from app.services.article_extractor import article_extractor, normalize_date
from app.services.openai_analyzer import openai_analyzer
from app.services.deduplicator import deduplicator
from app.services.logger_service import logger_service
from app.services.source_discovery import source_discovery
from app.services.cache_service import cache_service

# In-memory storage for runs
current_runs: Dict[str, Dict[str, Any]] = {}
cancelled_runs: Set[str] = set()

class RunCancelledException(Exception):
    def __init__(self, message: str, reviews_added: int = 0):
        super().__init__(message)
        self.reviews_added = reviews_added

def is_consent_or_cookie_page(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    keywords = ["cookies", "política de privacidad", "uso de datos", "consent", "before you continue", "aceptar todo", "configurar cookies"]
    matches = [kw in text_lower for kw in keywords]
    count = sum(matches)
    
    if "before you continue" in text_lower or ("cookies" in text_lower and "consent" in text_lower):
        return True
    if count >= 2:
        return True
    if "cookies" in text_lower and len(text_lower) < 2000 and ("política" in text_lower or "privacidad" in text_lower or "datos" in text_lower):
        return True
    return False

class RunService:
    def get_run_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        return current_runs.get(run_id)

    def cancel_run(self, run_id: str) -> bool:
        if run_id in current_runs:
            if current_runs[run_id]["status"] not in ("completed", "failed", "cancelled"):
                current_runs[run_id]["status"] = "cancelled"
                current_runs[run_id]["message"] = "Búsqueda cancelada por el usuario."
                cancelled_runs.add(run_id)
                self._add_in_memory_log(run_id, "INFO", "RUN_CANCELLED", "La ejecución ha sido cancelada por el usuario.")
                return True
        return False

    def _check_cancellation(self, run_id: str, reviews_added: int = 0):
        if run_id in cancelled_runs or (run_id in current_runs and current_runs[run_id].get("status") == "cancelled"):
            raise RunCancelledException("Búsqueda cancelada por el usuario.", reviews_added=reviews_added)

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
            "books_rows_read": 0,
            "books_pending_detected": 0,
            "books_skipped_missing_title": 0,
            "books_skipped_non_pending_status": 0,
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
            timestamp = get_now_madrid_str()
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
            # Priority:
            # 1. limit_books from request (if not None)
            # 2. Config "MAX_BOOKS_PER_RUN"
            # 3. Fallback to settings.MAX_BOOKS_PER_RUN (10)
            config_max_books = run_config.get("MAX_BOOKS_PER_RUN", settings.MAX_BOOKS_PER_RUN)
            if limit_books is not None:
                max_books = limit_books
            else:
                max_books = config_max_books
            max_pages = run_config["MAX_SEARCH_PAGES_PER_QUERY"]
            max_candidates = run_config["MAX_CANDIDATES_PER_BOOK"]
            min_score = run_config["MIN_MATCH_SCORE"]
            openai_model = run_config["OPENAI_MODEL"]
            
            review_domains_str = run_config.get("REVIEW_DOMAINS", "")
            review_domains = [d.strip() for d in review_domains_str.split(",") if d.strip()]
            
            self._add_in_memory_log(run_id, "INFO", "CONFIG_LOADED", f"Configuraciones leídas: Max libros={max_books}, Min score={min_score}, Dominios específicos={len(review_domains)}")
            
            # 2. Get pending books
            books_result = sheets_service.get_pending_books(sheet_id, limit=max_books)
            pending_books = books_result["books"]
            books_rows_read = books_result["books_rows_read"]
            books_pending_detected = books_result["books_pending_detected"]
            books_skipped_missing_title = books_result["books_skipped_missing_title"]
            books_skipped_not_included = books_result.get("books_skipped_not_included", 0)
            books_skipped_blocked_status = books_result.get("books_skipped_blocked_status", 0)

            current_runs[run_id]["books_rows_read"] = books_rows_read
            current_runs[run_id]["books_pending_detected"] = books_pending_detected
            current_runs[run_id]["books_skipped_missing_title"] = books_skipped_missing_title
            current_runs[run_id]["books_skipped_not_included"] = books_skipped_not_included
            current_runs[run_id]["books_skipped_blocked_status"] = books_skipped_blocked_status

            total_books = len(pending_books)
            current_runs[run_id]["books_total"] = total_books

            detection_summary = (
                f"Detección libros: leidas={books_rows_read}, "
                f"incluidas_en_busqueda={books_pending_detected}, "
                f"omitidas_no_marcadas={books_skipped_not_included}, "
                f"omitidas_sin_título={books_skipped_missing_title}, "
                f"omitidas_estado={books_skipped_blocked_status}"
            )
            logger_service.log("INFO", "BOOKS_DETECTION_SUMMARY", f"{log_prefix}{detection_summary}", sheet_id=sheet_id, run_id=run_id)
            self._add_in_memory_log(run_id, "INFO", "BOOKS_DETECTION_SUMMARY", detection_summary)

            if books_skipped_missing_title > 0:
                logger_service.log(
                    "WARNING", "BOOK_ROW_SKIPPED_MISSING_TITLE",
                    f"{log_prefix}{books_skipped_missing_title} fila(s) omitidas por falta de título.",
                    sheet_id=sheet_id, run_id=run_id
                )

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
                if run_id in cancelled_runs or current_runs[run_id].get("status") == "cancelled":
                    break
                isbn = book["isbn"]
                title = book["title"]
                author = book["author"]
                row_index = book["row_index"]

                # Log acceptance with available fields
                if not isbn and not author:
                    self._add_in_memory_log(run_id, "INFO", "BOOK_ROW_ACCEPTED_PENDING_WITHOUT_ISBN",
                        f"{log_prefix}Libro aceptado sin ISBN ni autor: '{title}'", isbn="")
                    logger_service.log("INFO", "BOOK_ROW_ACCEPTED_PENDING_WITHOUT_ISBN",
                        f"{log_prefix}Libro aceptado solo con título: '{title}'",
                        isbn="", sheet_id=sheet_id, run_id=run_id)
                elif not isbn:
                    self._add_in_memory_log(run_id, "INFO", "BOOK_ROW_ACCEPTED_PENDING_WITHOUT_ISBN",
                        f"{log_prefix}Libro aceptado sin ISBN: '{title}' por {author}", isbn="")
                    logger_service.log("INFO", "BOOK_ROW_ACCEPTED_PENDING_WITHOUT_ISBN",
                        f"{log_prefix}Libro aceptado sin ISBN: '{title}' por {author}",
                        isbn="", sheet_id=sheet_id, run_id=run_id)

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
                except RunCancelledException as e:
                    reviews_added = e.reviews_added
                    new_status = "cancelado" if reviews_added > 0 else "pendiente"
                    logger_service.log("INFO", "RUN_CANCELLED", f"Ejecución cancelada durante el procesamiento del libro '{title}'.", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                    self._add_in_memory_log(run_id, "INFO", "RUN_CANCELLED", f"Ejecución cancelada durante '{title}'.", isbn=isbn)
                    if not dry_run:
                        try:
                            sheets_service.update_book_status(
                                sheet_id=sheet_id,
                                row_index=row_index,
                                status=new_status,
                                last_run=get_now_madrid_str(),
                                reviews_found=reviews_added,
                                observations="Búsqueda cancelada por el usuario."
                            )
                        except Exception as e_sheet:
                            logger_service.log("ERROR", "SHEET_UPDATE_FAIL", f"Fallo al restaurar estado a '{new_status}': {e_sheet}", isbn=isbn, sheet_id=sheet_id)
                    current_runs[run_id]["status"] = "cancelled"
                    current_runs[run_id]["message"] = "Búsqueda cancelada por el usuario."
                    break
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
                                last_run=get_now_madrid_str(),
                                reviews_found=0,
                                observations=f"Error de proceso: {str(e)}"
                            )
                        except Exception as e_sheet:
                            logger_service.log("ERROR", "SHEET_UPDATE_FAIL", f"Fallo al marcar estado 'error' en Libros: {e_sheet}", isbn=isbn, sheet_id=sheet_id)
                finally:
                    current_runs[run_id]["books_processed"] += 1
            
            if current_runs[run_id]["status"] != "cancelled":
                current_runs[run_id]["status"] = "completed"
                current_runs[run_id]["message"] = f"Ejecución completada. Procesados {current_runs[run_id]['books_processed']} libros."
                self._auto_cleanup_descartes(sheet_id, run_id)
                self._auto_compact_sheet(sheet_id, run_id)
                self._add_in_memory_log(run_id, "INFO", "RUN_END", f"Ejecución global completada. Completados={current_runs[run_id]['books_completed']}, Sin resultados={current_runs[run_id]['books_no_results']}, Fallidos={current_runs[run_id]['books_failed']}")
                logger_service.log("INFO", "RUN_END", f"Ejecución global completada. Completados={current_runs[run_id]['books_completed']}, Sin resultados={current_runs[run_id]['books_no_results']}, Fallidos={current_runs[run_id]['books_failed']}", sheet_id=sheet_id, run_id=run_id)
            else:
                self._add_in_memory_log(run_id, "INFO", "RUN_END", f"Ejecución global cancelada por el usuario.")
                logger_service.log("INFO", "RUN_END", f"Ejecución global cancelada por el usuario.", sheet_id=sheet_id, run_id=run_id)

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
                            last_run=get_now_madrid_str(),
                            reviews_found=0,
                            observations=f"Error de proceso: {str(e)}"
                        )
                    except Exception as e_sheet:
                        logger_service.log("ERROR", "SHEET_UPDATE_FAIL", f"Fallo al marcar estado 'error' en Libros: {e_sheet}", isbn=isbn, sheet_id=sheet_id)
            finally:
                current_runs[run_id]["books_processed"] = 1

            current_runs[run_id]["status"] = "completed"
            current_runs[run_id]["message"] = f"Ejecución completada para ISBN {isbn}."
            self._auto_cleanup_descartes(sheet_id, run_id)
            self._auto_compact_sheet(sheet_id, run_id)
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
        self._check_cancellation(run_id)
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
                        last_run=get_now_madrid_str(),
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
                    last_run=get_now_madrid_str(),
                    reviews_found=0,
                    observations="Procesando..."
                )
            except Exception as e:
                logger_service.log("ERROR", "BOOK_STATUS_UPDATE_FAIL", f"No se pudo actualizar el estado del libro a procesando: {e}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                self._add_in_memory_log(run_id, "ERROR", "BOOK_STATUS_UPDATE_FAIL", f"Fallo al marcar procesando: {e}", isbn=isbn)

        # 1. Load configs & log effective configuration
        effective_min_score = min_score
        effective_max_candidates = max_candidates
        effective_news_complement = int(config.get("DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES", settings.DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES))
        effective_max_queries = max_queries
        
        config_effective_detail = json.dumps({
            "MIN_MATCH_SCORE": effective_min_score,
            "MAX_CANDIDATES_PER_BOOK": effective_max_candidates,
            "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES": effective_news_complement,
            "MAX_QUERIES_PER_BOOK": effective_max_queries,
            "GOOGLE_NEWS_BROAD_MAX_QUERIES": 10
        })
        logger_service.log("INFO", "RUN_CONFIG_EFFECTIVE", f"{log_prefix}Configuración efectiva para este libro", isbn=isbn, detail=config_effective_detail, sheet_id=sheet_id, run_id=run_id)
        self._add_in_memory_log(run_id, "INFO", "RUN_CONFIG_EFFECTIVE", f"{log_prefix}Configuración efectiva para este libro", isbn=isbn, detail=config_effective_detail)

        # Check if author is generic and log normalization
        author_is_generic = False
        if author and author.strip():
            author_is_generic = query_builder.is_generic_author(author)
            if author_is_generic:
                auth_norm_detail = json.dumps({
                    "original_author": author,
                    "normalized_author": "",
                    "author_is_generic": True
                })
                logger_service.log("INFO", "AUTHOR_NORMALIZED_AS_GENERIC", f"{log_prefix}Autor genérico detectado y normalizado", isbn=isbn, detail=auth_norm_detail, sheet_id=sheet_id, run_id=run_id)
                self._add_in_memory_log(run_id, "INFO", "AUTHOR_NORMALIZED_AS_GENERIC", f"{log_prefix}Autor genérico detectado y normalizado", isbn=isbn, detail=auth_norm_detail)

        # Generate queries categorized into 3 levels plus broad queries
        queries_dict = query_builder.build_queries(title, author, isbn, review_domains=review_domains)
        prioritarias = queries_dict["prioritarias"]
        apoyo = queries_dict["apoyo"]
        dominios = queries_dict["dominios"]
        broad_queries = query_builder.build_broad_queries(title, author, isbn)
        
        queries_built_detail = json.dumps({
            "book_title": title,
            "book_author": author,
            "author_is_generic": author_is_generic,
            "prioritarias": prioritarias,
            "apoyo": apoyo,
            "dominios": dominios,
            "broad_queries": broad_queries
        }, ensure_ascii=False)
        logger_service.log("INFO", "BOOK_QUERIES_BUILT", f"{log_prefix}Queries generadas para la búsqueda", isbn=isbn, detail=queries_built_detail, sheet_id=sheet_id, run_id=run_id)
        self._add_in_memory_log(run_id, "INFO", "BOOK_QUERIES_BUILT", f"{log_prefix}Queries generadas para la búsqueda", isbn=isbn, detail=queries_built_detail)

        self._add_in_memory_log(
            run_id, "INFO", "QUERIES_GENERATED", 
            f"{log_prefix}Generadas queries por nivel: Prioritarias={len(prioritarias)}, Apoyo={len(apoyo)}, Dominios={len(dominios)}, Broad={len(broad_queries)}", 
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
        # Use getattr for resilience against stale Docker images missing newer settings
        _enable_cascade_default = getattr(settings, "ENABLE_CASCADE_SEARCH", True)
        cascade_search = is_true(config.get("ENABLE_CASCADE_SEARCH", _enable_cascade_default))
        
        # If SEARCH_PROVIDER_MODE is domain_index_plus_news, force cascade search
        if search_mode == "domain_index_plus_news":
            cascade_search = True

        # Log resolved search mode
        logger_service.log("INFO", "SEARCH_MODE_RESOLVED", f"{log_prefix}Modo de búsqueda resuelto: config={config.get('SEARCH_PROVIDER_MODE')}, resolved={search_mode}, cascade={cascade_search}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
        self._add_in_memory_log(run_id, "INFO", "SEARCH_MODE_RESOLVED", f"Modo de búsqueda resuelto: config={config.get('SEARCH_PROVIDER_MODE')}, resolved={search_mode}, cascade={cascade_search}", isbn=isbn)

        internal_domains_attempted = 0
        internal_domains_with_results = 0

        if cascade_search:
            self._check_cancellation(run_id)
            logger_service.log("INFO", "CASCADE_SEARCH_STARTED", f"{log_prefix}Iniciando búsqueda en cascada", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
            self._add_in_memory_log(run_id, "INFO", "CASCADE_SEARCH_STARTED", "Iniciando búsqueda en cascada", isbn=isbn)

            # PHASE 1 — Domain Index
            self._check_cancellation(run_id)
            logger_service.log("INFO", "DOMAIN_INDEX_SEARCH_STARTED", f"{log_prefix}Iniciando Fase 1: Domain Index", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
            self._add_in_memory_log(run_id, "INFO", "DOMAIN_INDEX_SEARCH_STARTED", "Iniciando Fase 1: Domain Index", isbn=isbn)
            
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
            logger_service.log("INFO", "DOMAIN_INDEX_SEARCH_COMPLETED", f"{log_prefix}Fase 1: Domain Index completada. Candidatos={domain_index_candidates_count}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
            self._add_in_memory_log(run_id, "INFO", "DOMAIN_INDEX_SEARCH_COMPLETED", f"Fase 1: Domain Index completada. Candidatos={domain_index_candidates_count}", isbn=isbn)
            
            # PHASE 2 — Google News RSS / Búsqueda externa ligera
            self._check_cancellation(run_id)
            logger_service.log("INFO", "GOOGLE_NEWS_COMPLEMENT_STARTED", f"{log_prefix}Iniciando Fase 2: Google News Complement", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
            self._add_in_memory_log(run_id, "INFO", "GOOGLE_NEWS_COMPLEMENT_STARTED", "Iniciando Fase 2: Google News Complement", isbn=isbn)
            
            rss_max = int(config.get("DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES", settings.DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES))
            rss_queries_done = 0
            google_news_candidates = 0
            for q in prioritarias:
                self._check_cancellation(run_id)
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
            logger_service.log("INFO", "GOOGLE_NEWS_COMPLEMENT_COMPLETED", f"{log_prefix}Fase 2: Google News Complement completada. Candidatos nuevos={google_news_candidates_count}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
            self._add_in_memory_log(run_id, "INFO", "GOOGLE_NEWS_COMPLEMENT_COMPLETED", f"Fase 2: Google News Complement completada. Candidatos nuevos={google_news_candidates_count}", isbn=isbn)

            # If no candidates were found on Google News, run Broad Google News phase
            if google_news_candidates_count == 0:
                logger_service.log(
                    "INFO", "GOOGLE_NEWS_BROAD_STARTED",
                    f"{log_prefix}0 candidatos en Google News. Iniciando Fase 2 Broad (queries amplias)",
                    isbn=isbn, sheet_id=sheet_id, run_id=run_id
                )
                self._add_in_memory_log(
                    run_id, "INFO", "GOOGLE_NEWS_BROAD_STARTED",
                    f"{log_prefix}0 candidatos en Google News. Iniciando Fase 2 Broad (queries amplias)",
                    isbn=isbn
                )
                broad_queries_done = 0
                for q in broad_queries:
                    self._check_cancellation(run_id)
                    # Broad queries run on their own budget of up to 10 queries, ignoring max_queries!
                    if broad_queries_done >= 10:
                        break
                    if len(candidate_origin) >= max_candidates:
                        break
                    if queries_executed > 0 or broad_queries_done > 0:
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
                    broad_queries_done += 1
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
                logger_service.log(
                    "INFO", "GOOGLE_NEWS_BROAD_COMPLETED",
                    f"{log_prefix}Fase 2 Broad completada. Candidatos nuevos={google_news_candidates}, total Google News={google_news_candidates_count}",
                    isbn=isbn, sheet_id=sheet_id, run_id=run_id
                )
                self._add_in_memory_log(
                    run_id, "INFO", "GOOGLE_NEWS_BROAD_COMPLETED",
                    f"{log_prefix}Fase 2 Broad completada. Candidatos nuevos={google_news_candidates}",
                    isbn=isbn
                )

            # PHASE 3 — Internal Domain Search
            self._check_cancellation(run_id)
            _always_internal_default = getattr(settings, "ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH", True)
            _min_cand_default = getattr(settings, "MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH", 5)
            _enable_deep_default = getattr(settings, "ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS", True)
            always_run_internal = is_true(config.get("ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH", _always_internal_default))
            min_candidates_internal = int(config.get("MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH", _min_cand_default))
            enable_deep_search = is_true(config.get("ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS", _enable_deep_default))
            enable_internal_search = is_true(config.get("ENABLE_INTERNAL_DOMAIN_SEARCH", getattr(settings, "ENABLE_INTERNAL_DOMAIN_SEARCH", True)))

            total_before_internal = len(candidate_origin)
            internal_search_was_forced = False
            internal_search_skip_reason = ""

            # Decide whether to run internal search
            should_run_internal = False
            if not enable_internal_search:
                internal_search_skip_reason = "ENABLE_INTERNAL_DOMAIN_SEARCH=false"
            elif always_run_internal:
                should_run_internal = True
                internal_search_was_forced = True
            elif enable_deep_search and total_before_internal < min_candidates_internal:
                should_run_internal = True
            else:
                internal_search_skip_reason = f"enough_candidates ({total_before_internal} >= {min_candidates_internal})"

            if not should_run_internal:
                skip_msg = f"{log_prefix}Búsqueda interna omitida: {internal_search_skip_reason}"
                logger_service.log(
                    level="INFO",
                    action="INTERNAL_SEARCH_SKIPPED",
                    message=skip_msg,
                    isbn=isbn,
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                self._add_in_memory_log(run_id, "INFO", "INTERNAL_SEARCH_SKIPPED", skip_msg, isbn=isbn)
            else:
                force_label = " (forzada)" if internal_search_was_forced else f" (pocos candidatos: {total_before_internal} < {min_candidates_internal})"
                start_msg = f"{log_prefix}Iniciando búsqueda interna{force_label}"
                logger_service.log(
                    level="INFO",
                    action="INTERNAL_SEARCH_STARTED",
                    message=start_msg,
                    isbn=isbn,
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                self._add_in_memory_log(run_id, "INFO", "INTERNAL_SEARCH_STARTED", start_msg, isbn=isbn)

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

                internal_domains_attempted = len(domains)
                if domains:
                    source_by_domain = {s["domain"]: s for s in sources}
                    new_urls_found = 0
                    for domain in domains:
                        self._check_cancellation(run_id)
                        try:
                            items = internal_search_provider.search_domain_for_book(
                                domain=domain,
                                title=title,
                                author=author,
                                isbn=isbn,
                                config=config,
                                source_info=source_by_domain.get(domain),
                                sheet_id=sheet_id,
                                run_id=run_id
                            )
                            if items:
                                internal_domains_with_results += 1
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
                                enrich_enabled = is_true(config.get("ENRICH_INDEXED_URLS", settings.ENRICH_INDEXED_URLS))
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

                    # Re-run SourceDiscovery to retrieve new candidates (no max_candidates cap here yet)
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
                            if url not in candidate_origin:
                                candidate_origin[url] = {
                                    "query": "local_index",
                                    "provider": "InternalSearch",
                                    "title": match.get("title") or "",
                                    "snippet": match.get("snippet") or "",
                                    "position": match.get("score"),
                                    "pub_date": match.get("pub_date"),
                                    "score": match.get("score", 0),
                                    "matched_fields": match.get("matched_fields", [])
                                }
                                internal_candidates += 1
                                logger_service.log(
                                    level="INFO",
                                    action="DOMAIN_SEARCH_MATCH",
                                    message=f"{log_prefix}Match interno (score={match['score']}): {url}",
                                    isbn=isbn,
                                    detail=f"domain={match.get('domain')} | matched={match.get('matched_fields')} | title={match.get('title','')[:80]}",
                                    sheet_id=sheet_id,
                                    run_id=run_id
                                )
                        internal_search_candidates_count = internal_candidates

                logger_service.log(
                    level="INFO",
                    action="INTERNAL_SEARCH_COMPLETED",
                    message=f"{log_prefix}Búsqueda interna finalizada. Nuevos candidatos: {internal_search_candidates_count}",
                    isbn=isbn,
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                self._add_in_memory_log(
                    run_id, "INFO", "INTERNAL_SEARCH_COMPLETED",
                    f"{log_prefix}Búsqueda interna finalizada: nuevos_candidatos={internal_search_candidates_count}",
                    isbn=isbn
                )

            # --- Prioritize and cap candidates ---
            # Sort by quality: exact title+author match > InternalSearch > DomainIndex score > GoogleNews
            title_lower = title.strip().lower()
            author_lower = author.strip().lower() if author else ""

            def candidate_priority(item_kv):
                url_k, meta = item_kv
                cand_title = (meta.get("title") or "").lower()
                provider = (meta.get("provider") or "").lower()
                score = meta.get("score") or meta.get("position") or 0
                if isinstance(score, str):
                    try: score = float(score)
                    except: score = 0

                exact_match = (title_lower in cand_title) or (cand_title in title_lower and len(cand_title) > 5)
                author_match = author_lower and author_lower in cand_title
                is_internal = "internalsearch" in provider or "domainindex" in provider
                return (
                    -(2 if (exact_match and author_match) else 1 if exact_match else 0),  # exact match priority
                    -(1 if is_internal else 0),                                            # internal/index over news
                    -float(score)                                                           # higher score first
                )

            all_candidates = list(candidate_origin.items())
            all_candidates.sort(key=candidate_priority)

            # Apply max_candidates cap after prioritization
            candidates_discarded_by_limit = max(0, len(all_candidates) - max_candidates)
            all_candidates = all_candidates[:max_candidates]
            candidate_origin = dict(all_candidates)
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

            # If search_mode is google_news_only and we got 0 candidates, run Broad Google News phase
            if len(candidate_origin) == 0 and search_mode == "google_news_only":
                logger_service.log(
                    "INFO", "GOOGLE_NEWS_BROAD_STARTED",
                    f"{log_prefix}0 candidatos en Google News (modo single-provider). Iniciando queries amplias",
                    isbn=isbn, sheet_id=sheet_id, run_id=run_id
                )
                self._add_in_memory_log(
                    run_id, "INFO", "GOOGLE_NEWS_BROAD_STARTED",
                    f"{log_prefix}0 candidatos en Google News (modo single-provider). Iniciando queries amplias",
                    isbn=isbn
                )
                broad_queries_done = 0
                for q in broad_queries:
                    self._check_cancellation(run_id)
                    # Broad queries run on their own budget of up to 10 queries, ignoring max_queries!
                    if broad_queries_done >= 10:
                        break
                    if len(candidate_origin) >= max_candidates:
                        break
                    if queries_executed > 0 or broad_queries_done > 0:
                        time.sleep(search_delay)
                    found_items = search_service.search_with_fallback(
                        query=q, max_pages=max_pages, sheet_id=sheet_id, run_id=run_id, isbn=isbn, config=config, log_callback=self._add_in_memory_log
                    )
                    queries_executed += 1
                    broad_queries_done += 1
                    for item in found_items:
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
                logger_service.log(
                    "INFO", "GOOGLE_NEWS_BROAD_COMPLETED",
                    f"{log_prefix}Fase Broad completada. total Google News={len(candidate_origin)}",
                    isbn=isbn, sheet_id=sheet_id, run_id=run_id
                )
                self._add_in_memory_log(
                    run_id, "INFO", "GOOGLE_NEWS_BROAD_COMPLETED",
                    f"{log_prefix}Fase Broad completada. total Google News={len(candidate_origin)}",
                    isbn=isbn
                )

            candidates_discarded_by_limit = 0
            internal_search_was_forced = False
            internal_search_skip_reason = ""
            candidate_urls = list(candidate_origin.keys())

        # Log each candidate found with its originating query and provider
        for url, item in candidate_origin.items():
            provider_name = item["provider"]
            origin_query = item["query"]
            cand_title = item.get("title") or ""
            snippet = item.get("snippet") or ""
            pos = item.get("position") or ""

            logger_service.log(
                level="INFO",
                action="CANDIDATE_FOUND",
                message=f"{log_prefix}Candidato de {provider_name}: {url}",
                isbn=isbn,
                detail=f"provider={provider_name} | query={origin_query} | url={url} | title={cand_title} | snippet={snippet} | position={pos}",
                sheet_id=sheet_id,
                run_id=run_id
            )
            self._add_in_memory_log(
                run_id=run_id,
                level="INFO",
                action="CANDIDATE_FOUND",
                message=f"{log_prefix}Candidato de {provider_name}: {url}",
                isbn=isbn,
                detail=f"provider={provider_name} | query={origin_query} | url={url} | title={cand_title} | snippet={snippet} | position={pos}"
            )

        # Compile and Log Search Summary
        providers_used = search_service.get_providers_used()
        errors_count = search_service.get_and_reset_errors_count()

        search_summary = {
            "search_provider_mode_config": config.get("SEARCH_PROVIDER_MODE", settings.SEARCH_PROVIDER_MODE),
            "search_provider_mode_resolved": search_mode,
            "enable_cascade_search": cascade_search,
            "providers_used": providers_used,
            "domain_index_candidates_count": domain_index_candidates_count,
            "google_news_candidates_count": google_news_candidates_count,
            "internal_search_candidates_count": internal_search_candidates_count,
            "internal_domains_attempted": internal_domains_attempted,
            "internal_domains_with_results": internal_domains_with_results,
            "queries_executed": queries_executed,
            "candidate_urls": len(candidate_urls),
            "provider_errors": errors_count,
            "total_candidates_before_dedup": domain_index_candidates_count + google_news_candidates_count + internal_search_candidates_count,
            "total_candidates_after_dedup": len(candidate_urls),
            "candidates_discarded_by_limit": candidates_discarded_by_limit,
            "candidates_sent_to_openai": len(candidate_urls),
            "internal_search_was_forced": internal_search_was_forced,
            "internal_search_skip_reason": internal_search_skip_reason
        }

        summary_msg = (
            f"Resumen búsqueda: {queries_executed} queries, proveedores={providers_used}, "
            f"errores={errors_count}, candidatos={len(candidate_urls)} (Index={domain_index_candidates_count}, "
            f"News={google_news_candidates_count}, Interna={internal_search_candidates_count}, "
            f"descartados_por_límite={candidates_discarded_by_limit})"
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

        self._check_cancellation(run_id, reviews_added=reviews_added)
        # 3. Process candidate URLs
        for url in candidate_urls:
            self._check_cancellation(run_id, reviews_added=reviews_added)
            item = candidate_origin[url]
            origin_query = item["query"]
            provider_name = item["provider"]
            
            original_candidate_url = url
            resolved_url = url
            
            # Resolve Google News RSS redirect URLs to final article URLs
            if url.startswith("https://news.google.com/rss/articles/"):
                try:
                    from googlenewsdecoder import gnewsdecoder
                    decoded = gnewsdecoder(url, interval=1)
                    if decoded.get("status") and decoded.get("decoded_url"):
                        resolved_url = decoded["decoded_url"]
                        logger_service.log("INFO", "URL_RESOLVED", f"URL de Google News resuelta: {url} -> {resolved_url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                    else:
                        logger_service.log("WARNING", "URL_RESOLUTION_FAILED", f"No se pudo resolver URL de Google News: {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                except Exception as e:
                    logger_service.log("ERROR", "URL_RESOLUTION_ERROR", f"Error resolviendo URL de Google News {url}: {e}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)

            # Fallback if Google News URL could not be resolved
            if original_candidate_url.startswith("https://news.google.com/rss/articles/") and resolved_url == original_candidate_url:
                logger_service.log("WARNING", "URL_RESOLUTION_FAILED_SKIP", f"{log_prefix}Saltando URL de Google News no resuelta: {original_candidate_url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                if not dry_run:
                    sheets_service.add_descarte(sheet_id, [
                        isbn, title, author, origin_query, original_candidate_url, item.get("title") or "", "no se pudo resolver URL de Google News", 0, get_now_madrid_str()
                    ])
                descartes_added += 1
                failed_extractions += 1
                continue

            # Use resolved URL as the real URL for all subsequent logic
            url = resolved_url

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
                            isbn, title, author, origin_query, url, item.get("title") or "", "fuera de rango de fechas", 0, get_now_madrid_str()
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
                        isbn, title, author, origin_query, url, item.get("title") or "", "duplicado", 0, get_now_madrid_str()
                    ])
                descartes_added += 1
                continue

            # Extract article content
            article_data = {}
            try:
                article_data = article_extractor.extract(url, provider_item=item)
                extracted_ok_count += 1
                
                # Log METADATA_EXTRACTED
                meta_extracted_detail = json.dumps({
                    "url": url,
                    "article_author": article_data.get("author", ""),
                    "publication_name": article_data.get("publication_name", ""),
                    "published_date": article_data.get("date", ""),
                    "author_source": article_data.get("author_source", "empty"),
                    "date_source": article_data.get("date_source", "empty")
                }, ensure_ascii=False)
                logger_service.log(
                    "INFO", "METADATA_EXTRACTED", 
                    f"{log_prefix}Metadatos extraídos para la URL: {url}", 
                    isbn=isbn, detail=meta_extracted_detail, sheet_id=sheet_id, run_id=run_id
                )
                
                # Log AUTHOR_FALLBACK_TO_EMPTY if empty
                if not article_data.get("author"):
                    logger_service.log(
                        "INFO", "AUTHOR_FALLBACK_TO_EMPTY", 
                        f"{log_prefix}No se encontró autor real para la URL, se deja vacío: {url}", 
                        isbn=isbn, sheet_id=sheet_id, run_id=run_id
                    )
            except Exception as e:
                err_msg = str(e)
                reason = "error HTTP" if "error HTTP" in err_msg else "extracción fallida"
                if "texto insuficiente" in err_msg:
                    reason = "texto insuficiente"

                logger_service.log("WARNING", "EXTRACTION_FAILED", f"{log_prefix}Error extrayendo {url}: {err_msg}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                if not dry_run:
                    sheets_service.add_descarte(sheet_id, [
                        isbn, title, author, origin_query, url, item.get("title") or "", reason, 0, get_now_madrid_str()
                    ])
                descartes_added += 1
                failed_extractions += 1
                continue

            # Fallback to candidate title or snippet if extracted ones are empty
            art_title = article_data.get("title") or item.get("title") or ""
            
            # Detect cookies or consent wall pages
            extracted_text = article_data.get("text") or ""
            if is_consent_or_cookie_page(extracted_text):
                logger_service.log("WARNING", "EXTRACTION_COOKIES_DETECTED", f"{log_prefix}Detectada página de cookies/consentimiento para URL: {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                if not dry_run:
                    sheets_service.add_descarte(sheet_id, [
                        isbn, title, author, origin_query, url, item.get("title") or "", "página de cookies/consent", 0, get_now_madrid_str()
                    ])
                descartes_added += 1
                failed_extractions += 1
                continue

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
                            isbn, title, author, origin_query, url, art_title, "fuera de rango de fechas", 0, get_now_madrid_str()
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
            self._check_cancellation(run_id, reviews_added=reviews_added)

            # Log validation input
            logger_service.log(
                level="INFO",
                action="OPENAI_VALIDATION_INPUT",
                message=f"Enviando candidato a OpenAI: {url}",
                isbn=isbn,
                detail=json.dumps({
                    "isbn": isbn,
                    "book_title": title,
                    "book_author": author,
                    "candidate_title": art_title,
                    "candidate_url": url,
                    "original_candidate_url": original_candidate_url,
                    "query": origin_query
                }),
                sheet_id=sheet_id,
                run_id=run_id
            )
            self._add_in_memory_log(
                run_id=run_id,
                level="INFO",
                action="OPENAI_VALIDATION_INPUT",
                message=f"Enviando a OpenAI: {url}",
                isbn=isbn,
                detail=json.dumps({
                    "isbn": isbn,
                    "book_title": title,
                    "book_author": author,
                    "candidate_title": art_title,
                    "candidate_url": url,
                    "original_candidate_url": original_candidate_url,
                    "query": origin_query
                })
            )

            try:
                metadata_detected = {
                    "article_author": article_data.get("author") or "",
                    "publication_name": article_data.get("publication_name") or "",
                    "published_date": article_data.get("date") or "",
                    "author_source": article_data.get("author_source") or "empty",
                    "date_source": article_data.get("date_source") or "empty"
                }

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
                    model_override=openai_model,
                    metadata_detected=metadata_detected
                )

                # Log validation result
                logger_service.log(
                    level="INFO",
                    action="OPENAI_VALIDATION_RESULT",
                    message=f"Resultado validación OpenAI para {url}: is_valid={analysis.get('is_valid')}, score={analysis.get('match_score')}",
                    isbn=isbn,
                    detail=json.dumps({
                        "book_title": title,
                        "candidate_title": art_title,
                        "candidate_url": url,
                        "is_match": analysis.get("is_valid", False),
                        "match_score": analysis.get("match_score", 0),
                        "reason": analysis.get("reason", ""),
                        "summary": analysis.get("summary", "")
                    }),
                    sheet_id=sheet_id,
                    run_id=run_id
                )
                self._add_in_memory_log(
                    run_id=run_id,
                    level="INFO",
                    action="OPENAI_VALIDATION_RESULT",
                    message=f"Resultado OpenAI: is_valid={analysis.get('is_valid')}, score={analysis.get('match_score')}",
                    isbn=isbn,
                    detail=json.dumps({
                        "book_title": title,
                        "candidate_title": art_title,
                        "candidate_url": url,
                        "is_match": analysis.get("is_valid", False),
                        "match_score": analysis.get("match_score", 0),
                        "reason": analysis.get("reason", ""),
                        "summary": analysis.get("summary", "")
                    })
                )
            except Exception as e:
                logger_service.log("ERROR", "OPENAI_FAILED", f"{log_prefix}Error OpenAI para {url}: {e}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                if not dry_run:
                    sheets_service.add_descarte(sheet_id, [
                        isbn, title, author, origin_query, url, art_title, "error OpenAI", 0, get_now_madrid_str()
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
                            isbn, title, author, origin_query, url, art_title, "fuera de rango de fechas", 0, get_now_madrid_str()
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
                            isbn, title, author, origin_query, url, art_title, "fuera de rango de fechas", 0, get_now_madrid_str()
                        ])
                    descartes_added += 1
                    continue

            is_valid = analysis.get("is_valid", False)
            score = analysis.get("match_score", 0)
            openai_reason = analysis.get("reason", "")
            
            # Normalise inconsistency: score >= 1 should imply is_valid = True.
            # score == 0 should imply is_valid = False.
            if score >= 1 and not is_valid:
                logger_service.log("WARNING", "OPENAI_COHERENCE_FIX", f"{log_prefix}Incoherencia OpenAI: is_valid=False pero score={score}. Se fuerza is_valid=True. URL: {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                is_valid = True
            elif score == 0 and is_valid:
                logger_service.log("WARNING", "OPENAI_COHERENCE_FIX", f"{log_prefix}Incoherencia OpenAI: is_valid=True pero score=0. Se fuerza is_valid=False. URL: {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                is_valid = False
            
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
                        isbn, title, author, origin_query, url, art_title, descarte_reason, score, get_now_madrid_str()
                    ])
                descartes_added += 1
            else:
                # Valid Review!
                openai_accepted_count += 1
                logger_service.log("INFO", "ARTICLE_ACCEPTED", f"{log_prefix}Reseña válida aceptada (score: {score}): {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                self._add_in_memory_log(run_id, "INFO", "ARTICLE_ACCEPTED", f"{log_prefix}Aceptada (score={score}): {url}", isbn=isbn)

                if not dry_run:
                    # Select best metadata
                    def select_best_author(det_auth: str, ai_auth: str) -> str:
                        det = str(det_auth or "").strip()
                        ai = str(ai_auth or "").strip()
                        invalids = ("titulo web", "título web", "autor web", "autor web ", "redacción", "redaccion", "")
                        if det and det.lower() not in invalids:
                            return det
                        if ai and ai.lower() not in invalids:
                            return ai
                        if det.lower() in ("redacción", "redaccion"):
                            return "Redacción"
                        if ai.lower() in ("redacción", "redaccion"):
                            return "Redacción"
                        return ""

                    def select_best_date(det_dt: str, ai_dt: str) -> str:
                        det = str(det_dt or "").strip()
                        ai = str(ai_dt or "").strip()
                        norm_det = normalize_date(det)
                        norm_ai = normalize_date(ai)
                        if norm_det:
                            return norm_det
                        if norm_ai:
                            return norm_ai
                        if det:
                            return det
                        if ai:
                            return ai
                        return ""

                    best_author = select_best_author(article_data.get("author"), analysis.get("publication_author"))
                    best_date = select_best_date(article_data.get("date"), analysis.get("publication_date"))
                    best_medium = analysis.get("publication_name") or article_data.get("publication_name") or ""

                    review_dict = {
                        "¿Publicar?": False,
                        "Estado publicación": "",
                        "Fecha intento publicación": "",
                        "Error publicación": "",
                        "ISBN": isbn,
                        "Título del libro": title,
                        "Autor del libro": author,
                        "URL": url,
                        "Título para Web": art_title,
                        "Autor para Web": best_author,
                        "Medio de publicación": best_medium,
                        "Fecha de publicación": best_date,
                        "Idioma original": analysis.get("language", ""),
                        "Categoría": analysis.get("category", ""),
                        "Resumen": analysis.get("summary", ""),
                        "Score de coincidencia": score,
                        "Tipo de contenido": analysis.get("content_type", ""),
                        "Fecha de extracción": get_now_madrid_str(),
                        "Hash deduplicación": prim_hash,
                        "Query": origin_query
                    }
                    sheets_service.add_review(sheet_id, review_dict)
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

        self._check_cancellation(run_id, reviews_added=reviews_added)
        if dry_run:
            # Update row in Libros to show proof run, but KEEP status as 'pendiente'
            try:
                sheets_service.update_book_status(
                    sheet_id=sheet_id,
                    row_index=row_index,
                    status="pendiente", # Keep pending!
                    last_run=f"{get_now_madrid_str()} (Prueba)",
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
                last_run=get_now_madrid_str(),
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

    def _auto_cleanup_descartes(self, sheet_id: str, run_id: str):
        try:
            config = sheets_service.get_config_dict(sheet_id)
            max_rows = int(config.get("DESCARTES_MAX_ROWS", getattr(settings, "DESCARTES_MAX_ROWS", 1000)))
            retention_days = int(config.get("DESCARTES_RETENTION_DAYS", getattr(settings, "DESCARTES_RETENTION_DAYS", 30)))
            res = sheets_service.cleanup_descartes(sheet_id, max_rows=max_rows, retention_days=retention_days)
            logger_service.log(
                level="INFO",
                action="DESCARTES_CLEANUP",
                message=res.get("message", "Limpieza de descartes completada."),
                sheet_id=sheet_id,
                run_id=run_id,
                detail=json.dumps({"deleted_count": res.get("deleted_count", 0), "remaining_count": res.get("remaining_count", 0)})
            )
        except Exception as e:
            import logging
            logging.getLogger("encuentro-noticias").warning(f"Error doing auto descartes cleanup: {e}")

    def _auto_compact_sheet(self, sheet_id: str, run_id: str):
        try:
            res = sheets_service.compact_sheet(sheet_id)
            if res.get("compacted_tabs"):
                compacted_summaries = [
                    f"{t['title']} (celdas liberadas: {t['cells_freed']})"
                    for t in res["compacted_tabs"]
                ]
                msg = f"Compactación automática de celdas completada. Pestañas: {', '.join(compacted_summaries)}."
            else:
                msg = "Compactación automática de celdas completada. No se requirieron cambios."
                
            logger_service.log(
                level="INFO",
                action="SHEET_COMPACT",
                message=msg,
                sheet_id=sheet_id,
                run_id=run_id,
                detail=json.dumps(res, ensure_ascii=False)
            )
        except Exception as e:
            import logging
            logging.getLogger("encuentro-noticias").warning(f"Error doing auto sheet compaction: {e}")

run_service = RunService()
