import gspread
from google.oauth2.service_account import Credentials
from app.config import settings
from typing import List, Dict, Any, Optional, Tuple
import datetime
import logging
import pytz

def get_now_madrid() -> datetime.datetime:
    madrid_tz = pytz.timezone("Europe/Madrid")
    return datetime.datetime.now(madrid_tz)

def get_now_madrid_str() -> str:
    return get_now_madrid().strftime("%Y-%m-%d %H:%M:%S")

logger = logging.getLogger("encuentro-noticias")

def is_row_real(row: dict) -> bool:
    # Check if any of the target fields has non-empty text
    target_fields = [
        "URL", "URL normalizada", "Título del artículo", "Título del libro", 
        "Autor del libro", "ISBN", "Resumen", "Hash deduplicación", 
        "Título para Web", "Autor para Web", "Título del libro detectado por IA", 
        "Autor del libro detectado por IA", "WordPress ID", "WordPress URL", 
        "Fecha publicación", "Fecha de publicación"
    ]
    for field in target_fields:
        if str(row.get(field, "")).strip():
            return True
    return False

def col_num_to_letter(n: int) -> str:
    string = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        string = chr(65 + remainder) + string
    return string


def clean_domain_string(domain: str) -> str:
    s = str(domain).strip().lower()
    if s.startswith("http://"):
        s = s[7:]
    elif s.startswith("https://"):
        s = s[8:]
    if "/" in s:
        s = s.split("/")[0]
    return s.strip()

def normalize_config_val(val: Any) -> str:
    if val is None:
        return ""
    # Convert to string and strip
    s_val = str(val).strip()
    # Remove leading single quote if present
    if s_val.startswith("'"):
        s_val = s_val[1:]
    return s_val.strip()

def parse_bool(val: Any, default: bool) -> bool:
    s_val = normalize_config_val(val).lower()
    if not s_val:
        return default
    if s_val in ("true", "1", "yes", "sí", "si"):
        return True
    if s_val in ("false", "0", "no"):
        return False
    return default

def parse_int(val: Any, default: int) -> int:
    s_val = normalize_config_val(val)
    if not s_val:
        return default
    try:
        return int(s_val)
    except ValueError:
        return default

def parse_float(val: Any, default: float) -> float:
    s_val = normalize_config_val(val)
    if not s_val:
        return default
    try:
        return float(s_val)
    except ValueError:
        return default

def parse_str(val: Any, default: str) -> str:
    s_val = normalize_config_val(val)
    return s_val if s_val else default

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

class SheetsService:
    def __init__(self):
        self._client = None

    def get_client(self) -> gspread.Client:
        if self._client is None:
            creds_dict = settings.get_google_credentials()
            if not creds_dict:
                raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 is not configured in environment variables.")
            credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
            self._client = gspread.authorize(credentials)
        return self._client

    def ensure_sheet(self, sheet_id: str) -> Dict[str, Any]:
        """
        Prepares the sheet: creates tabs and writes headers if missing.
        Does not delete existing data.
        """
        client = self.get_client()
        try:
            spreadsheet = client.open_by_key(sheet_id)
        except gspread.exceptions.SpreadsheetNotFound:
            raise ValueError(f"Google Sheet with ID {sheet_id} was not found or is not shared with the service account.")

        # Rename old "Reseñas" worksheet to "Reseñas por publicar" if it exists
        try:
            old_ws = spreadsheet.worksheet("Reseñas")
            try:
                spreadsheet.worksheet("Reseñas por publicar")
            except gspread.exceptions.WorksheetNotFound:
                old_ws.update_title("Reseñas por publicar")
        except gspread.exceptions.WorksheetNotFound:
            pass

        # Tab schemas definition
        tabs = {
            "Libros": [
                "¿Incluir en búsqueda?", "ISBN", "Título del libro", "Autor del libro", 
                "Estado", "Última ejecución", "Reseñas encontradas", "Observaciones"
            ],
            "Reseñas por publicar": [
                "¿Publicar?", "Estado publicación", "Fecha intento publicación", "Error publicación", "ISBN", "Título del libro",
                "Autor del libro", "URL", "Título para Web", "Autor para Web",
                "Medio de publicación", "Fecha de publicación",
                "Idioma original", "Categoría", "Resumen", "Score de coincidencia",
                "Tipo de contenido", "Fecha de extracción", "Estado", "URL normalizada", "Hash deduplicación", "Query"
            ],
            "Reseñas publicadas": [
                "Fecha publicación", "WordPress ID", "WordPress URL", "ISBN", "Título del libro",
                "Autor del libro", "URL", "Título para Web", "Autor para Web",
                "Medio de publicación", "Fecha de publicación",
                "Idioma original", "Categoría", "Resumen", "Score de coincidencia",
                "Tipo de contenido", "Fecha de extracción", "Estado", "URL normalizada", "Hash deduplicación", "Query"
            ],
            "Descartes": [
                "ISBN", "Título del libro", "Autor del libro", "Query", "URL", 
                "Título detectado", "Motivo de descarte", "Score de coincidencia", "Fecha de extracción"
            ],
            "Fuentes": [
                "Dominio", "Activo", "Tipo", "Sitemap URL", "RSS URL",
                "Buscador interno", "Notas", "Última indexación", "URLs indexadas", "Errores"
            ],
            "Logs": [
                "Run ID", "Fecha", "Nivel", "ISBN", "Acción", "Mensaje", "Detalle"
            ],
            "Config": [
                "Clave", "Valor", "Descripción"
            ],
            "Config técnica": [
                "Clave", "Valor", "Descripción"
            ]
        }

        created_tabs = []
        existing_sheets = {ws.title: ws for ws in spreadsheet.worksheets()}
        for tab_name, headers in tabs.items():
            if tab_name in existing_sheets:
                worksheet = existing_sheets[tab_name]
            else:
                worksheet = spreadsheet.add_worksheet(title=tab_name, rows="1000", cols=str(len(headers) + 5))
                created_tabs.append(tab_name)
                worksheet.insert_row(headers, index=1)
                continue

            # Ensure headers are correct and run safe migrator if order/columns mismatch
            if tab_name == "Libros":
                try:
                    existing_headers = worksheet.row_values(1)
                    if existing_headers and "¿Incluir en búsqueda?" not in existing_headers:
                        logger.info("Migrating 'Libros' to add '¿Incluir en búsqueda?' checkbox column...")
                        all_rows = worksheet.get_all_values()
                        data_rows = all_rows[1:] if len(all_rows) > 1 else []
                        
                        new_rows = []
                        for row in data_rows:
                            row_dict = {}
                            for i, val in enumerate(row):
                                if i < len(existing_headers):
                                    row_dict[existing_headers[i]] = val
                            
                            new_row = []
                            for h in headers:
                                if h == "¿Incluir en búsqueda?":
                                    title_val = row_dict.get("Título del libro", "").strip()
                                    new_row.append(True if title_val else "")
                                else:
                                    new_row.append(row_dict.get(h, ""))
                            new_rows.append(new_row)
                            
                        worksheet.clear()
                        worksheet.resize(rows=max(1000, len(new_rows) + 50), cols=len(headers) + 5)
                        worksheet.update("A1", [headers] + new_rows)
                        logger.info("Worksheet 'Libros' successfully migrated.")
                except Exception as e_libros:
                    logger.error(f"Could not migrate 'Libros': {e_libros}")

            elif tab_name in ("Reseñas por publicar", "Reseñas publicadas"):
                try:
                    existing_headers = worksheet.row_values(1)
                    if existing_headers and existing_headers != headers:
                        logger.info(f"Reorganizing columns for '{tab_name}' safely...")
                        all_rows = worksheet.get_all_values()
                        data_rows = all_rows[1:] if len(all_rows) > 1 else []
                        
                        new_rows = []
                        for row in data_rows:
                            row_dict = {}
                            for i, val in enumerate(row):
                                if i < len(existing_headers):
                                    row_dict[existing_headers[i]] = val
                            if not is_row_real(row_dict):
                                continue
                                
                            # Safe copies
                            title_web_val = row_dict.get("Título para Web", "")
                            if not title_web_val or str(title_web_val).strip().lower() in ("titulo web", "título web"):
                                title_web_val = row_dict.get("Título del artículo") or row_dict.get("Título del libro detectado por IA") or ""
                            
                            author_web_val = row_dict.get("Autor para Web", "")
                            if not author_web_val or str(author_web_val).strip().lower() in ("autor web", "autor web "):
                                author_web_val = row_dict.get("Autor de la publicación") or row_dict.get("Autor del libro detectado por IA") or ""
                                
                            new_row = []
                            for h in headers:
                                if h == "Título para Web":
                                    raw_val = title_web_val
                                elif h == "Autor para Web":
                                    raw_val = author_web_val
                                else:
                                    raw_val = row_dict.get(h)
                                    
                                if raw_val is not None:
                                    if isinstance(raw_val, str):
                                        s = raw_val.strip()
                                        if s.lower() in ("titulo web", "título web", "autor web", "autor web "):
                                            raw_val = ""
                                        else:
                                            raw_val = s
                                    new_row.append(raw_val)
                                elif h == "¿Publicar?":
                                    new_row.append(False)
                                else:
                                    new_row.append("")
                            new_rows.append(new_row)
                            
                        # Overwrite sheet with new schema and reordered values
                        worksheet.clear()
                        worksheet.resize(rows=max(1000, len(new_rows) + 50), cols=len(headers) + 5)
                        worksheet.update("A1", [headers] + new_rows)
                        logger.info(f"Worksheet '{tab_name}' successfully migrated with {len(new_rows)} rows.")
                except Exception as e_hdr:
                    logger.error(f"Could not migrate headers/rows for {tab_name}: {e_hdr}")

            elif worksheet.col_count < len(headers):
                try:
                    existing_headers = worksheet.row_values(1)
                    for i, header in enumerate(headers):
                        if i >= len(existing_headers) or existing_headers[i] != header:
                            worksheet.update_cell(1, i + 1, header)
                except Exception as e_hdr:
                    logger.warning(f"Could not verify headers for {tab_name}: {e_hdr}")


        # Ensure Panel worksheet exists and has the correct layout
        if "Panel" in existing_sheets:
            panel_ws = existing_sheets["Panel"]
        else:
            panel_ws = spreadsheet.add_worksheet(title="Panel", rows="30", cols="10")
            created_tabs.append("Panel")

        panel_vals = panel_ws.get_all_values()
        if not panel_vals or len(panel_vals) < 11:
            panel_structure = [
                ["Encuentro Noticias — Panel de control", ""],
                ["", ""],
                ["Fecha mínima", "2024-01-01"],
                ["Fecha máxima", "2026-12-31"],
                ["Máximo de libros", "5"],
                ["Modo prueba", True],
                ["Incluir artículos sin fecha", True],
                ["Estado última búsqueda", "no iniciado"],
                ["Última búsqueda_id", ""],
                ["Última ejecución", ""],
                ["Mensaje", ""],
                ["", ""],
                ["Instrucciones:", ""],
                ["Use el botón 'Lanzar búsqueda' desde el menú 'Encuentro Noticias' para iniciar una búsqueda.", ""],
                ["Use 'Consultar estado' para refrescar el estado de la última búsqueda.", ""]
            ]
            panel_ws.clear()
            panel_ws.update(range_name="A1", values=panel_structure)
            
        # Apply date, number validations and boolean checkboxes always
        # Resolve these worksheets before the try block so they are always in scope
        libros_ws = spreadsheet.worksheet("Libros")
        try:
            ws_to_pub = spreadsheet.worksheet("Reseñas por publicar")
            ws_pub = spreadsheet.worksheet("Reseñas publicadas")
            
            val_requests = [
                # Date validation for B3:B4
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": panel_ws.id,
                            "startRowIndex": 2,
                            "endRowIndex": 4,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2
                        },
                        "rule": {
                            "condition": {
                                "type": "DATE_IS_VALID"
                            },
                            "showCustomUi": True,
                            "strict": True
                        }
                    }
                },
                # Date format (yyyy-mm-dd) for B3:B4
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": panel_ws.id,
                            "startRowIndex": 2,
                            "endRowIndex": 4,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "DATE",
                                    "pattern": "yyyy-mm-dd"
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat"
                    }
                },
                # Checkbox validation for B6:B7
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": panel_ws.id,
                            "startRowIndex": 5,
                            "endRowIndex": 7,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2
                        },
                        "rule": {
                            "condition": {
                                "type": "BOOLEAN"
                            },
                            "showCustomUi": True
                        }
                    }
                },
                # Positive integer validation for B5 (allows invalid/empty)
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": panel_ws.id,
                            "startRowIndex": 4,
                            "endRowIndex": 5,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2
                        },
                        "rule": {
                            "condition": {
                                "type": "NUMBER_GREATER",
                                "values": [{"userEnteredValue": "0"}]
                            },
                            "showCustomUi": True
                        }
                    }
                },
                # Checkbox validation for Reseñas por publicar Column A (index 0)
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws_to_pub.id,
                            "startRowIndex": 1,
                            "endRowIndex": 1000,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1
                        },
                        "rule": {
                            "condition": {
                                "type": "BOOLEAN"
                            },
                            "showCustomUi": True
                        }
                    }
                },
                # Checkbox validation for Libros Column A (index 0)
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": libros_ws.id,
                            "startRowIndex": 1,
                            "endRowIndex": 1000,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1
                        },
                        "rule": {
                            "condition": {
                                "type": "BOOLEAN"
                            },
                            "showCustomUi": True
                        }
                    }
                }
            ]
            spreadsheet.batch_update({"requests": val_requests})
        except Exception as e_val:
            logger.error(f"Error applying Panel validation format: {e_val}")
            # Non-fatal: formatting failures should not abort the entire setup

        # Fill empty checkboxes to TRUE for books with a title
        try:
            existing_headers = libros_ws.row_values(1)
            idx_incluir = existing_headers.index("¿Incluir en búsqueda?") if "¿Incluir en búsqueda?" in existing_headers else -1
            idx_title = existing_headers.index("Título del libro") if "Título del libro" in existing_headers else -1
            if idx_incluir != -1 and idx_title != -1:
                all_rows = libros_ws.get_all_values()
                updates = []
                for r_idx, row in enumerate(all_rows[1:], start=2):
                    title_val = row[idx_title].strip() if idx_title < len(row) else ""
                    incluir_val = row[idx_incluir].strip() if idx_incluir < len(row) else ""
                    if title_val and incluir_val == "":
                        updates.append(gspread.Cell(row=r_idx, col=idx_incluir + 1, value=True))
                if updates:
                    libros_ws.update_cells(updates)
                    logger.info(f"Updated {len(updates)} empty checkboxes to TRUE in 'Libros'.")
        except Exception as e_chk:
            logger.warning(f"Error checking/updating empty checkboxes in 'Libros': {e_chk}")

        # Initialize Config & Config técnica defaults
        config_ws = spreadsheet.worksheet("Config")
        config_rows = config_ws.get_all_records()
        existing_basic = {row["Clave"]: row for row in config_rows if "Clave" in row}

        tech_ws = spreadsheet.worksheet("Config técnica")
        tech_rows = tech_ws.get_all_records()
        existing_tech = {row["Clave"]: row for row in tech_rows if "Clave" in row}
        
        basic_defaults = [
            {"Clave": "MAX_BOOKS_PER_RUN", "Valor": settings.MAX_BOOKS_PER_RUN, "Descripción": "Cantidad máxima de libros a procesar por ejecución"},
            {"Clave": "MAX_CANDIDATES_PER_BOOK", "Valor": settings.MAX_CANDIDATES_PER_BOOK, "Descripción": "Cantidad máxima de URLs candidatas a evaluar por libro"},
            {"Clave": "MIN_MATCH_SCORE", "Valor": 1, "Descripción": "Score mínimo de validación de OpenAI para aceptar una reseña (0-100)"},
            {"Clave": "OPENAI_MODEL", "Valor": settings.OPENAI_MODEL, "Descripción": "Modelo de OpenAI a usar para análisis"},
            {"Clave": "WORDPRESS_POST_STATUS", "Valor": settings.WORDPRESS_POST_STATUS, "Descripción": "Estado por defecto para posts creados (draft, publish)"},
            {"Clave": "LOG_MAX_ROWS", "Valor": settings.LOG_MAX_ROWS, "Descripción": "Cantidad máxima de filas a mantener en la pestaña Logs"},
            {"Clave": "DESCARTES_MAX_ROWS", "Valor": getattr(settings, "DESCARTES_MAX_ROWS", 1000), "Descripción": "Cantidad máxima de descartes a mantener en la pestaña Descartes"}
        ]

        technical_defaults = [
            {"Clave": "MAX_SEARCH_PAGES_PER_QUERY", "Valor": settings.MAX_SEARCH_PAGES_PER_QUERY, "Descripción": "Páginas máximas del buscador a escanear por query"},
            {"Clave": "REVIEW_DOMAINS", "Valor": "revistadelibros.com,nueva-revista.net,aceprensa.com,elcultural.com,zendalibros.com,babelia.elpais.com", "Descripción": "Dominios culturales/literarios recomendados para búsquedas específicas (separados por coma)"},
            {"Clave": "SEARCH_DELAY_SECONDS", "Valor": settings.SEARCH_DELAY_SECONDS, "Descripción": "Espera en segundos entre cada búsqueda para evitar bloqueos"},
            {"Clave": "SEARCH_BACKOFF_SECONDS", "Valor": settings.SEARCH_BACKOFF_SECONDS, "Descripción": "Espera de enfriamiento en segundos si se detecta rate limit o error"},
            {"Clave": "MAX_QUERIES_PER_BOOK", "Valor": settings.MAX_QUERIES_PER_BOOK, "Descripción": "Límite máximo de búsquedas por libro"},
            {"Clave": "ENABLE_GOOGLE_NEWS_RSS", "Valor": settings.ENABLE_GOOGLE_NEWS_RSS, "Descripción": "Activar búsqueda complementaria mediante Google News RSS (true/false)"},
            {"Clave": "SEARCH_PROVIDER_MODE", "Valor": settings.SEARCH_PROVIDER_MODE, "Descripción": "Modo de proveedor de búsqueda: auto, free_only, google_news_only, serpapi, dataforseo"},
            {"Clave": "ENABLE_SERPAPI", "Valor": settings.ENABLE_SERPAPI, "Descripción": "Activar proveedor SerpAPI (true/false)"},
            {"Clave": "SERPAPI_API_KEY", "Valor": settings.SERPAPI_API_KEY or "", "Descripción": "API Key de SerpAPI"},
            {"Clave": "ENABLE_DATAFORSEO", "Valor": settings.ENABLE_DATAFORSEO, "Descripción": "Activar proveedor DataForSEO (true/false)"},
            {"Clave": "DATAFORSEO_LOGIN", "Valor": settings.DATAFORSEO_LOGIN or "", "Descripción": "Login (username/email) de DataForSEO"},
            {"Clave": "DATAFORSEO_PASSWORD", "Valor": settings.DATAFORSEO_PASSWORD or "", "Descripción": "Password de DataForSEO"},
            {"Clave": "ENABLE_DOMAIN_INDEX", "Valor": settings.ENABLE_DOMAIN_INDEX, "Descripción": "Activar indexación de dominios culturales (true/false)"},
            {"Clave": "DOMAIN_INDEX_MAX_URLS_PER_DOMAIN", "Valor": settings.DOMAIN_INDEX_MAX_URLS_PER_DOMAIN, "Descripción": "Máximo de URLs a indexar por dominio"},
            {"Clave": "DOMAIN_INDEX_REFRESH_DAYS", "Valor": settings.DOMAIN_INDEX_REFRESH_DAYS, "Descripción": "Días entre reindexaciones de un mismo dominio"},
            {"Clave": "DOMAIN_INDEX_MIN_SCORE", "Valor": settings.DOMAIN_INDEX_MIN_SCORE, "Descripción": "Score mínimo para considerar un URL candidato (0-100)"},
            {"Clave": "DOMAIN_INDEX_DB_PATH", "Valor": settings.DOMAIN_INDEX_DB_PATH, "Descripción": "Ruta al fichero SQLite del índice local"},
            {"Clave": "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES", "Valor": settings.DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES, "Descripción": "Queries máximas de GoogleNewsRss como complemento en modo domain_index_plus_news"},
            {"Clave": "BLOCK_PROVIDER_FOR_FULL_RUN", "Valor": settings.BLOCK_PROVIDER_FOR_FULL_RUN, "Descripción": "Bloquear proveedores permanentemente durante todo el run (true/false)"},
            {"Clave": "ENRICH_INDEXED_URLS", "Valor": settings.ENRICH_INDEXED_URLS, "Descripción": "Activar descarga de páginas para enriquecer metadatos (true/false)"},
            {"Clave": "DOMAIN_INDEX_ENRICH_MAX_PER_DOMAIN", "Valor": settings.DOMAIN_INDEX_ENRICH_MAX_PER_DOMAIN, "Descripción": "Cantidad máxima de URLs a enriquecer por dominio"},
            {"Clave": "DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS", "Valor": settings.DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS, "Descripción": "Timeout en segundos para la descarga de páginas"},
            {"Clave": "DISCOVER_INTERNAL_ARTICLE_LINKS", "Valor": settings.DISCOVER_INTERNAL_ARTICLE_LINKS, "Descripción": "Descubrir enlaces a artículos dentro de páginas índice (true/false)"},
            {"Clave": "DOMAIN_INDEX_INTERNAL_LINK_DEPTH", "Valor": settings.DOMAIN_INDEX_INTERNAL_LINK_DEPTH, "Descripción": "Profundidad de rastreo de enlaces internos"},
            {"Clave": "DOMAIN_INDEX_MAX_INTERNAL_LINKS_PER_PAGE", "Valor": settings.DOMAIN_INDEX_MAX_INTERNAL_LINKS_PER_PAGE, "Descripción": "Cantidad máxima de enlaces internos a descubrir por página índice"},
            {"Clave": "ENABLE_INTERNAL_DOMAIN_SEARCH", "Valor": settings.ENABLE_INTERNAL_DOMAIN_SEARCH, "Descripción": "Activar búsqueda interna en dominios de fuentes culturales (true/false)"},
            {"Clave": "INTERNAL_SEARCH_MAX_QUERIES_PER_BOOK", "Valor": settings.INTERNAL_SEARCH_MAX_QUERIES_PER_BOOK, "Descripción": "Cantidad máxima de consultas de búsqueda interna por libro"},
            {"Clave": "INTERNAL_SEARCH_MAX_RESULTS_PER_DOMAIN", "Valor": settings.INTERNAL_SEARCH_MAX_RESULTS_PER_DOMAIN, "Descripción": "Resultados máximos a extraer por dominio en búsqueda interna"},
            {"Clave": "INTERNAL_SEARCH_TIMEOUT_SECONDS", "Valor": settings.INTERNAL_SEARCH_TIMEOUT_SECONDS, "Descripción": "Timeout en segundos para la búsqueda interna"},
            {"Clave": "INTERNAL_SEARCH_DOMAINS_LIMIT", "Valor": settings.INTERNAL_SEARCH_DOMAINS_LIMIT, "Descripción": "Límite máximo de dominios a consultar en búsqueda interna"},
            {"Clave": "DEFAULT_INCLUDE_UNKNOWN_DATES", "Valor": settings.DEFAULT_INCLUDE_UNKNOWN_DATES, "Descripción": "Incluir artículos sin fecha de publicación detectada por defecto (true/false)"},
            {"Clave": "DEFAULT_DATE_MIN", "Valor": settings.DEFAULT_DATE_MIN or "", "Descripción": "Fecha de publicación mínima por defecto (YYYY-MM-DD)"},
            {"Clave": "DEFAULT_DATE_MAX", "Valor": settings.DEFAULT_DATE_MAX or "", "Descripción": "Fecha de publicación máxima por defecto (YYYY-MM-DD)"},
            {"Clave": "MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH", "Valor": getattr(settings, "MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH", 5), "Descripción": "Mínimo de candidatos requeridos antes de activar la búsqueda interna profunda"},
            {"Clave": "MIN_CANDIDATES_BEFORE_AI", "Valor": getattr(settings, "MIN_CANDIDATES_BEFORE_AI", 1), "Descripción": "Mínimo de candidatos requeridos para ejecutar el análisis IA de OpenAI"},
            {"Clave": "ENABLE_CASCADE_SEARCH", "Valor": getattr(settings, "ENABLE_CASCADE_SEARCH", True), "Descripción": "Activar búsqueda en cascada (Domain Index -> RSS -> Búsqueda Interna)"},
            {"Clave": "ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS", "Valor": getattr(settings, "ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS", True), "Descripción": "Activar búsqueda interna si el total de candidatos es bajo"},
            {"Clave": "ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH", "Valor": getattr(settings, "ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH", True), "Descripción": "Ejecutar siempre búsqueda interna en dominios activos (true=siempre, false=solo si pocos candidatos)"},
            {"Clave": "BACKEND_BASE_URL", "Valor": "http://127.0.0.1:8000", "Descripción": "URL base del backend para Apps Script"},
            {"Clave": "ADMIN_TOKEN", "Valor": settings.ADMIN_TOKEN or "secret_admin_token", "Descripción": "Token de administración secreto para Apps Script (cabecera X-Admin-Token)"},
            {"Clave": "WORDPRESS_BASE_URL", "Valor": settings.WORDPRESS_BASE_URL or "", "Descripción": "URL base de WordPress (ej. https://miweb.com)"},
            {"Clave": "WORDPRESS_USERNAME", "Valor": settings.WORDPRESS_USERNAME or "", "Descripción": "Usuario administrador/editor de WordPress"},
            {"Clave": "WORDPRESS_POST_TYPE", "Valor": settings.WORDPRESS_POST_TYPE, "Descripción": "Tipo de post en WordPress (posts, pages)"},
            {"Clave": "WORDPRESS_DEFAULT_CATEGORY_ID", "Valor": settings.WORDPRESS_DEFAULT_CATEGORY_ID or "", "Descripción": "ID de categoría de WordPress por defecto (opcional)"},
            {"Clave": "LOG_RETENTION_DAYS", "Valor": settings.LOG_RETENTION_DAYS, "Descripción": "Días de retención de logs en la pestaña Logs"},
            {"Clave": "DESCARTES_RETENTION_DAYS", "Valor": getattr(settings, "DESCARTES_RETENTION_DAYS", 30), "Descripción": "Días de retención de descartes en la pestaña Descartes"}
        ]

        # 1. Move technical keys currently in Config to Config técnica
        basic_keys_set = {b["Clave"] for b in basic_defaults}
        for row in config_rows:
            k = row.get("Clave")
            v = row.get("Valor")
            d = row.get("Descripción", "")
            if k and k not in basic_keys_set:
                if k not in existing_tech:
                    tech_ws.append_row([k, v, d], value_input_option="USER_ENTERED")
                    existing_tech[k] = {"Clave": k, "Valor": v, "Descripción": d}

        # 2. Re-write Config keeping only basic keys
        config_ws.clear()
        config_ws.append_row(["Clave", "Valor", "Descripción"])
        for b in basic_defaults:
            k = b["Clave"]
            val = existing_basic[k]["Valor"] if k in existing_basic else b["Valor"]
            desc = existing_basic[k]["Descripción"] if k in existing_basic else b["Descripción"]
            config_ws.append_row([k, val, desc], value_input_option="USER_ENTERED")

        # 3. Ensure all technical defaults exist in Config técnica
        for t in technical_defaults:
            k = t["Clave"]
            if k not in existing_tech:
                tech_ws.append_row([t["Clave"], t["Valor"], t["Descripción"]], value_input_option="USER_ENTERED")

        # Check and update existing technical limits if they are too low
        try:
            tech_all = tech_ws.get_all_values()
            if tech_all:
                tech_headers = tech_all[0]
                if "Clave" in tech_headers and "Valor" in tech_headers:
                    col_clave = tech_headers.index("Clave") + 1
                    col_valor = tech_headers.index("Valor") + 1
                    for r_idx, row in enumerate(tech_all[1:], start=2):
                        if col_clave - 1 < len(row) and col_valor - 1 < len(row):
                            clave = row[col_clave - 1]
                            valor_str = row[col_valor - 1]
                            try:
                                valor_int = int(valor_str)
                            except ValueError:
                                continue
                            if clave == "MAX_QUERIES_PER_BOOK" and valor_int < 12:
                                tech_ws.update_cell(r_idx, col_valor, 12)
                                logger.info(f"Updated MAX_QUERIES_PER_BOOK from {valor_int} to 12 in Config técnica")
                                logger_service.log("WARNING", "CONFIG_LOW_QUERY_LIMIT_WARNING", f"Actualizado límite bajo de MAX_QUERIES_PER_BOOK ({valor_int} -> 12)", sheet_id=sheet_id)
                            if clave == "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES" and valor_int < 10:
                                tech_ws.update_cell(r_idx, col_valor, 10)
                                logger.info(f"Updated DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES from {valor_int} to 10 in Config técnica")
                                logger_service.log("WARNING", "CONFIG_LOW_QUERY_LIMIT_WARNING", f"Actualizado límite bajo de DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES ({valor_int} -> 10)", sheet_id=sheet_id)
        except Exception as e_upd:
            logger.warning(f"Error checking/updating low query limits in Config técnica: {e_upd}")

        # Initialise Fuentes tab with default domains if empty
        fuentes_ws = spreadsheet.worksheet("Fuentes")
        fuentes_rows = fuentes_ws.get_all_records()
        if not fuentes_rows:
            default_sources = [
                ["revistadelibros.com",  "true", "cultural", "", "", "", "Revista de libros", "", "", ""],
                ["nueva-revista.net",    "true", "cultural", "", "", "", "Nueva Revista",      "", "", ""],
                ["aceprensa.com",        "true", "cultural", "", "", "", "Aceprensa",           "", "", ""],
                ["elcultural.com",       "true", "cultural", "", "", "", "El Cultural",         "", "", ""],
                ["zendalibros.com",      "true", "cultural", "", "", "", "Zenda Libros",        "", "", ""],
                ["babelia.elpais.com",   "true", "cultural", "", "", "", "Babelia/El País",     "", "", ""],
                ["wmagazin.com",         "true", "cultural", "", "", "", "WMagazín",            "", "", ""],
                ["theobjective.com",     "true", "cultural", "", "", "", "The Objective",       "", "", ""],
                ["ethic.es",             "true", "cultural", "", "", "", "Ethic",               "", "", ""],
                ["eldebate.com",         "true", "cultural", "", "", "", "El Debate",           "", "", ""],
            ]
            for row in default_sources:
                fuentes_ws.append_row(row)

        # Cleanup empty/false rows in Reseñas por publicar automatically on ensure_sheet
        try:
            self.cleanup_empty_publication_rows(sheet_id)
        except Exception as e_clean:
            import logging
            logging.getLogger("encuentro-noticias").warning(f"Auto empty rows cleanup in ensure_sheet failed: {e_clean}")

        return {
            "success": True,
            "sheet_id": sheet_id,
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}",
            "created_tabs": created_tabs
        }

    def get_config_dict(self, sheet_id: str) -> Dict[str, Any]:
        """
        Reads configurations from Config técnica first, then Config (user override), falls back to env settings.
        """
        client = self.get_client()
        try:
            spreadsheet = client.open_by_key(sheet_id)
            
            # 1. Read technical configs
            tech_dict = {}
            try:
                tech_ws = spreadsheet.worksheet("Config técnica")
                tech_records = tech_ws.get_all_records()
                for r in tech_records:
                    key = r.get("Clave")
                    val = r.get("Valor")
                    if key and val is not None:
                        tech_dict[key] = val
            except Exception as e_tech:
                logger.warning(f"Could not read Config técnica tab: {e_tech}")

            # 2. Read basic config (takes priority on duplicates)
            user_dict = {}
            try:
                config_ws = spreadsheet.worksheet("Config")
                config_records = config_ws.get_all_records()
                for r in config_records:
                    key = r.get("Clave")
                    val = r.get("Valor")
                    if key and val is not None:
                        user_dict[key] = val
            except Exception as e_user:
                logger.warning(f"Could not read Config tab: {e_user}")

            # Combine them: user_dict overwrites tech_dict
            config_dict = {**tech_dict, **user_dict}
            logger.info(f"CONFIG_LOADED: merged {len(config_dict)} total keys (User keys: {len(user_dict)}, Tech keys: {len(tech_dict)})")

            return {
                "MAX_BOOKS_PER_RUN": parse_int(config_dict.get("MAX_BOOKS_PER_RUN"), settings.MAX_BOOKS_PER_RUN),
                "MAX_SEARCH_PAGES_PER_QUERY": parse_int(config_dict.get("MAX_SEARCH_PAGES_PER_QUERY"), settings.MAX_SEARCH_PAGES_PER_QUERY),
                "MAX_CANDIDATES_PER_BOOK": parse_int(config_dict.get("MAX_CANDIDATES_PER_BOOK"), settings.MAX_CANDIDATES_PER_BOOK),
                "MIN_MATCH_SCORE": parse_int(config_dict.get("MIN_MATCH_SCORE"), settings.MIN_MATCH_SCORE),
                "OPENAI_MODEL": parse_str(config_dict.get("OPENAI_MODEL"), settings.OPENAI_MODEL),
                "REVIEW_DOMAINS": parse_str(config_dict.get("REVIEW_DOMAINS"), ""),
                "SEARCH_DELAY_SECONDS": parse_float(config_dict.get("SEARCH_DELAY_SECONDS"), settings.SEARCH_DELAY_SECONDS),
                "SEARCH_BACKOFF_SECONDS": parse_float(config_dict.get("SEARCH_BACKOFF_SECONDS"), settings.SEARCH_BACKOFF_SECONDS),
                "MAX_QUERIES_PER_BOOK": parse_int(config_dict.get("MAX_QUERIES_PER_BOOK"), settings.MAX_QUERIES_PER_BOOK),
                "ENABLE_GOOGLE_NEWS_RSS": parse_bool(config_dict.get("ENABLE_GOOGLE_NEWS_RSS"), settings.ENABLE_GOOGLE_NEWS_RSS),
                "SEARCH_PROVIDER_MODE": parse_str(config_dict.get("SEARCH_PROVIDER_MODE"), settings.SEARCH_PROVIDER_MODE),
                "ENABLE_SERPAPI": parse_bool(config_dict.get("ENABLE_SERPAPI"), settings.ENABLE_SERPAPI),
                "SERPAPI_API_KEY": parse_str(config_dict.get("SERPAPI_API_KEY"), settings.SERPAPI_API_KEY),
                "ENABLE_DATAFORSEO": parse_bool(config_dict.get("ENABLE_DATAFORSEO"), settings.ENABLE_DATAFORSEO),
                "DATAFORSEO_LOGIN": parse_str(config_dict.get("DATAFORSEO_LOGIN"), settings.DATAFORSEO_LOGIN),
                "DATAFORSEO_PASSWORD": parse_str(config_dict.get("DATAFORSEO_PASSWORD"), settings.DATAFORSEO_PASSWORD),
                "BLOCK_PROVIDER_FOR_FULL_RUN": parse_bool(config_dict.get("BLOCK_PROVIDER_FOR_FULL_RUN"), settings.BLOCK_PROVIDER_FOR_FULL_RUN),
                "ENABLE_DOMAIN_INDEX": parse_bool(config_dict.get("ENABLE_DOMAIN_INDEX"), settings.ENABLE_DOMAIN_INDEX),
                "DOMAIN_INDEX_MAX_URLS_PER_DOMAIN": parse_int(config_dict.get("DOMAIN_INDEX_MAX_URLS_PER_DOMAIN"), settings.DOMAIN_INDEX_MAX_URLS_PER_DOMAIN),
                "DOMAIN_INDEX_REFRESH_DAYS": parse_int(config_dict.get("DOMAIN_INDEX_REFRESH_DAYS"), settings.DOMAIN_INDEX_REFRESH_DAYS),
                "DOMAIN_INDEX_MIN_SCORE": parse_int(config_dict.get("DOMAIN_INDEX_MIN_SCORE"), settings.DOMAIN_INDEX_MIN_SCORE),
                "DOMAIN_INDEX_DB_PATH": parse_str(config_dict.get("DOMAIN_INDEX_DB_PATH"), settings.DOMAIN_INDEX_DB_PATH),
                "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES": parse_int(config_dict.get("DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES"), settings.DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES),
                "ENRICH_INDEXED_URLS": parse_bool(config_dict.get("ENRICH_INDEXED_URLS"), settings.ENRICH_INDEXED_URLS),
                "DOMAIN_INDEX_ENRICH_MAX_PER_DOMAIN": parse_int(config_dict.get("DOMAIN_INDEX_ENRICH_MAX_PER_DOMAIN"), settings.DOMAIN_INDEX_ENRICH_MAX_PER_DOMAIN),
                "DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS": parse_int(config_dict.get("DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS"), settings.DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS),
                "DISCOVER_INTERNAL_ARTICLE_LINKS": parse_bool(config_dict.get("DISCOVER_INTERNAL_ARTICLE_LINKS"), settings.DISCOVER_INTERNAL_ARTICLE_LINKS),
                "DOMAIN_INDEX_INTERNAL_LINK_DEPTH": parse_int(config_dict.get("DOMAIN_INDEX_INTERNAL_LINK_DEPTH"), settings.DOMAIN_INDEX_INTERNAL_LINK_DEPTH),
                "DOMAIN_INDEX_MAX_INTERNAL_LINKS_PER_PAGE": parse_int(config_dict.get("DOMAIN_INDEX_MAX_INTERNAL_LINKS_PER_PAGE"), settings.DOMAIN_INDEX_MAX_INTERNAL_LINKS_PER_PAGE),
                "ENABLE_INTERNAL_DOMAIN_SEARCH": parse_bool(config_dict.get("ENABLE_INTERNAL_DOMAIN_SEARCH"), settings.ENABLE_INTERNAL_DOMAIN_SEARCH),
                "INTERNAL_SEARCH_MAX_QUERIES_PER_BOOK": parse_int(config_dict.get("INTERNAL_SEARCH_MAX_QUERIES_PER_BOOK"), settings.INTERNAL_SEARCH_MAX_QUERIES_PER_BOOK),
                "INTERNAL_SEARCH_MAX_RESULTS_PER_DOMAIN": parse_int(config_dict.get("INTERNAL_SEARCH_MAX_RESULTS_PER_DOMAIN"), settings.INTERNAL_SEARCH_MAX_RESULTS_PER_DOMAIN),
                "INTERNAL_SEARCH_TIMEOUT_SECONDS": parse_int(config_dict.get("INTERNAL_SEARCH_TIMEOUT_SECONDS"), settings.INTERNAL_SEARCH_TIMEOUT_SECONDS),
                "INTERNAL_SEARCH_DOMAINS_LIMIT": parse_int(config_dict.get("INTERNAL_SEARCH_DOMAINS_LIMIT"), settings.INTERNAL_SEARCH_DOMAINS_LIMIT),
                "DEFAULT_INCLUDE_UNKNOWN_DATES": parse_bool(config_dict.get("DEFAULT_INCLUDE_UNKNOWN_DATES"), settings.DEFAULT_INCLUDE_UNKNOWN_DATES),
                "DEFAULT_DATE_MIN": parse_str(config_dict.get("DEFAULT_DATE_MIN"), settings.DEFAULT_DATE_MIN or ""),
                "DEFAULT_DATE_MAX": parse_str(config_dict.get("DEFAULT_DATE_MAX"), settings.DEFAULT_DATE_MAX or ""),
                "MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH": parse_int(config_dict.get("MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH"), getattr(settings, "MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH", 5)),
                "MIN_CANDIDATES_BEFORE_AI": parse_int(config_dict.get("MIN_CANDIDATES_BEFORE_AI"), getattr(settings, "MIN_CANDIDATES_BEFORE_AI", 1)),
                "ENABLE_CASCADE_SEARCH": parse_bool(config_dict.get("ENABLE_CASCADE_SEARCH"), getattr(settings, "ENABLE_CASCADE_SEARCH", True)),
                "ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS": parse_bool(config_dict.get("ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS"), getattr(settings, "ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS", True)),
                "ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH": parse_bool(config_dict.get("ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH"), getattr(settings, "ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH", True)),
                "DESCARTES_RETENTION_DAYS": parse_int(config_dict.get("DESCARTES_RETENTION_DAYS"), getattr(settings, "DESCARTES_RETENTION_DAYS", 30)),
                "DESCARTES_MAX_ROWS": parse_int(config_dict.get("DESCARTES_MAX_ROWS"), getattr(settings, "DESCARTES_MAX_ROWS", 1000)),
                "LOG_RETENTION_DAYS": parse_int(config_dict.get("LOG_RETENTION_DAYS"), settings.LOG_RETENTION_DAYS),
                "LOG_MAX_ROWS": parse_int(config_dict.get("LOG_MAX_ROWS"), settings.LOG_MAX_ROWS),
            }
        except Exception:
            # Fallback to local configs if sheet configs fail to read
            return {
                "MAX_BOOKS_PER_RUN": settings.MAX_BOOKS_PER_RUN,
                "MAX_SEARCH_PAGES_PER_QUERY": settings.MAX_SEARCH_PAGES_PER_QUERY,
                "MAX_CANDIDATES_PER_BOOK": settings.MAX_CANDIDATES_PER_BOOK,
                "MIN_MATCH_SCORE": settings.MIN_MATCH_SCORE,
                "OPENAI_MODEL": settings.OPENAI_MODEL,
                "REVIEW_DOMAINS": "",
                "SEARCH_DELAY_SECONDS": settings.SEARCH_DELAY_SECONDS,
                "SEARCH_BACKOFF_SECONDS": settings.SEARCH_BACKOFF_SECONDS,
                "MAX_QUERIES_PER_BOOK": settings.MAX_QUERIES_PER_BOOK,
                "ENABLE_GOOGLE_NEWS_RSS": settings.ENABLE_GOOGLE_NEWS_RSS,
                "SEARCH_PROVIDER_MODE": settings.SEARCH_PROVIDER_MODE,
                "ENABLE_SERPAPI": settings.ENABLE_SERPAPI,
                "SERPAPI_API_KEY": settings.SERPAPI_API_KEY,
                "ENABLE_DATAFORSEO": settings.ENABLE_DATAFORSEO,
                "DATAFORSEO_LOGIN": settings.DATAFORSEO_LOGIN,
                "DATAFORSEO_PASSWORD": settings.DATAFORSEO_PASSWORD,
                "BLOCK_PROVIDER_FOR_FULL_RUN": settings.BLOCK_PROVIDER_FOR_FULL_RUN,
                "ENABLE_DOMAIN_INDEX": settings.ENABLE_DOMAIN_INDEX,
                "DOMAIN_INDEX_MAX_URLS_PER_DOMAIN": settings.DOMAIN_INDEX_MAX_URLS_PER_DOMAIN,
                "DOMAIN_INDEX_REFRESH_DAYS": settings.DOMAIN_INDEX_REFRESH_DAYS,
                "DOMAIN_INDEX_MIN_SCORE": settings.DOMAIN_INDEX_MIN_SCORE,
                "DOMAIN_INDEX_DB_PATH": settings.DOMAIN_INDEX_DB_PATH,
                "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES": settings.DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES,
                "ENRICH_INDEXED_URLS": settings.ENRICH_INDEXED_URLS,
                "DOMAIN_INDEX_ENRICH_MAX_PER_DOMAIN": settings.DOMAIN_INDEX_ENRICH_MAX_PER_DOMAIN,
                "DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS": settings.DOMAIN_INDEX_ENRICH_TIMEOUT_SECONDS,
                "DISCOVER_INTERNAL_ARTICLE_LINKS": settings.DISCOVER_INTERNAL_ARTICLE_LINKS,
                "DOMAIN_INDEX_INTERNAL_LINK_DEPTH": settings.DOMAIN_INDEX_INTERNAL_LINK_DEPTH,
                "DOMAIN_INDEX_MAX_INTERNAL_LINKS_PER_PAGE": settings.DOMAIN_INDEX_MAX_INTERNAL_LINKS_PER_PAGE,
                "ENABLE_INTERNAL_DOMAIN_SEARCH": settings.ENABLE_INTERNAL_DOMAIN_SEARCH,
                "INTERNAL_SEARCH_MAX_QUERIES_PER_BOOK": settings.INTERNAL_SEARCH_MAX_QUERIES_PER_BOOK,
                "INTERNAL_SEARCH_MAX_RESULTS_PER_DOMAIN": settings.INTERNAL_SEARCH_MAX_RESULTS_PER_DOMAIN,
                "INTERNAL_SEARCH_TIMEOUT_SECONDS": settings.INTERNAL_SEARCH_TIMEOUT_SECONDS,
                "INTERNAL_SEARCH_DOMAINS_LIMIT": settings.INTERNAL_SEARCH_DOMAINS_LIMIT,
                "DEFAULT_INCLUDE_UNKNOWN_DATES": settings.DEFAULT_INCLUDE_UNKNOWN_DATES,
                "DEFAULT_DATE_MIN": settings.DEFAULT_DATE_MIN or "",
                "DEFAULT_DATE_MAX": settings.DEFAULT_DATE_MAX or "",
                "MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH": getattr(settings, "MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH", 5),
                "MIN_CANDIDATES_BEFORE_AI": getattr(settings, "MIN_CANDIDATES_BEFORE_AI", 1),
                "ENABLE_CASCADE_SEARCH": getattr(settings, "ENABLE_CASCADE_SEARCH", True),
                "ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS": getattr(settings, "ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS", True),
                "ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH": getattr(settings, "ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH", True),
                "DESCARTES_RETENTION_DAYS": getattr(settings, "DESCARTES_RETENTION_DAYS", 30),
                "DESCARTES_MAX_ROWS": getattr(settings, "DESCARTES_MAX_ROWS", 1000),
                "LOG_RETENTION_DAYS": settings.LOG_RETENTION_DAYS,
                "LOG_MAX_ROWS": settings.LOG_MAX_ROWS,
            }

    def get_pending_books(
        self,
        sheet_id: str,
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        Reads Libros tab. A row is eligible to process if it has a non-empty title and ¿Incluir en búsqueda? is True/empty.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Libros")
        records = worksheet.get_all_records()

        books = []
        rows_read = 0
        skipped_missing_title = 0
        skipped_not_included = 0
        skipped_blocked_status = 0

        BLOCKED_STATUSES = {"no buscar"}

        for index, row in enumerate(records, start=2):  # Headers are row 1
            isbn = str(row.get("ISBN", "")).strip()
            title = str(row.get("Título del libro", "")).strip()
            author = str(row.get("Autor del libro", "")).strip()
            status = str(row.get("Estado", "")).strip().lower()

            # Skip fully empty rows
            if not isbn and not title and not author and not status:
                continue

            rows_read += 1

            if not title:
                skipped_missing_title += 1
                continue

            # Check inclusion checkbox (empty/blank is treated as TRUE by default)
            incluir_raw = str(row.get("¿Incluir en búsqueda?", "")).strip().lower()
            if incluir_raw == "":
                incluir_raw = "true"
            is_included = incluir_raw != "false"

            if not is_included:
                skipped_not_included += 1
                continue

            # Skip only explicitly blocked statuses
            if status in BLOCKED_STATUSES:
                skipped_blocked_status += 1
                continue

            books.append({
                "row_index": index,
                "isbn": isbn,
                "title": title,
                "author": author,
                "previous_status": status or "sin_estado"
            })

            if len(books) >= limit:
                break

        return {
            "books": books,
            "books_rows_read": rows_read,
            "books_pending_detected": len(books),
            "books_skipped_missing_title": skipped_missing_title,
            "books_skipped_not_included": skipped_not_included,
            "books_skipped_blocked_status": skipped_blocked_status
        }


    def get_book_by_isbn(self, sheet_id: str, isbn: str) -> Optional[Dict[str, Any]]:
        """
        Finds a specific book by ISBN in the Libros tab.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Libros")
        records = worksheet.get_all_records()

        isbn_clean = str(isbn).strip()
        for index, row in enumerate(records, start=2):
            row_isbn = str(row.get("ISBN", "")).strip()
            if row_isbn == isbn_clean:
                return {
                    "row_index": index,
                    "isbn": row_isbn,
                    "title": str(row.get("Título del libro", "")).strip(),
                    "author": str(row.get("Autor del libro", "")).strip(),
                    "status": str(row.get("Estado", "")).strip()
                }
        return None

    def update_book_status(self, sheet_id: str, row_index: int, status: str, last_run: str, reviews_found: int, observations: str):
        """
        Updates the status fields of a book row in Libros tab.
        Uses a range update if contiguous, otherwise falls back to individual cell updates.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Libros")
        
        headers = worksheet.row_values(1)
        
        def get_col_index(name: str) -> Optional[int]:
            try:
                return headers.index(name) + 1
            except ValueError:
                return None

        col_estado = get_col_index("Estado") or 5
        col_last_run = get_col_index("Última ejecución") or 6
        col_reviews = get_col_index("Reseñas encontradas") or 7
        col_obs = get_col_index("Observaciones") or 8

        if col_last_run == col_estado + 1 and col_reviews == col_estado + 2 and col_obs == col_estado + 3:
            col_start_letter = col_num_to_letter(col_estado)
            col_end_letter = col_num_to_letter(col_obs)
            range_name = f"{col_start_letter}{row_index}:{col_end_letter}{row_index}"
            values = [[status, last_run, reviews_found, observations]]
            worksheet.update(range_name, values)
        else:
            worksheet.update_cell(row_index, col_estado, status)
            worksheet.update_cell(row_index, col_last_run, last_run)
            worksheet.update_cell(row_index, col_reviews, reviews_found)
            worksheet.update_cell(row_index, col_obs, observations)

    def get_books_status_summary(self, sheet_id: str) -> Dict[str, int]:
        """
        Reads Libros tab and returns a count summary by status.
        """
        client = self.get_client()
        try:
            spreadsheet = client.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet("Libros")
            records = worksheet.get_all_records()
        except Exception:
            return {"pendiente": 0, "procesando": 0, "completado": 0, "sin_resultados": 0, "error": 0, "total": 0}

        counts = {"pendiente": 0, "procesando": 0, "completado": 0, "sin_resultados": 0, "error": 0}
        total = 0
        
        for row in records:
            isbn = str(row.get("ISBN", "")).strip()
            title = str(row.get("Título del libro", "")).strip()
            author = str(row.get("Autor del libro", "")).strip()
            if isbn and title and author:
                total += 1
                status = str(row.get("Estado", "")).strip().lower()
                if status in ("", "pendiente", "none"):
                    counts["pendiente"] += 1
                elif status in counts:
                    counts[status] += 1
                else:
                    counts["pendiente"] += 1

        counts["total"] = total
        return counts

    def get_all_reviews(self, sheet_id: str) -> List[Dict[str, Any]]:
        """
        Reads all items from Reseñas.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Reseñas por publicar")
        return worksheet.get_all_records()

    def add_review(self, sheet_id: str, review_dict: Dict[str, Any]):
        """
        Appends or overwrites a row in Reseñas por publicar using header names.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Reseñas por publicar")
        
        def clean_val(val: Any) -> Any:
            if isinstance(val, str):
                s = val.strip()
                if s.lower() in ("titulo web", "título web", "autor web", "autor web "):
                    return ""
                return s
            return val

        headers = worksheet.row_values(1)
        full_row = []
        for h in headers:
            val = review_dict.get(h)
            if val is None:
                if h == "¿Publicar?":
                    val = False
                else:
                    val = ""
            full_row.append(clean_val(val))
            
        records = worksheet.get_all_records()
        overwrite_row_index = None
        for idx, row in enumerate(records):
            if not self.is_row_real(row):
                overwrite_row_index = idx + 2
                break
                
        if overwrite_row_index:
            worksheet.update(range_name=f"A{overwrite_row_index}", values=[full_row])
        else:
            worksheet.append_row(full_row)


    def add_descarte(self, sheet_id: str, descarte_data: List[Any]):
        """
        Appends a row to Descartes.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Descartes")
        worksheet.append_row(descarte_data)

    def add_log(self, sheet_id: str, log_data: List[Any]):
        """
        Appends a single row to Logs. Use add_log_batch for multiple rows.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Logs")
        worksheet.append_row(log_data)

    def add_log_batch(self, sheet_id: str, log_rows: List[List[Any]]):
        """
        Appends multiple rows to Logs in a single API call (reduces quota usage).
        """
        if not log_rows:
            return
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Logs")
        worksheet.append_rows(log_rows, value_input_option="RAW")

    def update_reviews_hashes(self, sheet_id: str, updates: List[Tuple[int, str]]):
        """
        Updates the deduplication hash for multiple rows.
        updates: list of (row_index, hash_value)
        Col S (19) is the 'Hash deduplicación' column.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Reseñas por publicar")
        
        # Get headers to find "Hash deduplicación" column index dynamically
        headers = worksheet.row_values(1)
        col_idx = 26  # default fallback if not found
        if "Hash deduplicación" in headers:
            col_idx = headers.index("Hash deduplicación") + 1
            
        for row_idx, hash_val in updates:
            worksheet.update_cell(row_idx, col_idx, hash_val)

    def get_active_sources(self, sheet_id: str) -> List[Dict[str, Any]]:
        """
        Reads the Fuentes tab and returns active domain configs.
        """
        try:
            client = self.get_client()
            spreadsheet = client.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet("Fuentes")
            records = worksheet.get_all_records()
        except Exception as e:
            import logging
            logging.getLogger("encuentro-noticias").warning(f"get_active_sources error: {e}")
            return []

        sources = []
        for i, row in enumerate(records, start=2):
            domain_raw = str(row.get("Dominio", "")).strip()
            if not domain_raw:
                continue
            domain = clean_domain_string(domain_raw)
            if not domain:
                continue
            active_val = str(row.get("Activo", "true")).strip().lower()
            if active_val not in ("true", "1", "yes", "sí", "si"):
                continue
            sources.append({
                "domain": domain,
                "active": True,
                "tipo": str(row.get("Tipo", "cultural")).strip(),
                "sitemap_url": str(row.get("Sitemap URL", "")).strip(),
                "rss_url": str(row.get("RSS URL", "")).strip(),
                "row_index": i,
            })
        return sources

    def update_source_stats(
        self,
        sheet_id: str,
        domain: str,
        last_indexed: str,
        urls_indexed: int,
        errors: str,
    ):
        """
        Updates Última indexación, URLs indexadas, Errores columns for a domain in Fuentes tab.
        Columns: H=Última indexación, I=URLs indexadas, J=Errores
        """
        try:
            client = self.get_client()
            spreadsheet = client.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet("Fuentes")
            records = worksheet.get_all_records()
            for i, row in enumerate(records, start=2):
                row_dom_raw = str(row.get("Dominio", "")).strip()
                if clean_domain_string(row_dom_raw) == clean_domain_string(domain):
                    worksheet.update(f"H{i}:J{i}", [[last_indexed, urls_indexed, errors]])
                    break
        except Exception as e:
            import logging
            logging.getLogger("encuentro-noticias").warning(f"update_source_stats error: {e}")

    def clear_all_rows(self, sheet_id: str, worksheet_name: str) -> Dict[str, Any]:
        """Clears all rows in a worksheet except the first row (header)."""
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet(worksheet_name)
        num_rows = worksheet.row_count
        deleted_count = 0
        if num_rows > 1:
            reqs = [{
                "deleteDimension": {
                    "range": {
                        "sheetId": worksheet.id,
                        "dimension": "ROWS",
                        "startIndex": 1,
                        "endIndex": num_rows
                    }
                }
            }]
            spreadsheet.batch_update({"requests": reqs})
            deleted_count = num_rows - 1
            
        return {
            "deleted_count": deleted_count,
            "remaining_count": 0,
            "message": f"Se eliminaron todas las filas de la pestaña '{worksheet_name}'."
        }

    def cleanup_logs(self, sheet_id: str, max_rows: int = 1000, retention_days: int = 30) -> Dict[str, Any]:
        """
        Cleans up old logs based on retention days and a maximum row limit.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Logs")
        records = worksheet.get_all_records()
        if not records:
            return {"deleted_count": 0, "remaining_count": 0, "message": "La hoja de Logs está vacía."}
            
        now = get_now_madrid()
        cutoff_date = now - datetime.timedelta(days=retention_days)
        
        rows_to_delete = []
        
        # Parse date column: "Fecha" is column index 1 in records
        for idx, record in enumerate(records):
            row_idx = idx + 2
            date_str = str(record.get("Fecha", "")).strip()
            
            # Check if older than retention_days
            try:
                # Try parsing "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"
                if " " in date_str:
                    log_date = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                else:
                    log_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
                if log_date < cutoff_date:
                    rows_to_delete.append(row_idx)
                    continue
            except Exception:
                pass
                
        # Now check max rows limit among the non-deleted records
        remaining_records_count = len(records) - len(rows_to_delete)
        if remaining_records_count > max_rows:
            extra_to_delete = remaining_records_count - max_rows
            # Find records that are NOT already in rows_to_delete
            already_deleted_set = set(rows_to_delete)
            added = 0
            for idx in range(len(records)):
                row_idx = idx + 2
                if row_idx not in already_deleted_set:
                    rows_to_delete.append(row_idx)
                    added += 1
                    if added >= extra_to_delete:
                        break
                        
        # Delete rows. Use batch_update to run all deletions in a single write call.
        deleted_count = len(rows_to_delete)
        if rows_to_delete:
            reqs = []
            for r_idx in sorted(rows_to_delete, reverse=True):
                reqs.append({
                    "deleteDimension": {
                        "range": {
                            "sheetId": worksheet.id,
                            "dimension": "ROWS",
                            "startIndex": r_idx - 1,
                            "endIndex": r_idx
                        }
                    }
                })
            spreadsheet.batch_update({"requests": reqs})
                
        # Get new count
        new_count = len(worksheet.get_all_records())
        return {
            "deleted_count": deleted_count,
            "remaining_count": new_count,
            "message": f"Se eliminaron {deleted_count} logs antiguos. Quedan {new_count} logs."
        }

    @staticmethod
    def is_row_real(row: dict) -> bool:
        """
        Determines if a row in Reseñas por publicar or Reseñas publicadas contains real review data.
        """
        target_fields = [
            "URL", "URL normalizada", "Título del artículo", 
            "Título del libro", "Autor del libro", "ISBN", 
            "Resumen", "Hash deduplicación", 
            "Título para Web", "Autor para Web",
            "Título del libro detectado por IA", "Autor del libro detectado por IA",
            "WordPress ID", "WordPress URL", "Fecha publicación", "Fecha de publicación"
        ]
        for field in target_fields:
            if str(row.get(field, "")).strip():
                return True
        return False

    def cleanup_empty_publication_rows(self, sheet_id: str, worksheet_names: List[str] = None) -> Dict[str, int]:
        """
        Clears cells for rows in specified worksheets (default: ['Reseñas por publicar', 'Reseñas publicadas'])
        that do not contain real review data, preserving checkbox validation formats.
        Converts text/string boolean values ('FALSE' / 'TRUE') to real checkboxes in real rows.
        """
        if not worksheet_names:
            worksheet_names = ["Reseñas por publicar", "Reseñas publicadas"]
            
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        
        results = {}
        for ws_name in worksheet_names:
            try:
                worksheet = spreadsheet.worksheet(ws_name)
            except Exception:
                continue
                
            records = worksheet.get_all_records()
            if not records:
                results[ws_name] = 0
                continue

            # Get header length
            headers = worksheet.row_values(1)
            num_cols = len(headers) if headers else 27

            # Fetch column A unformatted values to check if they are string "FALSE" / "TRUE"
            try:
                col_a_range = f"A2:A{len(records) + 1}"
                col_a_data = worksheet.get(col_a_range, value_render_option="UNFORMATTED_VALUE")
            except Exception:
                col_a_data = []

            reqs = []
            cleaned_count = 0
            for idx, row in enumerate(records):
                row_idx = idx + 2  # 1-indexed, headers is row 1
                
                # Safe retrieval from col_a_data
                raw_a_val = None
                if idx < len(col_a_data) and col_a_data[idx]:
                    raw_a_val = col_a_data[idx][0]

                if not self.is_row_real(row):
                    # Only clear if it contains any non-empty cell value (to minimize API overhead)
                    has_any_val = any(str(v).strip() for v in row.values())
                    if has_any_val:
                        reqs.append({
                            "updateCells": {
                                "range": {
                                    "sheetId": worksheet.id,
                                    "startRowIndex": row_idx - 1,
                                    "endRowIndex": row_idx,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": num_cols
                                },
                                "fields": "userEnteredValue"
                            }
                        })
                        cleaned_count += 1
                else:
                    # Real row: check if Column A is a string representation of FALSE or TRUE
                    # and convert to real bool values.
                    if isinstance(raw_a_val, str):
                        val_clean = raw_a_val.strip().upper()
                        if val_clean in ("FALSE", "'FALSE", "FALSE "):
                            reqs.append({
                                "updateCells": {
                                    "range": {
                                        "sheetId": worksheet.id,
                                        "startRowIndex": row_idx - 1,
                                        "endRowIndex": row_idx,
                                        "startColumnIndex": 0,
                                        "endColumnIndex": 1
                                    },
                                    "rows": [{
                                        "values": [{
                                            "userEnteredValue": {
                                                "boolValue": False
                                            }
                                        }]
                                    }],
                                    "fields": "userEnteredValue"
                                }
                            })
                        elif val_clean in ("TRUE", "'TRUE", "TRUE "):
                            reqs.append({
                                "updateCells": {
                                    "range": {
                                        "sheetId": worksheet.id,
                                        "startRowIndex": row_idx - 1,
                                        "endRowIndex": row_idx,
                                        "startColumnIndex": 0,
                                        "endColumnIndex": 1
                                    },
                                    "rows": [{
                                        "values": [{
                                            "userEnteredValue": {
                                                "boolValue": True
                                            }
                                        }]
                                    }],
                                    "fields": "userEnteredValue"
                                }
                            })

            if reqs:
                spreadsheet.batch_update({"requests": reqs})

            results[ws_name] = cleaned_count
            
        return results

    def add_published_reviews(self, sheet_id: str, rows_data: List[List[Any]]):
        """
        Adds multiple published reviews to Reseñas publicadas, reusing empty/false rows if they exist,
        otherwise appending them.
        """
        if not rows_data:
            return
            
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Reseñas publicadas")
        
        # Clean placeholders from rows_data: "Titulo Web", "Autor Web"
        def clean_val(val: Any) -> Any:
            if isinstance(val, str):
                s = val.strip()
                if s.lower() in ("titulo web", "título web", "autor web", "autor web "):
                    return ""
                return s
            return val
        rows_data = [[clean_val(item) for item in row] for row in rows_data]
        
        # Read existing records to find empty/false row indices
        records = worksheet.get_all_records()
        
        # Find all row indices that are not real (1-based, headers is row 1)
        empty_row_indices = []
        for idx, row in enumerate(records):
            if not self.is_row_real(row):
                empty_row_indices.append(idx + 2)
                
        # We will write to these empty row indices first
        # Any remaining rows in rows_data will be appended
        rows_to_append = []
        batch_data = []
        empty_idx_cursor = 0
        
        for row_val in rows_data:
            if empty_idx_cursor < len(empty_row_indices):
                target_row = empty_row_indices[empty_idx_cursor]
                empty_idx_cursor += 1
                batch_data.append({
                    "range": f"'Reseñas publicadas'!A{target_row}",
                    "values": [row_val]
                })
            else:
                rows_to_append.append(row_val)
                
        if batch_data:
            spreadsheet.values_batch_update({
                "valueInputOption": "USER_ENTERED",
                "data": batch_data
            })
            
        if rows_to_append:
            worksheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")

    def cleanup_descartes(self, sheet_id: str, max_rows: int = 1000, retention_days: int = 30) -> Dict[str, Any]:
        """
        Cleans up old descartes in the 'Descartes' worksheet based on retention days and max row limit.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Descartes")
        records = worksheet.get_all_records()
        if not records:
            return {"deleted_count": 0, "remaining_count": 0, "message": "La hoja de Descartes está vacía."}
            
        now = get_now_madrid()
        cutoff_date = now - datetime.timedelta(days=retention_days)
        
        rows_to_delete = []
        
        # Parse date column: "Fecha de extracción" is the date column in Descartes
        for idx, record in enumerate(records):
            row_idx = idx + 2
            date_str = str(record.get("Fecha de extracción", "")).strip()
            
            # Check if older than retention_days
            try:
                if " " in date_str:
                    log_date = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                else:
                    log_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
                if log_date < cutoff_date:
                    rows_to_delete.append(row_idx)
                    continue
            except Exception:
                pass
                
        # Limit rows among the non-deleted records
        remaining_records_count = len(records) - len(rows_to_delete)
        if remaining_records_count > max_rows:
            extra_to_delete = remaining_records_count - max_rows
            already_deleted_set = set(rows_to_delete)
            added = 0
            for idx in range(len(records)):
                row_idx = idx + 2
                if row_idx not in already_deleted_set:
                    rows_to_delete.append(row_idx)
                    added += 1
                    if added >= extra_to_delete:
                        break
                        
        deleted_count = len(rows_to_delete)
        if rows_to_delete:
            reqs = []
            for r_idx in sorted(rows_to_delete, reverse=True):
                reqs.append({
                    "deleteDimension": {
                        "range": {
                            "sheetId": worksheet.id,
                            "dimension": "ROWS",
                            "startIndex": r_idx - 1,
                            "endIndex": r_idx
                        }
                    }
                })
            spreadsheet.batch_update({"requests": reqs})
                
        new_count = len(worksheet.get_all_records())
        return {
            "deleted_count": deleted_count,
            "remaining_count": new_count,
            "message": f"Se eliminaron {deleted_count} filas antiguas de Descartes. Quedan {new_count} filas."
        }

sheets_service = SheetsService()
