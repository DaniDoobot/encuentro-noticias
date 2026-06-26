from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas import RunConfig, BookRunConfig, RunResponse, RunStatusResponse, DedupeRebuildResponse
from app.services.run_service import run_service
from app.config import settings
from app.dependencies import verify_admin_token

router = APIRouter(dependencies=[Depends(verify_admin_token)])

@router.post("/runs", response_model=RunResponse)
def post_runs(config: RunConfig):
    """
    Launches a background scraping and validation run for pending books in Google Sheets.
    """
    run_id = run_service.trigger_run(limit_books=config.limit_books, dry_run=config.dry_run)
    return RunResponse(
        run_id=run_id,
        message="Background scraping run started successfully."
    )

@router.post("/runs/book/{isbn}", response_model=RunResponse)
def post_run_book(isbn: str, config: BookRunConfig):
    """
    Launches a background scraping and validation run for a single book by ISBN.
    """
    run_id = run_service.trigger_single_book_run(isbn=isbn, dry_run=config.dry_run)
    return RunResponse(
        run_id=run_id,
        message=f"Background scraping run started successfully for ISBN {isbn}."
    )

@router.get("/runs/{run_id}", response_model=RunStatusResponse)
def get_run(run_id: str):
    """
    Returns the execution status and in-memory log logbook of a run.
    """
    status_data = run_service.get_run_status(run_id)
    if not status_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found."
        )
    return RunStatusResponse(
        run_id=status_data["run_id"],
        status=status_data["status"],
        books_total=status_data["books_total"],
        books_processed=status_data["books_processed"],
        books_completed=status_data["books_completed"],
        books_failed=status_data["books_failed"],
        books_no_results=status_data["books_no_results"],
        message=status_data["message"],
        logs=status_data["logs"]
    )

@router.post("/dedupe/rebuild", response_model=DedupeRebuildResponse)
def rebuild_dedupe():
    """
    Recalculates and populates deduplication hashes for all records in the Reseñas tab.
    """
    try:
        updated_count = run_service.rebuild_dedupe_hashes(settings.GOOGLE_SHEET_ID)
        return DedupeRebuildResponse(
            success=True,
            message=f"Deduplication hashes successfully rebuilt for {updated_count} rows.",
            hashes_processed=updated_count
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to rebuild dedupe hashes: {str(e)}"
        )
