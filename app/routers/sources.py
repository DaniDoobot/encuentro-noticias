import uuid
import threading
import logging
import datetime
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas import (
    IndexSourcesRequest, IndexSourcesResponse,
    SourcesStatusResponse, DomainStats,
    DomainSearchRequest, DomainSearchMatch, DomainSearchResponse,
    DebugInternalSearchRequest, DebugInternalSearchResult, DebugInternalSearchResponse
)
from app.config import settings
from app.dependencies import verify_admin_token
from app.services.sheets_service import sheets_service
from app.services.domain_indexer import domain_indexer
from app.services.cache_service import cache_service
from app.services.logger_service import logger_service

logger = logging.getLogger("encuentro-noticias")

router = APIRouter(dependencies=[Depends(verify_admin_token)])

job_registry: Dict[str, Dict[str, Any]] = {}
job_registry_lock = threading.Lock()


def update_job_status(job_id: str, **kwargs):
    """Update job metrics in the thread-safe global registry."""
    with job_registry_lock:
        if job_id in job_registry:
            job_registry[job_id].update(kwargs)


def run_indexing_background(job_id: str, limit_domains: int, force_refresh: bool, sheet_id: str):
    execute_indexing_job(job_id, limit_domains, force_refresh, sheet_id)

def execute_indexing_job(job_id: str, limit_domains: int, force_refresh: bool, sheet_id: str) -> Dict[str, Any]:
    try:
        config = sheets_service.get_config_dict(sheet_id)
        sources = sheets_service.get_active_sources(sheet_id)
        
        # Limit the number of domains if requested
        if limit_domains > 0:
            sources = sources[:limit_domains]
            
        update_job_status(job_id, domains_total=len(sources))

        def log_fn(msg: str):
            logger_service.log(
                level="INFO",
                action="DOMAIN_INDEX",
                message=msg,
                sheet_id=sheet_id,
                run_id=job_id
            )
            
        logger_service.log(
            level="INFO",
            action="DOMAIN_INDEX_START",
            message=f"Iniciando indexación de {len(sources)} fuentes",
            sheet_id=sheet_id,
            run_id=job_id
        )

        def on_domain_start(domain: str):
            update_job_status(job_id, current_domain=domain)

        def on_domain_complete(stats: Dict[str, Any]):
            with job_registry_lock:
                if job_id in job_registry:
                    job = job_registry[job_id]
                    job["domains_completed"] += 1
                    if stats.get("errors"):
                        job["errors"].extend(stats["errors"])

        def on_progress(found_inc: int, stored_inc: int, enriched_inc: int):
            with job_registry_lock:
                if job_id in job_registry:
                    job = job_registry[job_id]
                    job["urls_found"] += found_inc
                    job["urls_stored"] += stored_inc
                    job["urls_enriched"] += enriched_inc
        
        results = domain_indexer.index_all(
            sources=sources,
            config=config,
            force_refresh=force_refresh,
            log_fn=log_fn,
            run_id=job_id,
            sheet_id=sheet_id,
            on_domain_start=on_domain_start,
            on_domain_complete=on_domain_complete,
            on_progress=on_progress
        )
        
        # Map results to diagnostic format
        mapped_results = []
        domains_indexed = 0
        domains_failed = 0
        domains_skipped = 0
        domains_processed = 0

        for res in results:
            domain = res.get("domain")
            if not domain:
                continue
                
            skipped = res.get("skipped", False)
            errs_list = res.get("errors", [])
            
            # Get stats from DB to know the total URLs
            try:
                db_stats = cache_service.get_domain_stats(domain)
                urls_count = db_stats.get("urls", 0)
            except Exception:
                urls_count = res.get("urls_found", 0)
            
            if skipped:
                domains_skipped += 1
                status_str = "skipped"
            elif errs_list:
                domains_failed += 1
                domains_processed += 1
                status_str = "error"
            else:
                domains_indexed += 1
                domains_processed += 1
                status_str = "ok"

            mapped_results.append({
                "domain": domain,
                "processed": not skipped,
                "urls_indexed": urls_count,
                "status": status_str,
                "errors": errs_list
            })

            # Update Sheets if not skipped
            if not skipped:
                errs_str = ", ".join(errs_list) if errs_list else ""
                last_idx = res.get("last_indexed", datetime.datetime.utcnow().isoformat())
                
                sheets_service.update_source_stats(
                    sheet_id=sheet_id,
                    domain=domain,
                    last_indexed=last_idx,
                    urls_indexed=urls_count,
                    errors=errs_str
                )

        logger_service.log(
            level="INFO",
            action="DOMAIN_INDEX_COMPLETED",
            message=f"Indexación de dominios completada para {len(results)} dominios.",
            sheet_id=sheet_id,
            run_id=job_id
        )
        logger_service.flush_log_batch(sheet_id, job_id)
        
        final_state = {
            "success": True,
            "status": "completed",
            "finished_at": datetime.datetime.utcnow().isoformat(),
            "domains_total": len(sources),
            "domains_processed": domains_processed,
            "domains_indexed": domains_indexed,
            "domains_failed": domains_failed,
            "domains_skipped": domains_skipped,
            "results": mapped_results
        }
        
        with job_registry_lock:
            if job_id in job_registry:
                job_registry[job_id].update(final_state)
                
        return final_state
        
    except Exception as e:
        logger.error(f"Error in indexing task: {e}")
        logger_service.log(
            level="ERROR",
            action="DOMAIN_INDEX_FAILED",
            message=f"Fallo en indexación de dominios: {str(e)}",
            sheet_id=sheet_id,
            run_id=job_id
        )
        logger_service.flush_log_batch(sheet_id, job_id)
        
        err_state = {
            "success": False,
            "status": "failed",
            "finished_at": datetime.datetime.utcnow().isoformat(),
            "domains_total": 0,
            "domains_processed": 0,
            "domains_indexed": 0,
            "domains_failed": 0,
            "domains_skipped": 0,
            "results": [],
            "errors": [str(e)]
        }
        with job_registry_lock:
            if job_id in job_registry:
                job_registry[job_id].update(err_state)
        return err_state

@router.post("/sources/index")
def post_sources_index(req: IndexSourcesRequest):
    """
    Launches or runs a domain index task for active sources in Google Sheets.
    """
    job_id = f"idx_{uuid.uuid4().hex[:8]}"
    limit = req.limit_domains if req.limit_domains is not None else 10
    force = req.force_refresh if req.force_refresh is not None else False
    background = req.background if req.background is not None else False
    
    # Check if google credentials are valid
    try:
        sheets_service.get_client()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Google Sheets service is not configured or available: {str(e)}"
        )

    # Initialize job in registry
    with job_registry_lock:
        job_registry[job_id] = {
            "job_id": job_id,
            "status": "running",
            "started_at": datetime.datetime.utcnow().isoformat(),
            "finished_at": None,
            "domains_total": limit,
            "domains_completed": 0,
            "current_domain": "",
            "urls_found": 0,
            "urls_stored": 0,
            "urls_enriched": 0,
            "errors": []
        }
        
    if background:
        thread = threading.Thread(
            target=run_indexing_background,
            args=(job_id, limit, force, settings.GOOGLE_SHEET_ID)
        )
        thread.daemon = True
        thread.start()
        
        return {
            "success": True,
            "job_id": job_id,
            "status": "running",
            "message": f"Indexación iniciada en segundo plano con ID {job_id}."
        }
    else:
        # Run synchronously
        res = execute_indexing_job(job_id, limit, force, settings.GOOGLE_SHEET_ID)
        return res


@router.get("/sources/index/status")
def get_sources_index_status(job_id: Optional[str] = None):
    """
    Returns the real-time status of a background indexing job.
    If no job_id is provided, returns the latest job status.
    """
    with job_registry_lock:
        if not job_registry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No indexing jobs have been run yet."
            )
        if job_id:
            if job_id not in job_registry:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Indexing job '{job_id}' not found."
                )
            return job_registry[job_id]
        # Return latest job by started_at descending
        latest_job = max(job_registry.values(), key=lambda j: j["started_at"])
        return latest_job


@router.get("/sources/status", response_model=SourcesStatusResponse)
def get_sources_status():
    """
    Returns statistics about the SQLite cache and active domains.
    """
    try:
        cache_service.init_db(settings.DOMAIN_INDEX_DB_PATH)
        total_urls = cache_service.get_total_urls()
        db_stats = cache_service.get_all_domains_stats()
        db_stats_map = {d["domain"]: d for d in db_stats}
        
        # Get statuses from domain_status table
        db_statuses = cache_service.get_all_domain_statuses()
        db_statuses_map = {s["domain"]: s for s in db_statuses}
        
        # Get active sources from Google Sheets to ensure they are listed
        active_sources = sheets_service.get_active_sources(settings.GOOGLE_SHEET_ID)
        
        domains_list = []
        seen_domains = set()
        
        def make_stats(dom: str, fallback_cnt: int, fallback_idx: Optional[str]) -> DomainStats:
            status = db_statuses_map.get(dom, {})
            stats_grp = db_stats_map.get(dom, {})
            
            urls = status.get("urls_count")
            if urls is None:
                urls = stats_grp.get("cnt", fallback_cnt)
                
            last_indexed = status.get("last_indexed")
            if not last_indexed:
                last_indexed = stats_grp.get("last_indexed", fallback_idx)
                
            errors = status.get("errors_count", 0)
            last_discovery_method = status.get("last_discovery_method", "none")
            last_error = status.get("last_error", "")
            
            return DomainStats(
                domain=dom,
                urls=urls,
                last_indexed=last_indexed,
                errors=errors,
                last_discovery_method=last_discovery_method,
                last_error=last_error
            )
        
        # Add active sources first
        for src in active_sources:
            domain = src["domain"]
            seen_domains.add(domain)
            domains_list.append(make_stats(domain, 0, None))
            
        # Add any other domains present in DB but not active in sheets
        all_db_domains = set(db_stats_map.keys()).union(set(db_statuses_map.keys()))
        for domain in all_db_domains:
            if domain not in seen_domains:
                domains_list.append(make_stats(domain, 0, None))
                
        return SourcesStatusResponse(total_urls=total_urls, domains=domains_list)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get sources status: {str(e)}"
        )


@router.post("/debug/domain-search", response_model=DomainSearchResponse)
def debug_domain_search(req: DomainSearchRequest):
    """
    Manual testing of local domain index matching. Does not write to Google Sheets.
    """
    try:
        cache_service.init_db(settings.DOMAIN_INDEX_DB_PATH)
        try:
            config = sheets_service.get_config_dict(settings.GOOGLE_SHEET_ID)
        except Exception:
            config = {}
            
        from app.services.source_discovery import source_discovery
        candidates = source_discovery.find_candidates(
            title=req.title,
            author=req.author or "",
            isbn=req.isbn or "",
            config=config
        )
        
        matches = []
        for c in candidates:
            matches.append(DomainSearchMatch(
                url=c["url"],
                domain=c["domain"],
                title=c["title"],
                score=c["score"],
                matched_fields=c["matched_fields"],
                snippet=c.get("snippet", ""),
                pub_date=c.get("pub_date", "")
            ))
            
        return DomainSearchResponse(
            total_matches=len(matches),
            matches=matches
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Domain search failed: {str(e)}"
        )


@router.post("/debug/internal-domain-search", response_model=DebugInternalSearchResponse)
def debug_internal_domain_search(req: DebugInternalSearchRequest):
    """
    Manual testing of wordpress api and html search on target domains.
    """
    try:
        from app.services.internal_search_provider import internal_search_provider, generate_internal_queries
        
        queries = generate_internal_queries(req.title, req.author or "", req.isbn or "")
        
        try:
            config = sheets_service.get_config_dict(settings.GOOGLE_SHEET_ID)
        except Exception:
            config = {}
            
        domains = req.domains
        if not domains:
            try:
                active_sources = sheets_service.get_active_sources(settings.GOOGLE_SHEET_ID)
                domains = [s["domain"] for s in active_sources if s.get("active", True) and s.get("domain")]
            except Exception:
                domains = []
                
        results = []
        for domain in domains:
            try:
                items = internal_search_provider.search_domain_for_book(
                    domain=domain,
                    title=req.title,
                    author=req.author or "",
                    isbn=req.isbn or "",
                    config=config
                )
                for item in items:
                    results.append(DebugInternalSearchResult(
                        domain=domain,
                        provider=item.get("provider", "unknown"),
                        query=item.get("query", ""),
                        url=item.get("url", ""),
                        title=item.get("title", ""),
                        snippet=item.get("snippet", ""),
                        status=item.get("status", "ok"),
                        error=item.get("error", "")
                    ))
            except Exception as e:
                results.append(DebugInternalSearchResult(
                    domain=domain,
                    provider="error",
                    query="",
                    url="",
                    title="",
                    snippet="",
                    status="error",
                    error=str(e)
                ))
                
        return DebugInternalSearchResponse(
            queries=queries,
            results=results
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Debug internal search failed: {str(e)}"
        )

