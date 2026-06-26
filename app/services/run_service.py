import uuid
import datetime
from typing import Dict, Any, List, Optional, Set
import threading
from urllib.parse import urlparse

from app.config import settings
from app.services.sheets_service import sheets_service
from app.services.query_builder import query_builder
from app.services.search_service import search_service
from app.services.article_extractor import article_extractor
from app.services.openai_analyzer import openai_analyzer
from app.services.deduplicator import deduplicator
from app.services.logger_service import logger_service

# In-memory storage for runs
current_runs: Dict[str, Dict[str, Any]] = {}

class RunService:
    def get_run_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        return current_runs.get(run_id)

    def trigger_run(self, limit_books: int = 10, dry_run: bool = False) -> str:
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
            "logs": []
        }
        
        # Start execution in a background thread
        thread = threading.Thread(
            target=self.execute_run,
            args=(run_id, limit_books, dry_run)
        )
        thread.daemon = True
        thread.start()
        
        return run_id

    def trigger_single_book_run(self, isbn: str, dry_run: bool = False) -> str:
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
            "logs": []
        }
        
        thread = threading.Thread(
            target=self.execute_single_book,
            args=(run_id, isbn, dry_run)
        )
        thread.daemon = True
        thread.start()
        
        return run_id

    def rebuild_dedupe_hashes(self, sheet_id: str) -> int:
        """
        Reads all rows from Reseñas, calculates their primary hashes,
        and writes them back if they are empty or incorrect.
        """
        reviews = sheets_service.get_all_reviews(sheet_id)
        updates = [] # List of tuples: (row_index, hash)
        
        for idx, row in enumerate(reviews, start=2): # Header is row 1
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

    def execute_run(self, run_id: str, limit_books: int, dry_run: bool):
        sheet_id = settings.GOOGLE_SHEET_ID
        log_prefix = "[PRUEBA] " if dry_run else ""
        self._add_in_memory_log(run_id, "INFO", "RUN_START", f"{log_prefix}Iniciando run global (limit_books={limit_books}, dry_run={dry_run})")
        logger_service.log("INFO", "RUN_START", f"{log_prefix}Iniciando ejecución {run_id}", sheet_id=sheet_id, run_id=run_id)

        try:
            current_runs[run_id]["status"] = "running"
            
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
                        dry_run=dry_run
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

    def execute_single_book(self, run_id: str, isbn: str, dry_run: bool):
        sheet_id = settings.GOOGLE_SHEET_ID
        log_prefix = "[PRUEBA] " if dry_run else ""
        self._add_in_memory_log(run_id, "INFO", "RUN_START", f"{log_prefix}Iniciando run individual para ISBN {isbn} (dry_run={dry_run})")
        logger_service.log("INFO", "RUN_START", f"{log_prefix}Iniciando ejecución individual {run_id} para ISBN {isbn}", sheet_id=sheet_id, run_id=run_id)

        try:
            current_runs[run_id]["status"] = "running"
            
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
                    dry_run=dry_run
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
        dry_run: bool = False
    ) -> str:
        """
        Runs the extraction and validation pipeline for a single book.
        """
        log_prefix = "[PRUEBA] " if dry_run else ""
        logger_service.log("INFO", "BOOK_PROCESS_START", f"{log_prefix}Procesando libro: '{title}' por {author}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
        self._add_in_memory_log(run_id, "INFO", "BOOK_PROCESS_START", f"{log_prefix}Procesando: '{title}' por {author}", isbn=isbn)

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

        # 1. Generate queries
        queries = query_builder.build_queries(title, author, isbn, review_domains=review_domains)
        self._add_in_memory_log(run_id, "INFO", "QUERIES_GENERATED", f"{log_prefix}Generadas {len(queries)} queries.", isbn=isbn)

        # 2. Gather candidate URLs tracking their query origins
        candidate_origin: Dict[str, str] = {}
        for q in queries:
            if len(candidate_origin) >= max_candidates:
                break
            found_urls = search_service.search(q, max_pages=max_pages)
            for url in found_urls:
                if url not in candidate_origin:
                    candidate_origin[url] = q
                    if len(candidate_origin) >= max_candidates:
                        break

        candidate_urls = list(candidate_origin.keys())

        # Log each candidate found with its originating query
        for url, origin_query in candidate_origin.items():
            logger_service.log(
                level="INFO",
                action="CANDIDATE_FOUND",
                message=f"{log_prefix}Candidato: {url}",
                isbn=isbn,
                detail=f"Query: {origin_query}",
                sheet_id=sheet_id,
                run_id=run_id
            )
            self._add_in_memory_log(
                run_id=run_id,
                level="INFO",
                action="CANDIDATE_FOUND",
                message=f"{log_prefix}Candidato: {url}",
                isbn=isbn,
                detail=f"Query: {origin_query}"
            )

        self._add_in_memory_log(run_id, "INFO", "SEARCH_COMPLETED", f"{log_prefix}Búsqueda finalizada. Encontradas {len(candidate_urls)} URLs candidatas.", isbn=isbn)
        logger_service.log("INFO", "SEARCH_COMPLETED", f"{log_prefix}Encontradas {len(candidate_urls)} URLs candidatas.", isbn=isbn, sheet_id=sheet_id, run_id=run_id)

        reviews_added = 0
        descartes_added = 0
        failed_extractions = 0
        observation = ""

        # 3. Process candidate URLs
        for url in candidate_urls:
            origin_query = candidate_origin.get(url, "")
            
            # Check primary duplicate
            norm_url = deduplicator.normalize_url(url)
            prim_hash = deduplicator.get_primary_hash(isbn, url)
            
            if prim_hash in existing_hashes:
                # Save to descartes
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

            # Validate secondary key with actual article title
            art_title = article_data.get("title") or ""
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
                logger_service.log("INFO", "ARTICLE_DISCARDED", f"{log_prefix}URL descartada ({descarte_reason}, score: {score}): {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                self._add_in_memory_log(run_id, "INFO", "ARTICLE_DISCARDED", f"{log_prefix}Descarte ({descarte_reason}, score={score}): {url}", isbn=isbn)
                
                if not dry_run:
                    sheets_service.add_descarte(sheet_id, [
                        isbn, title, author, origin_query, url, art_title, descarte_reason, score, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ])
                descartes_added += 1
            else:
                # Valid Review!
                logger_service.log("INFO", "ARTICLE_ACCEPTED", f"{log_prefix}Reseña válida aceptada (score: {score}): {url}", isbn=isbn, sheet_id=sheet_id, run_id=run_id)
                self._add_in_memory_log(run_id, "INFO", "ARTICLE_ACCEPTED", f"{log_prefix}Aceptada (score={score}): {url}", isbn=isbn)

                if not dry_run:
                    # Col schema for 'Reseñas' tab:
                    # ISBN, Título libro, Autor libro, Query, URL, URL norm, Título artículo, Título IA, Autor IA, Medio,
                    # Autor publicación, Fecha pub, Idioma, Categoría, Resumen, Score, Tipo contenido, Fecha ext, Hash, Estado
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
                    # Add to existing hashes to prevent duplicates within the same run
                    existing_hashes.add(prim_hash)
                    existing_secondary_keys.add(sec_key)
                
                reviews_added += 1

        # Determine final status
        final_status = "completado"
        if reviews_added == 0:
            final_status = "sin_resultados"
            observation = f"Búsqueda finalizada. 0 reseñas aceptadas. {descartes_added} descartes."
            if failed_extractions > 0:
                observation += f" ({failed_extractions} fallos de red/extracción)."
        else:
            observation = f"Proceso finalizado. Encontradas y guardadas {reviews_added} reseñas. {descartes_added} descartes."

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
            return "error"

        return final_status

run_service = RunService()
