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
    date_min: Optional[str] = Field(default=None, description="ISO Date YYYY-MM-DD")
    date_max: Optional[str] = Field(default=None, description="ISO Date YYYY-MM-DD")
    include_unknown_dates: Optional[bool] = Field(default=None, description="If true, includes articles without a detected date")

class BookRunConfig(BaseModel):
    dry_run: Optional[bool] = Field(default=False, description="If true, does search/extraction but doesn't modify google sheets reviews/descartes or change book status to completed/error")
    date_min: Optional[str] = Field(default=None, description="ISO Date YYYY-MM-DD")
    date_max: Optional[str] = Field(default=None, description="ISO Date YYYY-MM-DD")
    include_unknown_dates: Optional[bool] = Field(default=None, description="If true, includes articles without a detected date")

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

class DebugSearchRequest(BaseModel):
    query: str
    providers: List[str] = Field(default=["DuckDuckGo", "BingHtml", "GoogleNewsRss"])

class ProviderDebugResult(BaseModel):
    provider: str
    status: str
    status_code: Optional[int] = None
    urls: List[str]
    debug: Dict[str, Any] = {}

class DebugSearchResponse(BaseModel):
    query: str
    results: List[ProviderDebugResult]


class IndexSourcesRequest(BaseModel):
    limit_domains: Optional[int] = Field(default=10, description="Max domains to index in this batch")
    force_refresh: Optional[bool] = Field(default=False, description="If true, bypasses refresh_days check")

class IndexSourcesResponse(BaseModel):
    job_id: str
    domains_queued: int
    message: str

class DomainStats(BaseModel):
    domain: str
    urls: int
    last_indexed: Optional[str] = None
    errors: int = 0
    last_discovery_method: Optional[str] = "none"
    last_error: Optional[str] = ""


class SourcesStatusResponse(BaseModel):
    total_urls: int
    domains: List[DomainStats]

class DomainSearchRequest(BaseModel):
    title: str
    author: Optional[str] = ""
    isbn: Optional[str] = ""

class DomainSearchMatch(BaseModel):
    url: str
    domain: str
    title: str
    score: int
    matched_fields: List[str]
    snippet: Optional[str] = ""
    pub_date: Optional[str] = ""

class DomainSearchResponse(BaseModel):
    total_matches: int
    matches: List[DomainSearchMatch]


class DebugInternalSearchRequest(BaseModel):
    title: str
    author: Optional[str] = ""
    isbn: Optional[str] = ""
    domains: List[str] = Field(default=[])


class DebugInternalSearchResult(BaseModel):
    domain: str
    provider: str
    query: str
    url: str
    title: str
    snippet: str
    status: str
    error: Optional[str] = ""


class DebugInternalSearchResponse(BaseModel):
    queries: List[str]
    results: List[DebugInternalSearchResult]



