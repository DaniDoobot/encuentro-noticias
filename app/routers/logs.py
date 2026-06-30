import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from app.config import settings
from app.dependencies import verify_admin_token
from app.services.sheets_service import sheets_service
from app.services.logger_service import logger_service

logger = logging.getLogger("encuentro-noticias")

router = APIRouter(dependencies=[Depends(verify_admin_token)])

class CleanupLogsResponse(BaseModel):
    success: bool
    message: str
    deleted_count: int
    remaining_count: int

@router.post("/logs/cleanup", response_model=CleanupLogsResponse)
def post_logs_cleanup():
    """
    Cleans up old logs in the 'Logs' worksheet of Google Sheets.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    
    # Read config to get LOG_RETENTION_DAYS and LOG_MAX_ROWS
    try:
        config = sheets_service.get_config_dict(sheet_id)
        max_rows = int(config.get("LOG_MAX_ROWS", settings.LOG_MAX_ROWS))
        retention_days = int(config.get("LOG_RETENTION_DAYS", settings.LOG_RETENTION_DAYS))
    except Exception as e_cfg:
        logger.warning(f"Error reading log retention config: {e_cfg}. Using defaults.")
        max_rows = settings.LOG_MAX_ROWS
        retention_days = settings.LOG_RETENTION_DAYS

    try:
        res = sheets_service.cleanup_logs(sheet_id, max_rows=max_rows, retention_days=retention_days)
        
        # Log this action as well
        logger_service.log(
            level="INFO",
            action="LOGS_CLEANUP",
            message=res.get("message", "Limpieza de logs completada."),
            sheet_id=sheet_id,
            detail={"deleted_count": res.get("deleted_count", 0), "remaining_count": res.get("remaining_count", 0)}
        )
        logger_service.flush_log_batch(sheet_id)
        
        return CleanupLogsResponse(
            success=True,
            message=res.get("message", "Logs limpiados."),
            deleted_count=res.get("deleted_count", 0),
            remaining_count=res.get("remaining_count", 0)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cleanup logs: {str(e)}"
        )

class DeleteAllLogsResponse(BaseModel):
    success: bool
    message: str
    deleted_count: int

@router.post("/logs/delete-all", response_model=DeleteAllLogsResponse)
def post_logs_delete_all():
    """
    Deletes all log entries in the 'Logs' worksheet of Google Sheets.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    try:
        res = sheets_service.clear_all_rows(sheet_id, "Logs")
        return DeleteAllLogsResponse(
            success=True,
            message="Todos los logs fueron eliminados correctamente.",
            deleted_count=res.get("deleted_count", 0)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete all logs: {str(e)}"
        )
