from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas import BooksStatusResponse
from app.services.sheets_service import sheets_service
from app.config import settings
from app.dependencies import verify_admin_token

router = APIRouter(dependencies=[Depends(verify_admin_token)])

@router.get("/books/status", response_model=BooksStatusResponse)
def get_books_status():
    """
    Returns a count summary of all books registered in the Libros tab, grouped by state.
    """
    try:
        summary = sheets_service.get_books_status_summary(settings.GOOGLE_SHEET_ID)
        return BooksStatusResponse(
            pendiente=summary["pendiente"],
            procesando=summary["procesando"],
            completado=summary["completado"],
            sin_resultados=summary["sin_resultados"],
            error=summary["error"],
            total=summary["total"]
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch books summary: {str(e)}"
        )
