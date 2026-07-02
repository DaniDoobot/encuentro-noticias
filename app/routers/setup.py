from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import Optional
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



@router.get("/debug/google-news")
def debug_google_news(q: str):
    """
    Directly runs Google News RSS provider for a query.
    Useful to verify what Google News returns compared to browser results.
    """
    try:
        from app.services.search_providers import GoogleNewsRssSearchProvider
        provider = GoogleNewsRssSearchProvider()
        res = provider.search(query=q)
        
        results = []
        if res.status == "ok":
            parsed = res.debug.get("organic_results_parsed", [])
            for item in parsed:
                # Try to extract the publication name (source) from the title suffix
                title_str = item.get("title", "")
                source_name = "GoogleNewsRss"
                if " - " in title_str:
                    parts = title_str.rsplit(" - ", 1)
                    if len(parts) == 2:
                        source_name = parts[1].strip()
                
                results.append({
                    "title": title_str,
                    "url": item.get("url", ""),
                    "source": source_name
                })
        return {
            "query": q,
            "parsed_results_count": len(results),
            "results": results
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Google News debug query failed: {str(e)}"
        )


@router.get("/setup/inspect-panel-prueba")
def inspect_panel_prueba():
    """
    Temporary endpoint to inspect 'Panel prueba' structure.
    """
    try:
        client = sheets_service.get_client()
        sheet_id = settings.GOOGLE_SHEET_ID
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Panel prueba")
        rows = worksheet.get_all_values()
        return {
            "success": True,
            "rows": rows[:40]
        }
    except Exception as e:
        import traceback
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }


@router.post("/setup/sheet-size-report")
def get_sheet_size_report(sheet_id: str = None):
    """
    Returns a report detailing grid sizes and estimated excess cells for all tabs.
    """
    s_id = sheet_id or settings.GOOGLE_SHEET_ID
    try:
        return sheets_service.get_sheet_size_report(s_id)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate sheet size report: {str(e)}"
        )


@router.post("/setup/compact-sheet")
async def compact_sheet(request: Request, sheet_id: Optional[str] = None, dry_run: Optional[bool] = None):
    """
    Compacts worksheets by shrinking trailing empty rows/cols to recommended sizes.
    Accepts sheet_id and dry_run from query parameters or JSON body.
    """
    body_dry_run = None
    body_sheet_id = None
    try:
        body_data = await request.json()
        if body_data:
            body_dry_run = body_data.get("dry_run")
            body_sheet_id = body_data.get("sheet_id")
    except Exception:
        pass

    final_dry_run = dry_run if dry_run is not None else (body_dry_run if body_dry_run is not None else False)
    final_sheet_id = sheet_id or body_sheet_id or settings.GOOGLE_SHEET_ID

    try:
        return sheets_service.compact_sheet(final_sheet_id, dry_run=final_dry_run)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to compact sheet: {str(e)}"
        )


@router.post("/setup/test-log")
def test_log(sheet_id: str = None):
    """
    Test endpoint that writes a single log entry into the Logs sheet.
    """
    s_id = sheet_id or settings.GOOGLE_SHEET_ID
    from app.services.sheets_service import get_now_madrid_str
    now = get_now_madrid_str()
    log_row = [now, "INFO", "LOG_WRITE_TEST", "", "Prueba de escritura de logs", "{}", "manual-test"]
    res = sheets_service.add_log_batch(s_id, [log_row])
    
    # Parse next_row from range (e.g., "A2:G2" -> next_row = 2)
    next_row = 2
    if res.get("range"):
        import re
        m = re.search(r'\d+', res["range"])
        if m:
            next_row = int(m.group(0))
            
    return {
        "success": res.get("success", False),
        "range": res.get("range", ""),
        "next_row": next_row
    }
