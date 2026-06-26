from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas import SetupResponse
from app.services.sheets_service import sheets_service
from app.config import settings
from app.dependencies import verify_admin_token

router = APIRouter(dependencies=[Depends(verify_admin_token)])

@router.post("/setup/ensure-sheet", response_model=SetupResponse)
def ensure_sheet():
    """
    Initializes the Google Sheet. Checks if all worksheets (Libros, Reseñas, Descartes, Logs, Config) 
    exist and initializes columns and default configuration values.
    """
    try:
        res = sheets_service.ensure_sheet(settings.GOOGLE_SHEET_ID)
        return SetupResponse(
            success=True,
            message="Google Sheet verified and prepared successfully.",
            sheet_id=res["sheet_id"],
            sheet_url=res["sheet_url"]
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Google Sheets setup failed: {str(e)}"
        )
