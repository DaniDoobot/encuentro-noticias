import uuid
import json
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

def execute_indexing_job(job_id: str, limit_domains: Optional[int], force_refresh: bool, sheet_id: str) -> Dict[str, Any]:
    # Set indexing active flag on SheetsService
    sheets_service.is_indexing_active = True
    try:
        # Invalidate caches to read fresh values
        sheets_service.invalidate_sources_cache(sheet_id)
        if sheet_id in sheets_service._config_cache:
            del sheets_service._config_cache[sheet_id]
            
        config = sheets_service.get_config_dict(sheet_id)
        
        # Invalidate again after fetching config just in case, and read active sources
        sheets_service.invalidate_sources_cache(sheet_id)
        
        # Read Fuentes raw to calculate sources_total
        client = sheets_service.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Fuentes")
        records = worksheet.get_all_records()
        
        sources_total = 0
        for r in records:
            if str(r.get("Dominio", "")).strip():
                sources_total += 1
                
        # Now get active and index-enabled sources
        sources = sheets_service.get_active_sources(sheet_id)
        sources_selected = len(sources)
        
        req_limit = limit_domains
        config_limit_val = config.get("INDEX_MAX_SOURCES_PER_RUN", 0)
        try:
            config_limit = int(config_limit_val) if config_limit_val not in (None, "") else 0
        except Exception:
            config_limit = 0
            
        if req_limit is not None:
            effective_limit = req_limit
        else:
            effective_limit = config_limit

        if effective_limit > 0:
            sources = sources[:effective_limit]
        else:
            effective_limit = 0
            
        domains_total = len(sources)
        
        logger_service.log(
            level="INFO",
            action="INDEX_LIMIT_RESOLVED",
            message=f"Resolución de límite de fuentes: req={req_limit}, config={config_limit}, efectivo={effective_limit}",
            detail=json.dumps({
                "req_limit_domains": req_limit,
                "config_INDEX_MAX_SOURCES_PER_RUN": config_limit,
                "effective_limit": effective_limit,
                "sources_total": sources_total,
                "sources_selected": sources_selected,
                "domains_total": domains_total,
                "domains_to_index": [s["domain"] for s in sources]
            }),
            sheet_id=sheet_id,
            run_id=job_id
        )
        
        update_job_status(
            job_id,
            sources_total=sources_total,
            sources_selected=sources_selected,
            domains_total=domains_total,
            max_sources_per_run=effective_limit
        )

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
            
            # Immediately update status in Google Sheets in real-time
            domain = stats.get("domain")
            if domain:
                skipped = stats.get("skipped", False)
                if not skipped:
                    errs_list = stats.get("errors", [])
                    row_index = stats.get("row_index")
                    urls_found = stats.get("urls_found", 0)
                    urls_stored = stats.get("urls_stored", 0)
                    urls_enriched = stats.get("urls_enriched", 0)
                    last_activity = stats.get("last_activity") or datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    
                    if urls_stored == 0:
                        status_str = "completado_sin_urls"
                    else:
                        status_str = "completado" if len(errs_list) == 0 else "error_parcial"
                    
                    sheets_service.update_source_index_status(
                        sheet_id=sheet_id,
                        domain=domain,
                        last_indexed=last_activity,
                        urls_indexed=urls_stored,
                        errors=errs_list,
                        row_index=row_index,
                        job_id=job_id,
                        status_str=status_str,
                        progreso="100%",
                        urls_found=urls_found,
                        urls_stored=urls_stored,
                        urls_enriched=urls_enriched,
                        last_activity=last_activity
                    )

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
            urls_stored = res.get("urls_stored", 0)
            
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
                "urls_indexed": urls_stored,
                "status": status_str,
                "errors": errs_list
            })

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
    finally:
        # Clear indexing active flag
        sheets_service.is_indexing_active = False

@router.post("/sources/index")
def post_sources_index(req: IndexSourcesRequest):
    """
    Launches or runs a domain index task for active sources in Google Sheets.
    """
    # 1. Protect against double indexation
    with job_registry_lock:
        is_running = any(job.get("status") == "running" for job in job_registry.values())
    if is_running or sheets_service.is_indexing_active:
        running_job = None
        with job_registry_lock:
            for job in job_registry.values():
                if job.get("status") == "running":
                    running_job = job
                    break
        msg = "Ya hay una tarea de indexación en ejecución."
        if running_job:
            msg += f" Job ID activo: {running_job['job_id']}"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=msg
        )

    job_id = f"idx_{uuid.uuid4().hex[:8]}"
    limit = req.limit_domains  # Can be None!
    force = req.force_refresh if req.force_refresh is not None else False
    
    # Check if google credentials are valid
    try:
        sheets_service.get_client()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Google Sheets service is not configured or available: {str(e)}"
        )

    # Invalidate cache of sources and config before selecting sources
    sheets_service.invalidate_sources_cache(settings.GOOGLE_SHEET_ID)
    if settings.GOOGLE_SHEET_ID in sheets_service._config_cache:
        del sheets_service._config_cache[settings.GOOGLE_SHEET_ID]

    # Initialize job in registry
    with job_registry_lock:
        job_registry[job_id] = {
            "job_id": job_id,
            "status": "running",
            "started_at": datetime.datetime.utcnow().isoformat(),
            "finished_at": None,
            "domains_total": limit or 0,
            "domains_completed": 0,
            "current_domain": "",
            "urls_found": 0,
            "urls_stored": 0,
            "urls_enriched": 0,
            "errors": []
        }
        
    thread = threading.Thread(
        target=run_indexing_background,
        args=(job_id, limit, force, settings.GOOGLE_SHEET_ID)
    )
    thread.daemon = True
    thread.start()
    
    return {
        "job_id": job_id,
        "status": "running",
        "message": "Indexación iniciada en segundo plano"
    }


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


@router.get("/sources/index/preview")
def get_sources_index_preview(limit_domains: Optional[int] = None):
    """
    Returns a preview of the domains that would be indexed under current config and requests.
    Does not run any execution, write status or modify logs.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    
    # Invalidate cache of sources and config to get fresh values
    sheets_service.invalidate_sources_cache(sheet_id)
    if sheet_id in sheets_service._config_cache:
        del sheets_service._config_cache[sheet_id]
        
    config = sheets_service.get_config_dict(sheet_id)
    
    # Read Fuentes raw to calculate sources_total
    client = sheets_service.get_client()
    spreadsheet = client.open_by_key(sheet_id)
    worksheet = spreadsheet.worksheet("Fuentes")
    records = worksheet.get_all_records()
    
    sources_total = 0
    for r in records:
        if str(r.get("Dominio", "")).strip():
            sources_total += 1
            
    # Now get active and index-enabled sources
    sources = sheets_service.get_active_sources(sheet_id)
    sources_selected = len(sources)
    
    config_limit_val = config.get("INDEX_MAX_SOURCES_PER_RUN", 0)
    try:
        config_limit = int(config_limit_val) if config_limit_val not in (None, "") else 0
    except Exception:
        config_limit = 0
        
    if limit_domains is not None:
        effective_limit = limit_domains
    else:
        effective_limit = config_limit
        
    # Apply limit
    if effective_limit > 0:
        sources_to_index = sources[:effective_limit]
    else:
        sources_to_index = sources
        effective_limit = 0
        
    domains_to_index = [s["domain"] for s in sources_to_index]
    
    return {
        "sources_total": sources_total,
        "sources_selected": sources_selected,
        "config_INDEX_MAX_SOURCES_PER_RUN": config_limit,
        "effective_limit": effective_limit,
        "domains_to_index": domains_to_index
    }


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
            
        active_sources = []
        try:
            active_sources = sheets_service.get_active_sources(settings.GOOGLE_SHEET_ID)
        except Exception:
            pass
        source_by_domain = {s["domain"]: s for s in active_sources}

        domains = req.domains
        if not domains:
            domains = [s["domain"] for s in active_sources if s.get("active", True) and s.get("domain")]
                
        results = []
        for domain in domains:
            try:
                items = internal_search_provider.search_domain_for_book(
                    domain=domain,
                    title=req.title,
                    author=req.author or "",
                    isbn=req.isbn or "",
                    config=config,
                    source_info=source_by_domain.get(domain)
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

@router.post("/sources/defaults/append")
def post_sources_defaults_append():
    """
    Appends default sources (cultural, religious, press) to the sheet, avoiding duplicates.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    try:
        count = sheets_service.append_default_sources(sheet_id)
        
        # Log this action
        logger_service.log(
            level="INFO",
            action="SOURCES_DEFAULTS_APPEND",
            message=f"Se añadieron {count} nuevas fuentes por defecto.",
            sheet_id=sheet_id,
            detail=json.dumps({"appended_count": count})
        )
        logger_service.flush_log_batch(sheet_id)
        
        return {
            "success": True,
            "appended_count": count,
            "message": f"Se añadieron {count} nuevas fuentes por defecto."
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to append default sources: {str(e)}"
        )

@router.post("/sources/sync-status")
def post_sources_sync_status():
    """
    Synchronizes the SQLite cache stats with the Fuentes sheet in Google Sheets.
    """
    sheet_id = settings.GOOGLE_SHEET_ID
    try:
        # Log SOURCE_SHEET_UPDATE_STARTED
        logger_service.log(
            level="INFO",
            action="SOURCE_SHEET_UPDATE_STARTED",
            message="Iniciando sincronización masiva de la pestaña Fuentes",
            sheet_id=sheet_id
        )
        
        cache_service.init_db(settings.DOMAIN_INDEX_DB_PATH)
        result = sheets_service.sync_sources_status(sheet_id)
        
        # Log SOURCE_SHEET_UPDATE_COMPLETED
        logger_service.log(
            level="INFO",
            action="SOURCE_SHEET_UPDATE_COMPLETED",
            message=f"Sincronización masiva de Fuentes completada. Fuentes actualizadas: {result['sources_updated']}",
            sheet_id=sheet_id,
            detail=f"Total URLs en cache: {result['total_urls']}"
        )
        logger_service.flush_log_batch(sheet_id)
        
        return result
    except Exception as e:
        # Log SOURCE_SHEET_UPDATE_FAILED
        logger_service.log(
            level="ERROR",
            action="SOURCE_SHEET_UPDATE_FAILED",
            message=f"Error en sincronización masiva de Fuentes: {str(e)}",
            sheet_id=sheet_id
        )
        logger_service.flush_log_batch(sheet_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sincronización fallida: {str(e)}"
        )

