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
