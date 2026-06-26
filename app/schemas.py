from pydantic import BaseModel, Field
from typing import Optional, Dict, List, Any

class HealthResponse(BaseModel):
    status: str

class SetupResponse(BaseModel):
    success: bool
    message: str
    sheet_id: str
    sheet_url: str

class RunConfig(BaseModel):
    limit_books: Optional[int] = Field(default=10, description="Max books to process in this run")
    dry_run: Optional[bool] = Field(default=False, description="If true, does search/extraction but doesn't modify google sheets reviews/descartes or change book status to completed/error")

class BookRunConfig(BaseModel):
    dry_run: Optional[bool] = Field(default=False, description="If true, does search/extraction but doesn't modify google sheets reviews/descartes or change book status to completed/error")

class RunResponse(BaseModel):
    run_id: str
    message: str

class RunLog(BaseModel):
    timestamp: str
    level: str
    action: str
    message: str
    detail: Optional[str] = None

class RunStatusResponse(BaseModel):
    run_id: str
    status: str
    books_total: int
    books_processed: int
    books_completed: int
    books_failed: int
    books_no_results: int
    message: str
    logs: List[Dict[str, Any]]

class BooksStatusResponse(BaseModel):
    pendiente: int
    procesando: int
    completado: int
    sin_resultados: int
    error: int
    total: int

class DedupeRebuildResponse(BaseModel):
    success: bool
    message: str
    hashes_processed: int
