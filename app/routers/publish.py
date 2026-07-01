import datetime
from typing import Dict, Any, List, Optional, Set
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
import gspread
import threading
import uuid

from app.config import settings
from app.dependencies import verify_admin_token
from app.services.sheets_service import sheets_service, get_now_madrid_str
from app.services.wordpress_publisher import wordpress_publisher
from app.services.logger_service import logger_service

logger = logging.getLogger("encuentro-noticias")

def is_row_real(row: dict) -> bool:
    # Check if any of the target fields has non-empty text
    target_fields = [
        "URL", "URL normalizada", "Título del artículo", 
        "Título del libro", "Autor del libro", "ISBN", 
        "Resumen", "Hash deduplicación", 
        "Título para Web", "Autor para Web",
        "Título del libro detectado por IA", "Autor del libro detectado por IA",
        "WordPress ID", "WordPress URL", "Fecha publicación", "Fecha de publicación"
    ]
    for field in target_fields:
        if str(row.get(field, "")).strip():
            return True
    return False

router = APIRouter(dependencies=[Depends(verify_admin_token)])

# In-memory publication runs
current_publications: Dict[str, Dict[str, Any]] = {}
cancelled_publications: Set[str] = set()

class PublishReviewsRequest(BaseModel):
    dry_run: bool = True
    background: bool = False

class PublishReviewsResponse(BaseModel):
    success: bool
    message: str
    sheet_id: str
    worksheet_name: str
    total_rows_read: int
    non_empty_rows_detected: int
    selected_rows: int
    unselected_rows: int
    skipped_empty_rows: int
    skipped_already_published: int
    published_count: int
    errors_count: int
    dry_run: bool
    debug_examples: List[str]
    publish_id: Optional[str] = None

def execute_publication_sync(publish_id: Optional[str], sheet_id: str, dry_run: bool) -> PublishReviewsResponse:
    try:
        client = sheets_service.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        ws_to_pub = spreadsheet.worksheet("Reseñas por publicar")
        ws_pub = spreadsheet.worksheet("Reseñas publicadas")
    except Exception as e_sheet:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Google Sheets connection or worksheet not found: {str(e_sheet)}"
        )

    try:
        records = ws_to_pub.get_all_records()
        headers_to_pub = ws_to_pub.row_values(1)
        headers_pub = ws_pub.row_values(1)
    except Exception as e_read:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error reading headers or reviews from sheet: {str(e_read)}"
        )

    def get_col_index(header_name: str) -> Optional[int]:
        try:
            return headers_to_pub.index(header_name) + 1
        except ValueError:
            return None

    col_status = get_col_index("Estado publicación") or 2
    col_attempt = get_col_index("Fecha intento publicación") or 3
    col_error = get_col_index("Error publicación") or 4

    published_count = 0
    errors_count = 0
    unselected_count = 0
    
    total_rows_read = len(records)
    non_empty_rows_detected = 0
    selected_rows = 0
    unselected_rows = 0
    skipped_empty_rows = 0
    skipped_already_published = 0
    debug_examples = []

    rows_to_delete: List[int] = []
    rows_to_append_pub: List[List[Any]] = []
    cells_to_update: List[gspread.Cell] = []
    
    now_str = get_now_madrid_str()
    
    try:
        config = sheets_service.get_config_dict(sheet_id)
    except Exception as e_cfg:
        logger.error(f"Error reading config for publication: {e_cfg}")
        config = {}

    for idx, row in enumerate(records):
        row_idx = idx + 2  # 1-indexed, headers is row 1
        
        # Check cancellation
        if publish_id and publish_id in cancelled_publications:
            logger.info(f"Proceso de publicación {publish_id} cancelado cooperativamente.")
            break

        # Skip empty rows
        if not is_row_real(row):
            skipped_empty_rows += 1
            continue
            
        non_empty_rows_detected += 1
        
        # Capture first 5 debug examples
        if len(debug_examples) < 5:
            book_t = row.get("Título del libro") or row.get("Título para Web") or "Sin título"
            is_marked_t = str(row.get("¿Publicar?", "")).strip().upper() in ("TRUE", "1") or row.get("¿Publicar?") is True
            debug_examples.append(f"Row {row_idx}: {book_t} (¿Publicar?: {is_marked_t})")
            
        # Skip if already published (has WordPress ID)
        wp_id = str(row.get("WordPress ID", "")).strip()
        if wp_id:
            skipped_already_published += 1
            continue
            
        is_marked = str(row.get("¿Publicar?", "")).strip().upper() in ("TRUE", "1") or row.get("¿Publicar?") is True
        
        if is_marked:
            selected_rows += 1
        else:
            unselected_rows += 1
            
        if not is_marked:
            unselected_count += 1
            # Mark as "No publicada" if it wasn't already
            if str(row.get("Estado publicación", "")).strip() != "No publicada":
                cells_to_update.append(gspread.Cell(row=row_idx, col=col_status, value="No publicada"))
                cells_to_update.append(gspread.Cell(row=row_idx, col=col_attempt, value=""))
                cells_to_update.append(gspread.Cell(row=row_idx, col=col_error, value=""))
            continue
            
        # Attempt WordPress publication
        pub_res = wordpress_publisher.publish_review(row, config, dry_run=dry_run, sheet_id=sheet_id, run_id=publish_id or "")
        
        if pub_res.get("success"):
            published_count += 1
            wpid = pub_res.get("wordpress_id", "")
            wpurl = pub_res.get("wordpress_url", "")
            
            if dry_run:
                # Update in place for dry_run
                cells_to_update.append(gspread.Cell(row=row_idx, col=col_status, value="Publicada"))
                cells_to_update.append(gspread.Cell(row=row_idx, col=col_attempt, value=now_str))
                cells_to_update.append(gspread.Cell(row=row_idx, col=col_error, value=""))
            else:
                # Add to published tab and collect index to delete
                row_vals = []
                for h in headers_pub:
                    if h == "Fecha publicación":
                        row_vals.append(now_str)
                    elif h == "WordPress ID":
                        row_vals.append(wpid)
                    elif h == "WordPress URL":
                        row_vals.append(wpurl)
                    elif h == "Estado publicación":
                        row_vals.append("Publicada")
                    elif h == "Fecha intento publicación":
                        row_vals.append(now_str)
                    elif h == "Error publicación":
                        row_vals.append("")
                    else:
                        val = row.get(h)
                        if val is None:
                            # Try fallbacks for renamed fields
                            if h == "Título para Web":
                                val = row.get("Título del libro detectado por IA") or row.get("Título del artículo")
                            elif h == "Autor para Web":
                                val = row.get("Autor del libro detectado por IA") or row.get("Autor de la publicación")
                        
                        val_str = str(val or "").strip()
                        if val_str.lower() in ("titulo web", "título web", "autor web", "autor web "):
                            val_str = ""
                        row_vals.append(val_str)
                
                rows_to_append_pub.append(row_vals)
                rows_to_delete.append(row_idx)
        else:
            errors_count += 1
            err_msg = pub_res.get("error", "Error al publicar")
            cells_to_update.append(gspread.Cell(row=row_idx, col=col_status, value="Error"))
            cells_to_update.append(gspread.Cell(row=row_idx, col=col_attempt, value=now_str))
            cells_to_update.append(gspread.Cell(row=row_idx, col=col_error, value=err_msg))

    # Commit cell updates
    if cells_to_update:
        try:
            chunk_size = 500
            for i in range(0, len(cells_to_update), chunk_size):
                chunk = cells_to_update[i:i + chunk_size]
                ws_to_pub.update_cells(chunk)
        except Exception as e_cells:
            logger.error(f"Error committing cell updates in Reseñas por publicar: {e_cells}")
            
    if not dry_run:
        # Append to published worksheet
        if rows_to_append_pub:
            try:
                sheets_service.add_published_reviews(sheet_id, rows_to_append_pub)
            except Exception as e_app:
                logger.error(f"Error appending to Reseñas publicadas: {e_app}")
        # Delete rows in reverse order to keep correct indexing
        if rows_to_delete:
            try:
                delete_reqs = []
                for r_idx in sorted(rows_to_delete, reverse=True):
                    delete_reqs.append({
                        "deleteDimension": {
                            "range": {
                                "sheetId": ws_to_pub.id,
                                "dimension": "ROWS",
                                "startIndex": r_idx - 1,
                                "endIndex": r_idx
                            }
                        }
                    })
                spreadsheet.batch_update({"requests": delete_reqs})
            except Exception as e_del:
                logger.error(f"Error deleting published rows from Reseñas por publicar: {e_del}")

    # Log action
    summary_msg = f"Publicación finalizada. Publicadas: {published_count}, Errores: {errors_count}, No marcadas: {unselected_count}."
    if publish_id and publish_id in cancelled_publications:
        summary_msg = f"Publicación cancelada por el usuario. Publicadas antes de cancelar: {published_count}, Errores: {errors_count}."

    logger_service.log(
        level="INFO",
        action="PUBLISH_REVIEWS",
        message=summary_msg,
        sheet_id=sheet_id,
        detail={
            "dry_run": dry_run,
            "published": published_count,
            "errors": errors_count,
            "unselected": unselected_count,
            "cancelled": bool(publish_id and publish_id in cancelled_publications)
        }
    )
    logger_service.flush_log_batch(sheet_id)

    return PublishReviewsResponse(
        success=True,
        message="Simulación completada en dry_run." if dry_run else ("Publicación cancelada por el usuario." if publish_id and publish_id in cancelled_publications else "Proceso de publicación completado con éxito."),
        sheet_id=sheet_id,
        worksheet_name="Reseñas por publicar",
        total_rows_read=total_rows_read,
        non_empty_rows_detected=non_empty_rows_detected,
        selected_rows=selected_rows,
        unselected_rows=unselected_rows,
        skipped_empty_rows=skipped_empty_rows,
        skipped_already_published=skipped_already_published,
        published_count=published_count,
        errors_count=errors_count,
        dry_run=dry_run,
        debug_examples=debug_examples,
        publish_id=publish_id
    )

def execute_publication_background(publish_id: str, sheet_id: str, dry_run: bool):
    try:
        res = execute_publication_sync(publish_id, sheet_id, dry_run)
        if publish_id in cancelled_publications:
            current_publications[publish_id]["status"] = "cancelled"
            current_publications[publish_id]["message"] = "Publicación cancelada por el usuario."
        else:
            current_publications[publish_id]["status"] = "completed"
            current_publications[publish_id]["message"] = "Publicación en segundo plano completada."
        current_publications[publish_id]["published_count"] = res.published_count
        current_publications[publish_id]["errors_count"] = res.errors_count
        current_publications[publish_id]["details"] = res.model_dump()
    except Exception as e:
        logger.error(f"Error in background publication {publish_id}: {e}")
        current_publications[publish_id]["status"] = "failed"
        current_publications[publish_id]["message"] = f"Fallo en la publicación: {str(e)}"

@router.post("/publish/reviews", response_model=PublishReviewsResponse)
def post_publish_reviews(req: PublishReviewsRequest):
    """
    Reads marked reviews from 'Reseñas por publicar', publishes them,
    and moves successfully published ones to 'Reseñas publicadas'.
    Supports optional background execution if background = true in payload.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    dry_run = req.dry_run
    
    if req.background:
        publish_id = f"pub_{uuid.uuid4().hex[:8]}"
        current_publications[publish_id] = {
            "status": "running",
            "published_count": 0,
            "errors_count": 0,
            "message": "Iniciando proceso de publicación en segundo plano...",
            "details": None
        }
        thread = threading.Thread(
            target=execute_publication_background,
            args=(publish_id, sheet_id, dry_run)
        )
        thread.daemon = True
        thread.start()
        
        return PublishReviewsResponse(
            success=True,
            message="Proceso de publicación iniciado en segundo plano.",
            sheet_id=sheet_id,
            worksheet_name="Reseñas por publicar",
            total_rows_read=0,
            non_empty_rows_detected=0,
            selected_rows=0,
            unselected_rows=0,
            skipped_empty_rows=0,
            skipped_already_published=0,
            published_count=0,
            errors_count=0,
            dry_run=dry_run,
            debug_examples=[],
            publish_id=publish_id
        )
    else:
        return execute_publication_sync(None, sheet_id, dry_run)

class PublishStatusResponse(BaseModel):
    publish_id: str
    status: str
    published_count: int
    errors_count: int
    message: str
    details: Optional[Dict[str, Any]] = None

@router.get("/publish/{publish_id}/status", response_model=PublishStatusResponse)
def get_publish_status(publish_id: str):
    if publish_id not in current_publications:
        raise HTTPException(status_code=404, detail="Proceso de publicación no encontrado.")
    pub = current_publications[publish_id]
    return PublishStatusResponse(
        publish_id=publish_id,
        status=pub["status"],
        published_count=pub["published_count"],
        errors_count=pub["errors_count"],
        message=pub["message"],
        details=pub.get("details")
    )

@router.post("/publish/{publish_id}/cancel")
def post_publish_cancel(publish_id: str):
    if publish_id in current_publications:
        cancelled_publications.add(publish_id)
        current_publications[publish_id]["status"] = "cancelled"
        current_publications[publish_id]["message"] = "Publicación cancelada por el usuario."
        return {"success": True, "message": f"Publicación {publish_id} cancelada cooperativamente."}
    return {"success": False, "message": f"Proceso de publicación '{publish_id}' no encontrado."}

@router.post("/publish/test-wordpress")
def post_publish_test_wordpress():
    """
    Tests connection credentials and permissions of WordPress, returning a detailed diagnostic report.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    try:
        config = sheets_service.get_config_dict(sheet_id)
    except Exception:
        config = {}
        
    res = wordpress_publisher.diagnose_connection(config)
    return res

class TestDraftRequest(BaseModel):
    confirm_create_draft: bool = False

class TestDraftResponse(BaseModel):
    success: bool
    wordpress_id: Optional[int] = None
    wordpress_url: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None

@router.post("/publish/test-draft", response_model=TestDraftResponse)
def post_publish_test_draft(req: TestDraftRequest):
    """
    Creates a draft post in WordPress to verify publishing credentials and permissions.
    """
    if not req.confirm_create_draft:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debe confirmar la creación del borrador enviando confirm_create_draft = true en el cuerpo de la petición."
        )
        
    sheet_id = settings.GOOGLE_SHEET_ID
    try:
        config = sheets_service.get_config_dict(sheet_id)
    except Exception:
        config = {}
        
    title = "Prueba Encuentro Noticias API"
    content = "Este es un borrador de prueba creado desde el backend."
    
    res = wordpress_publisher.publish_draft_post(title, content, config)
    if not res.get("success"):
        return TestDraftResponse(
            success=False,
            error=res.get("error", "Error al crear el borrador.")
        )
        
    wpid = res.get("wordpress_id")
    try:
        wpid_int = int(wpid) if wpid else None
    except ValueError:
        wpid_int = None
        
    return TestDraftResponse(
        success=True,
        wordpress_id=wpid_int,
        wordpress_url=res.get("wordpress_url"),
        status=res.get("status")
    )

class CleanupEmptyRowsRequest(BaseModel):
    worksheets: Optional[List[str]] = None

class CleanupEmptyRowsResponse(BaseModel):
    success: bool
    message: str
    cleaned_details: Dict[str, int]

@router.post("/reviews/cleanup-empty-publication-rows", response_model=CleanupEmptyRowsResponse)
def post_cleanup_empty_rows(req: Optional[CleanupEmptyRowsRequest] = None):
    """
    Cleans up empty/false rows in the specified tabs (default: both Reseñas por publicar and Reseñas publicadas) of Google Sheets.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    worksheets = req.worksheets if req else None
    try:
        cleaned_details = sheets_service.cleanup_empty_publication_rows(sheet_id, worksheets)
        total_cleaned = sum(cleaned_details.values())
        
        logger_service.log(
            level="INFO",
            action="REVIEWS_CLEANUP",
            message=f"Limpieza de filas de publicación completada: {total_cleaned} filas limpiadas en total.",
            sheet_id=sheet_id,
            detail={"cleaned_details": cleaned_details}
        )
        logger_service.flush_log_batch(sheet_id)
        
        ws_str = ", ".join(cleaned_details.keys())
        return CleanupEmptyRowsResponse(
            success=True,
            message=f"Se limpiaron {total_cleaned} filas vacías en total en las pestañas: {ws_str}.",
            cleaned_details=cleaned_details
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cleanup empty publication rows: {str(e)}"
        )
