from fastapi import APIRouter
from app.schemas import HealthResponse

router = APIRouter()

@router.get("/health", response_model=HealthResponse)
def health():
    """
    Public health check endpoint.
    """
    return HealthResponse(status="ok")
