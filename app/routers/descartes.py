import logging
import json
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from app.config import settings
from app.dependencies import verify_admin_token
from app.services.sheets_service import sheets_service
from app.services.logger_service import logger_service

logger = logging.getLogger("encuentro-noticias")

router = APIRouter(dependencies=[Depends(verify_admin_token)])

class CleanupDescartesResponse(BaseModel):
    success: bool
    deleted_rows: int
    remaining_rows: int
    max_rows: int
    retention_days: int

@router.post("/descartes/cleanup", response_model=CleanupDescartesResponse)
def post_descartes_cleanup():
    """
    Cleans up old descartes in the 'Descartes' worksheet of Google Sheets.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    
    # Read config to get DESCARTES_RETENTION_DAYS and DESCARTES_MAX_ROWS
    try:
        config = sheets_service.get_config_dict(sheet_id)
        max_rows = int(config.get("DESCARTES_MAX_ROWS", getattr(settings, "DESCARTES_MAX_ROWS", 1000)))
        retention_days = int(config.get("DESCARTES_RETENTION_DAYS", getattr(settings, "DESCARTES_RETENTION_DAYS", 30)))
    except Exception as e_cfg:
        logger.warning(f"Error reading descartes retention config: {e_cfg}. Using defaults.")
        max_rows = getattr(settings, "DESCARTES_MAX_ROWS", 1000)
        retention_days = getattr(settings, "DESCARTES_RETENTION_DAYS", 30)

    try:
        res = sheets_service.cleanup_descartes(sheet_id, max_rows=max_rows, retention_days=retention_days)
        
        # Log this action
        logger_service.log(
            level="INFO",
            action="DESCARTES_CLEANUP",
            message=res.get("message", "Limpieza de descartes completada."),
            sheet_id=sheet_id,
            detail=json.dumps({"deleted_count": res.get("deleted_count", 0), "remaining_count": res.get("remaining_count", 0)})
        )
        logger_service.flush_log_batch(sheet_id)
        
        return CleanupDescartesResponse(
            success=True,
            deleted_rows=res.get("deleted_count", 0),
            remaining_rows=res.get("remaining_count", 0),
            max_rows=max_rows,
            retention_days=retention_days
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cleanup descartes: {str(e)}"
        )
