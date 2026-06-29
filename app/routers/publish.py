import datetime
from typing import Dict, Any, List
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
import gspread

from app.config import settings
from app.dependencies import verify_admin_token
from app.services.sheets_service import sheets_service
from app.services.wordpress_publisher import wordpress_publisher
from app.services.logger_service import logger_service

logger = logging.getLogger("encuentro-noticias")

router = APIRouter(dependencies=[Depends(verify_admin_token)])

class PublishReviewsRequest(BaseModel):
    dry_run: bool = True

class PublishReviewsResponse(BaseModel):
    success: bool
    message: str
    published_count: int
    errors_count: int
    unselected_count: int

@router.post("/publish/reviews", response_model=PublishReviewsResponse)
def post_publish_reviews(req: PublishReviewsRequest):
    """
    Reads marked reviews from 'Reseñas por publicar', publishes them,
    and moves successfully published ones to 'Reseñas publicadas'.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    dry_run = req.dry_run
    
    try:
        config = sheets_service.get_config_dict(sheet_id)
    except Exception as e_cfg:
        logger.error(f"Error reading config for publication: {e_cfg}")
        config = {}

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
    except Exception as e_read:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error reading reviews from sheet: {str(e_read)}"
        )

    published_count = 0
    errors_count = 0
    unselected_count = 0
    
    rows_to_delete: List[int] = []
    rows_to_append_pub: List[List[Any]] = []
    cells_to_update: List[gspread.Cell] = []
    
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for idx, row in enumerate(records):
        row_idx = idx + 2  # 1-indexed, headers is row 1
        
        # Skip if already published (has WordPress ID)
        wp_id = str(row.get("WordPress ID", "")).strip()
        if wp_id:
            continue
            
        is_marked = str(row.get("¿Publicar?", "")).strip().upper() == "TRUE"
        
        if not is_marked:
            unselected_count += 1
            # Mark as "No publicada" if it wasn't already
            if str(row.get("Estado publicación", "")).strip() != "No publicada":
                cells_to_update.append(gspread.Cell(row=row_idx, col=2, value="No publicada"))
                cells_to_update.append(gspread.Cell(row=row_idx, col=3, value=""))
                cells_to_update.append(gspread.Cell(row=row_idx, col=4, value=""))
                cells_to_update.append(gspread.Cell(row=row_idx, col=5, value=""))
                cells_to_update.append(gspread.Cell(row=row_idx, col=6, value=""))
                cells_to_update.append(gspread.Cell(row=row_idx, col=7, value=""))
            continue
            
        # Attempt WordPress publication
        pub_res = wordpress_publisher.publish_review(row, config, dry_run=dry_run)
        
        if pub_res.get("success"):
            published_count += 1
            wpid = pub_res.get("wordpress_id", "")
            wpurl = pub_res.get("wordpress_url", "")
            
            if dry_run:
                # Update in place for dry_run
                cells_to_update.append(gspread.Cell(row=row_idx, col=2, value="Publicada"))
                cells_to_update.append(gspread.Cell(row=row_idx, col=3, value=now_str))
                cells_to_update.append(gspread.Cell(row=row_idx, col=4, value=now_str))
                cells_to_update.append(gspread.Cell(row=row_idx, col=5, value=wpid))
                cells_to_update.append(gspread.Cell(row=row_idx, col=6, value=wpurl))
                cells_to_update.append(gspread.Cell(row=row_idx, col=7, value=""))
            else:
                # Add to published tab and collect index to delete
                headers_ordered = [
                    "¿Publicar?", "Estado publicación", "Fecha intento publicación", "Fecha publicación",
                    "WordPress ID", "WordPress URL", "Error publicación", "ISBN", "Título del libro",
                    "Autor del libro", "Query", "URL", "URL normalizada", "Título del artículo",
                    "Título del libro detectado por IA", "Autor del libro detectado por IA",
                    "Medio de publicación", "Autor de la publicación", "Fecha de publicación",
                    "Idioma original", "Categoría", "Resumen", "Score de coincidencia",
                    "Tipo de contenido", "Fecha de extracción", "Hash deduplicación", "Estado"
                ]
                row_vals = []
                for h in headers_ordered:
                    if h == "¿Publicar?":
                        row_vals.append("TRUE")
                    elif h == "Estado publicación":
                        row_vals.append("Publicada")
                    elif h == "Fecha intento publicación":
                        row_vals.append(now_str)
                    elif h == "Fecha publicación":
                        row_vals.append(now_str)
                    elif h == "WordPress ID":
                        row_vals.append(wpid)
                    elif h == "WordPress URL":
                        row_vals.append(wpurl)
                    elif h == "Error publicación":
                        row_vals.append("")
                    else:
                        row_vals.append(str(row.get(h, "")))
                
                rows_to_append_pub.append(row_vals)
                rows_to_delete.append(row_idx)
        else:
            errors_count += 1
            err_msg = pub_res.get("error", "Error al publicar")
            cells_to_update.append(gspread.Cell(row=row_idx, col=2, value="Error"))
            cells_to_update.append(gspread.Cell(row=row_idx, col=3, value=now_str))
            cells_to_update.append(gspread.Cell(row=row_idx, col=4, value=""))
            cells_to_update.append(gspread.Cell(row=row_idx, col=5, value=""))
            cells_to_update.append(gspread.Cell(row=row_idx, col=6, value=""))
            cells_to_update.append(gspread.Cell(row=row_idx, col=7, value=err_msg))

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
                ws_pub.append_rows(rows_to_append_pub)
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
    logger_service.log(
        level="INFO",
        action="PUBLISH_REVIEWS",
        message=summary_msg,
        sheet_id=sheet_id,
        detail={
            "dry_run": dry_run,
            "published": published_count,
            "errors": errors_count,
            "unselected": unselected_count
        }
    )
    logger_service.flush_log_batch(sheet_id)

    return PublishReviewsResponse(
        success=True,
        message="Simulación completada en dry_run." if dry_run else "Proceso de publicación completado con éxito.",
        published_count=published_count,
        errors_count=errors_count,
        unselected_count=unselected_count
    )

@router.post("/publish/test-wordpress")
def post_publish_test_wordpress():
    """
    Tests connection credentials and permissions of WordPress.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    try:
        config = sheets_service.get_config_dict(sheet_id)
    except Exception:
        config = {}
        
    res = wordpress_publisher.test_connection(config)
    if not res.get("success"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=res.get("message", "Error de conexión con WordPress.")
        )
    return res
