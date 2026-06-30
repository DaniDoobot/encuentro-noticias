from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from app.schemas import (
    RunConfig, BookRunConfig, RunResponse, RunStatusResponse, 
    DedupeRebuildResponse, DebugSearchRequest, DebugSearchResponse, 
    ProviderDebugResult
)
from app.services.run_service import run_service
from app.config import settings
from app.dependencies import verify_admin_token
from app.services.search_providers import (
    DuckDuckGoSearchProvider,
    BingHtmlSearchProvider,
    GoogleNewsRssSearchProvider,
    SerpApiSearchProvider,
    DataForSeoSearchProvider
)
from app.services.sheets_service import sheets_service

router = APIRouter(dependencies=[Depends(verify_admin_token)])

import datetime

def validate_iso_date(date_str: Optional[str], name: str) -> Optional[datetime.date]:
    if not date_str:
        return None
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Parameter {name} must be in YYYY-MM-DD format."
        )

@router.post("/runs", response_model=RunResponse)
def post_runs(config: RunConfig):
    """
    Launches a background scraping and validation run for pending books in Google Sheets.
    """
    d_min = validate_iso_date(config.date_min, "date_min")
    d_max = validate_iso_date(config.date_max, "date_max")
    if d_min and d_max and d_min > d_max:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_min cannot be greater than date_max."
        )
        
    run_id = run_service.trigger_run(
        limit_books=config.limit_books, 
        dry_run=config.dry_run,
        date_min=config.date_min,
        date_max=config.date_max,
        include_unknown_dates=config.include_unknown_dates
    )
    return RunResponse(
        run_id=run_id,
        message="Background scraping run started successfully."
    )

@router.post("/runs/book/{isbn}", response_model=RunResponse)
def post_run_book(isbn: str, config: BookRunConfig):
    """
    Launches a background scraping and validation run for a single book by ISBN.
    """
    d_min = validate_iso_date(config.date_min, "date_min")
    d_max = validate_iso_date(config.date_max, "date_max")
    if d_min and d_max and d_min > d_max:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_min cannot be greater than date_max."
        )
        
    run_id = run_service.trigger_single_book_run(
        isbn=isbn, 
        dry_run=config.dry_run,
        date_min=config.date_min,
        date_max=config.date_max,
        include_unknown_dates=config.include_unknown_dates
    )
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
        logs=status_data["logs"],
        books_details=status_data.get("books_details"),
        books_rows_read=status_data.get("books_rows_read"),
        books_pending_detected=status_data.get("books_pending_detected"),
        books_skipped_missing_title=status_data.get("books_skipped_missing_title"),
        books_skipped_non_pending_status=status_data.get("books_skipped_non_pending_status")
    )

@router.post("/runs/{run_id}/cancel")
def post_cancel_run(run_id: str):
    """
    Cancels a running background execution.
    """
    success = run_service.cancel_run(run_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found or not in running state."
        )
    return {"success": True, "message": f"Run {run_id} has been marked for cancellation."}

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

@router.post("/debug/search", response_model=DebugSearchResponse)
def debug_search(req: DebugSearchRequest):
    """
    Manual search provider testing endpoint. Does not write to Google Sheets or call OpenAI.
    """
    results = []
    
    # Load config credentials
    try:
        config = sheets_service.get_config_dict(settings.GOOGLE_SHEET_ID)
    except Exception:
        config = {}

    api_key = config.get("SERPAPI_API_KEY", settings.SERPAPI_API_KEY)
    login = config.get("DATAFORSEO_LOGIN", settings.DATAFORSEO_LOGIN)
    password = config.get("DATAFORSEO_PASSWORD", settings.DATAFORSEO_PASSWORD)

    ddg = DuckDuckGoSearchProvider()
    bing = BingHtmlSearchProvider()
    rss = GoogleNewsRssSearchProvider()
    serpapi = SerpApiSearchProvider()
    dataforseo = DataForSeoSearchProvider()
    
    provider_map = {
        "duckduckgo": ddg,
        "binghtml": bing,
        "googlenewsrss": rss,
        "serpapi": serpapi,
        "dataforseo": dataforseo
    }
    
    for p_name in req.providers:
        p_key = p_name.lower().strip()
        provider = provider_map.get(p_key)
        if not provider:
            results.append(ProviderDebugResult(
                provider=p_name,
                status="error",
                status_code=400,
                urls=[],
                debug={"error": f"Provider {p_name} is not supported or not found."}
            ))
            continue
            
        kwargs = {"max_pages": 1, "timeout": settings.REQUEST_TIMEOUT_SECONDS}
        if p_key == "serpapi":
            kwargs["api_key"] = api_key
        elif p_key == "dataforseo":
            kwargs["login"] = login
            kwargs["password"] = password
            
        try:
            res = provider.search(req.query, **kwargs)
            results.append(ProviderDebugResult(
                provider=res.provider,
                status=res.status,
                status_code=res.status_code,
                urls=res.urls,
                debug=res.debug
            ))
        except Exception as e:
            results.append(ProviderDebugResult(
                provider=provider.name(),
                status="error",
                status_code=None,
                urls=[],
                debug={"error": str(e)}
            ))
            
    return DebugSearchResponse(query=req.query, results=results)

