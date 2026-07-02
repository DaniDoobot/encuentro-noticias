import gspread
import threading
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
        "URL", "Título del artículo", "Título del libro", 
        "Autor del libro", "ISBN", "Resumen", "Hash deduplicación", 
        "Título para Web", "Autor para Web", "Título del libro detectado por IA", 
        "Autor del libro detectado por IA", "WordPress ID", "WordPress URL", 
        "Fecha publicación", "Fecha de publicación"
    ]
    for field in target_fields:
        if str(row.get(field, "")).strip():
            return True
    return False

def clean_sheet_value(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("'"):
            return value[1:]
    elif value is None:
        return ""
    return value

def clean_row_values(row: List[Any]) -> List[Any]:
    return [clean_sheet_value(val) for val in row]

DEFAULT_DOMAIN_CONFIGS = {
    "revistadelibros.com": {
        "sitemap_url": "https://www.revistadelibros.com/sitemap.xml",
        "rss_url": "https://www.revistadelibros.com/feed/",
        "buscador_interno": "https://www.revistadelibros.com/?s={query}"
    },
    "nueva-revista.net": {
        "sitemap_url": "https://www.nueva-revista.net/sitemap.xml",
        "rss_url": "https://www.nueva-revista.net/feed/",
        "buscador_interno": "https://www.nueva-revista.net/?s={query}"
    },
    "aceprensa.com": {
        "sitemap_url": "",
        "rss_url": "https://www.aceprensa.com/feed/",
        "buscador_interno": ""
    },
    "zendalibros.com": {
        "sitemap_url": "https://www.zendalibros.com/sitemap.xml",
        "rss_url": "https://www.zendalibros.com/feed/",
        "buscador_interno": "https://www.zendalibros.com/?s={query}"
    },
    "religionenlibertad.com": {
        "sitemap_url": "",
        "rss_url": "https://www.religionenlibertad.com/rss/",
        "buscador_interno": "https://www.religionenlibertad.com/buscar/{query}"
    },
    "aciprensa.com": {
        "sitemap_url": "",
        "rss_url": "https://www.aciprensa.com/rss/",
        "buscador_interno": "https://www.aciprensa.com/buscar/{query}"
    }
}

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
    if s.startswith("www."):
        s = s[4:]
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
    if s_val in ("true", "1", "yes", "sí", "si", "verdadero"):
        return True
    if s_val in ("false", "0", "no", "falso"):
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
        self.old_modo_prueba = None
        self._log_lock = threading.Lock()

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
        panel_recreated = False
        modo_prueba_added = False
        modo_prueba_value = "FALSE"

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
                "Tipo de contenido", "Fecha de extracción", "Hash deduplicación", "Query"
            ],
            "Reseñas publicadas": [
                "Fecha publicación", "WordPress ID", "WordPress URL", "ISBN", "Título del libro",
                "Autor del libro", "URL", "Título para Web", "Autor para Web",
                "Medio de publicación", "Fecha de publicación",
                "Idioma original", "Categoría", "Resumen", "Score de coincidencia",
                "Tipo de contenido", "Fecha de extracción", "Hash deduplicación", "Query"
            ],
            "Descartes": [
                "ISBN", "Título del libro", "Autor del libro", "Query", "URL", 
                "Título detectado", "Motivo de descarte", "Score de coincidencia", "Fecha de extracción"
            ],
            "Fuentes": [
                "Dominio", "Activo", "Tipo", "Notas", "Última indexación", "URLs indexadas", "Errores"
            ],
            "Logs": [
                "Fecha", "Nivel", "Acción", "ISBN", "Mensaje", "Detalle", "Run ID"
            ],
            "Config": [
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
                worksheet.insert_row(clean_row_values(headers), index=1)
                continue

            # Ensure headers are correct and run safe migrator if order/columns mismatch
            if tab_name in ("Libros", "Reseñas por publicar", "Reseñas publicadas", "Descartes", "Fuentes"):
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
                                    
                            if tab_name in ("Reseñas por publicar", "Reseñas publicadas") and not is_row_real(row_dict):
                                continue
                                
                            new_row = []
                            for h in headers:
                                if h == "¿Incluir en búsqueda?" and tab_name == "Libros":
                                    title_val = row_dict.get("Título del libro", "").strip()
                                    new_row.append(True if title_val else "")
                                elif h == "Título para Web" and tab_name in ("Reseñas por publicar", "Reseñas publicadas"):
                                    title_web_val = row_dict.get("Título para Web", "")
                                    if not title_web_val or str(title_web_val).strip().lower() in ("titulo web", "título web"):
                                        title_web_val = row_dict.get("Título del artículo") or row_dict.get("Título del libro detectado por IA") or ""
                                    new_row.append(title_web_val)
                                elif h == "Autor para Web" and tab_name in ("Reseñas por publicar", "Reseñas publicadas"):
                                    author_web_val = row_dict.get("Autor para Web", "")
                                    if not author_web_val or str(author_web_val).strip().lower() in ("autor web", "autor web "):
                                        author_web_val = row_dict.get("Autor de la publicación") or row_dict.get("Autor del libro detectado por IA") or ""
                                    new_row.append(author_web_val)
                                elif h == "¿Publicar?" and tab_name == "Reseñas por publicar":
                                    new_row.append(row_dict.get("¿Publicar?", False))
                                else:
                                    new_row.append(row_dict.get(h, ""))
                            new_rows.append(new_row)
                            
                        # Overwrite sheet with new schema and reordered values
                        worksheet.clear()
                        worksheet.resize(rows=max(1000, len(new_rows) + 50), cols=len(headers) + 5)
                        cleaned_rows = [clean_row_values(r) for r in new_rows]
                        worksheet.update("A1", [headers] + cleaned_rows)
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


        # Ensure Logs worksheet structure is correct/migrated
        try:
            self.ensure_logs_sheet_structure(sheet_id)
        except Exception as e_logs_struct:
            logger.error(f"Error ensuring Logs structure during ensure_sheet: {e_logs_struct}")

        # Ensure Panel worksheet exists and has the correct layout
        is_old_layout = False
        old_inputs = {
            "date_min": "2024-01-01",
            "date_max": "2026-12-31",
            "limit_books": 10,
            "include_unknown": True
        }

        if "Panel" in existing_sheets:
            try:
                panel_ws = existing_sheets["Panel"]
                panel_vals = panel_ws.get_all_values()
                if panel_vals:
                    # Detect old panel signals
                    has_old_header = False
                    if len(panel_vals) >= 1 and len(panel_vals[0]) >= 1:
                        if "Encuentro Noticias — Panel de control" in panel_vals[0][0] and "búsqueda y subida" not in panel_vals[0][0]:
                            has_old_header = True
                    
                    has_old_fecha_min = False
                    if len(panel_vals) >= 3 and len(panel_vals[2]) >= 1:
                        if panel_vals[2][0] == "Fecha mínima":
                            has_old_fecha_min = True

                    has_old_modo_prueba = False
                    if len(panel_vals) >= 6 and len(panel_vals[5]) >= 1:
                        if "Modo prueba" in panel_vals[5][0]:
                            has_old_modo_prueba = True

                    has_new_layout = False
                    if len(panel_vals) >= 3 and len(panel_vals[2]) >= 2:
                        if panel_vals[2][1] == "Filtros de búsqueda":
                            has_new_layout = True

                    if has_old_header or has_old_fecha_min or has_old_modo_prueba or not has_new_layout or len(panel_vals) < 17:
                        is_old_layout = True
                        logger.info("PANEL_MIGRATION_DETECTED: Old layout detected in Panel tab.")
                        
                        # Extract old values to migrate
                        if len(panel_vals) >= 3 and len(panel_vals[2]) >= 2:
                            old_inputs["date_min"] = panel_vals[2][1]
                        if len(panel_vals) >= 4 and len(panel_vals[3]) >= 2:
                            old_inputs["date_max"] = panel_vals[3][1]
                        if len(panel_vals) >= 5 and len(panel_vals[4]) >= 2:
                            try:
                                old_inputs["limit_books"] = int(panel_vals[4][1])
                            except Exception:
                                pass
                        if len(panel_vals) >= 6 and len(panel_vals[5]) >= 2:
                            b6_val = str(panel_vals[5][1]).strip().upper()
                            if b6_val in ("TRUE", "VERDADERO", "SÍ", "SI", "1"):
                                self.old_modo_prueba = "TRUE"
                            elif b6_val in ("FALSE", "FALSO", "NO", "0"):
                                self.old_modo_prueba = "FALSE"
                        if len(panel_vals) >= 7 and len(panel_vals[6]) >= 2:
                            old_b7 = str(panel_vals[6][1]).strip().upper()
                            old_inputs["include_unknown"] = old_b7 in ("TRUE", "VERDADERO", "SÍ", "SI", "1")
            except Exception as e_detect:
                logger.error(f"Error checking old Panel for migration: {e_detect}")
        else:
            is_old_layout = True

        if "Panel" not in existing_sheets or is_old_layout:
            panel_recreated = True
            logger.info("PANEL_MIGRATION_DETECTED: Panel worksheet needs recreation.")
            
            # Delete old worksheet if present
            if "Panel" in existing_sheets:
                try:
                    spreadsheet.del_worksheet(existing_sheets["Panel"])
                    logger.info("PANEL_RECREATED: Old Panel deleted successfully.")
                except Exception as e_del:
                    logger.error(f"Error deleting old Panel worksheet: {e_del}")

            # Recreate from Panel prueba if available
            if "Panel prueba" in existing_sheets:
                try:
                    panel_ws = spreadsheet.duplicate_sheet(
                        existing_sheets["Panel prueba"].id,
                        insert_sheet_index=0,
                        new_sheet_name="Panel"
                    )
                    logger.info("PANEL_RECREATED: Duplicated Panel prueba as Panel.")
                except Exception as e_dup:
                    logger.error(f"Error duplicating Panel prueba, creating clean tab instead: {e_dup}")
                    panel_ws = spreadsheet.add_worksheet(title="Panel", rows="30", cols="10")
                    try:
                        spreadsheet.batch_update({
                            "requests": [{
                                "updateSheetProperties": {
                                    "properties": {
                                        "sheetId": panel_ws.id,
                                        "index": 0
                                    },
                                    "fields": "index"
                                }
                            }]
                        })
                    except Exception:
                        pass
            else:
                panel_ws = spreadsheet.add_worksheet(title="Panel", rows="30", cols="10")
                try:
                    spreadsheet.batch_update({
                        "requests": [{
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": panel_ws.id,
                                    "index": 0
                                },
                                "fields": "index"
                            }
                        }]
                    })
                except Exception:
                    pass

            # Populate structure and values
            panel_structure = [
                ["", "Panel de control de búsqueda y subida a web automática de reseñas", "", "", "", ""],
                ["", "", "", "", "", ""],
                ["", "Filtros de búsqueda", "", "", "Información última ejecución", ""],
                ["", "Fecha mínima", old_inputs["date_min"], "", "Estado", ""],
                ["", "Fecha máxima", old_inputs["date_max"], "", "ID", ""],
                ["", "Máximo de libros", old_inputs["limit_books"], "", "Fecha", ""],
                ["", "Incluir artículos sin fecha", old_inputs["include_unknown"], "", "Mensaje", ""],
                ["", "", "", "", "", ""],
                ["", "", "", "", "", ""],
                ["", "Resumen operativo", "", "", "", ""],
                ["", "Ejecución inactiva", "", "", "", ""],
                ["", "", "", "", "", ""],
                ["", "", "", "", "", ""],
                ["", "Instrucciones", "", "", "", ""],
                ["", "1. Usa el botón “Lanzar búsqueda” del menú “Encuentro Noticias” para iniciar una ejecución.", "", "", "", ""],
                ["", "2. Usa “Consultar estado” para refrescar el estado de la última ejecución.", "", "", "", ""],
                ["", "3. Revisa el resumen operativo para saber si el proceso está publicando, completado o con error.", "", "", "", ""]
            ]
            panel_ws.clear()
            cleaned_panel_structure = [clean_row_values(row) for row in panel_structure]
            panel_ws.update(range_name="A1", values=cleaned_panel_structure)
        else:
            panel_ws = existing_sheets["Panel"]
            
        # Apply validations
        libros_ws = spreadsheet.worksheet("Libros")
        try:
            ws_to_pub = spreadsheet.worksheet("Reseñas por publicar")
            ws_pub = spreadsheet.worksheet("Reseñas publicadas")
            
            val_requests = [
                # FULL SANITIZATION: Clear validation on entire Panel sheet to avoid corrupt rules
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": panel_ws.id,
                            "startRowIndex": 0,
                            "endRowIndex": 30,
                            "startColumnIndex": 0,
                            "endColumnIndex": 10
                        }
                    }
                },
                # Date validation for C4:C5
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": panel_ws.id,
                            "startRowIndex": 3,
                            "endRowIndex": 5,
                            "startColumnIndex": 2,
                            "endColumnIndex": 3
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
                # Date format (yyyy-mm-dd) for C4:C5
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": panel_ws.id,
                            "startRowIndex": 3,
                            "endRowIndex": 5,
                            "startColumnIndex": 2,
                            "endColumnIndex": 3
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
                # Number validation for C6 (Máximo de libros): integer 1-5000
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": panel_ws.id,
                            "startRowIndex": 5,
                            "endRowIndex": 6,
                            "startColumnIndex": 2,
                            "endColumnIndex": 3
                        },
                        "rule": {
                            "condition": {
                                "type": "NUMBER_BETWEEN",
                                "values": [
                                    {"userEnteredValue": "1"},
                                    {"userEnteredValue": "5000"}
                                ]
                            },
                            "showCustomUi": True,
                            "strict": False
                        }
                    }
                },
                # Boolean checkbox for C7 (Incluir artículos sin fecha)
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": panel_ws.id,
                            "startRowIndex": 6,
                            "endRowIndex": 7,
                            "startColumnIndex": 2,
                            "endColumnIndex": 3
                        },
                        "rule": {
                            "condition": {
                                "type": "BOOLEAN"
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
        
        # Read old technical configurations if they exist for migration
        tech_records = []
        try:
            tech_ws = spreadsheet.worksheet("Config técnica")
            tech_records = tech_ws.get_all_records()
        except gspread.exceptions.WorksheetNotFound:
            pass
            
        allowed_migration_keys = {
            "MAX_SEARCH_PAGES_PER_QUERY", "REVIEW_DOMAINS", "SEARCH_DELAY_SECONDS",
            "SEARCH_BACKOFF_SECONDS", "MAX_QUERIES_PER_BOOK", "GOOGLE_NEWS_BROAD_MAX_QUERIES",
            "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES", "MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH",
            "MIN_CANDIDATES_BEFORE_AI", "ENABLE_CASCADE_SEARCH",
            "ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS", "ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH",
            "LOG_RETENTION_DAYS", "DESCARTES_RETENTION_DAYS",
            "BACKEND_BASE_URL"
        }
        
        # Migrate allowed technical keys to existing_basic
        for r in tech_records:
            k = r.get("Clave")
            v = r.get("Valor")
            d = r.get("Descripción", "")
            if k and k in allowed_migration_keys:
                if k not in existing_basic:
                    existing_basic[k] = {"Clave": k, "Valor": v, "Descripción": d}

        # Remove WORDPRESS_POST_STATUS from config if present
        if "WORDPRESS_POST_STATUS" in existing_basic:
            del existing_basic["WORDPRESS_POST_STATUS"]

        # Determine values for config MODO_PRUEBA
        if "MODO_PRUEBA" in existing_basic:
            modo_prueba_value = str(existing_basic["MODO_PRUEBA"]["Valor"]).strip().upper()
        elif self.old_modo_prueba is not None:
            existing_basic["MODO_PRUEBA"] = {
                "Clave": "MODO_PRUEBA",
                "Valor": self.old_modo_prueba,
                "Descripción": "Si está activado, las búsquedas/publicaciones se simulan sin ejecutar acciones reales."
            }
            modo_prueba_added = True
            modo_prueba_value = self.old_modo_prueba
            logger.info(f"MODO_PRUEBA_MIGRATED: Migrated Modo prueba from Panel to Config: {self.old_modo_prueba}")
        else:
            modo_prueba_added = True
            modo_prueba_value = "FALSE"
            logger.info("MODO_PRUEBA_DEFAULT_ADDED: Added default MODO_PRUEBA = FALSE to Config.")

        basic_defaults = [
            {"Clave": "BACKEND_BASE_URL", "Valor": "https://encuentro-backend.doobot.ai", "Descripción": "URL base del backend usada por el menú de Google Sheets para lanzar procesos"},
            {"Clave": "MODO_PRUEBA", "Valor": "FALSE", "Descripción": "Si está activado, las búsquedas/publicaciones se simulan sin ejecutar acciones reales."},
            {"Clave": "MAX_BOOKS_PER_RUN", "Valor": settings.MAX_BOOKS_PER_RUN, "Descripción": "Cantidad máxima de libros a procesar por ejecución"},
            {"Clave": "MAX_CANDIDATES_PER_BOOK", "Valor": settings.MAX_CANDIDATES_PER_BOOK, "Descripción": "Cantidad máxima de URLs candidatas a evaluar por libro"},
            {"Clave": "MIN_MATCH_SCORE", "Valor": 1, "Descripción": "Score mínimo de validación de OpenAI para aceptar una reseña (0-100)"},
            {"Clave": "OPENAI_MODEL", "Valor": settings.OPENAI_MODEL, "Descripción": "Modelo de OpenAI a usar para análisis"},
            {"Clave": "LOG_MAX_ROWS", "Valor": settings.LOG_MAX_ROWS, "Descripción": "Cantidad máxima de filas a mantener en la pestaña Logs"},
            {"Clave": "DESCARTES_MAX_ROWS", "Valor": getattr(settings, "DESCARTES_MAX_ROWS", 1000), "Descripción": "Cantidad máxima de descartes a mantener en la pestaña Descartes"},
            {"Clave": "MAX_SEARCH_PAGES_PER_QUERY", "Valor": settings.MAX_SEARCH_PAGES_PER_QUERY, "Descripción": "Páginas máximas del buscador a escanear por query"},
            {"Clave": "REVIEW_DOMAINS", "Valor": "revistadelibros.com,nueva-revista.net,aceprensa.com,elcultural.com,zendalibros.com,babelia.elpais.com", "Descripción": "Dominios culturales/literarios recomendados para búsquedas específicas (separados por coma)"},
            {"Clave": "SEARCH_DELAY_SECONDS", "Valor": settings.SEARCH_DELAY_SECONDS, "Descripción": "Espera en segundos entre cada búsqueda para evitar bloqueos"},
            {"Clave": "SEARCH_BACKOFF_SECONDS", "Valor": settings.SEARCH_BACKOFF_SECONDS, "Descripción": "Espera de enfriamiento en segundos si se detecta rate limit o error"},
            {"Clave": "MAX_QUERIES_PER_BOOK", "Valor": getattr(settings, "MAX_QUERIES_PER_BOOK", 12), "Descripción": "Límite máximo de queries de búsqueda permitidas por libro"},
            {"Clave": "GOOGLE_NEWS_BROAD_MAX_QUERIES", "Valor": 10, "Descripción": "Límite de queries para la búsqueda amplia en Google News si la normal da 0 candidatos"},
            {"Clave": "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES", "Valor": 10, "Descripción": "Límite de queries adicionales a realizar en dominios activos"},
            {"Clave": "MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH", "Valor": getattr(settings, "MIN_CANDIDATES_BEFORE_INTERNAL_SEARCH", 5), "Descripción": "Mínimo de candidatos requeridos antes de activar la búsqueda interna profunda"},
            {"Clave": "MIN_CANDIDATES_BEFORE_AI", "Valor": getattr(settings, "MIN_CANDIDATES_BEFORE_AI", 1), "Descripción": "Mínimo de candidatos requeridos para ejecutar el análisis IA de OpenAI"},
            {"Clave": "ENABLE_CASCADE_SEARCH", "Valor": getattr(settings, "ENABLE_CASCADE_SEARCH", True), "Descripción": "Activar búsqueda en cascada (Domain Index -> RSS -> Búsqueda Interna)"},
            {"Clave": "ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS", "Valor": getattr(settings, "ENABLE_DEEP_INTERNAL_SEARCH_ON_LOW_RESULTS", True), "Descripción": "Activar búsqueda interna si el total de candidatos es bajo"},
            {"Clave": "ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH", "Valor": getattr(settings, "ALWAYS_RUN_INTERNAL_DOMAIN_SEARCH", True), "Descripción": "Ejecutar siempre búsqueda interna en dominios activos (true=siempre, false=solo si pocos candidatos)"},
            {"Clave": "LOG_RETENTION_DAYS", "Valor": settings.LOG_RETENTION_DAYS, "Descripción": "Días de retención de logs en la pestaña Logs"},
            {"Clave": "DESCARTES_RETENTION_DAYS", "Valor": getattr(settings, "DESCARTES_RETENTION_DAYS", 30), "Descripción": "Días de retención de descartes en la pestaña Descartes"}
        ]

        # Re-write Config keeping only allowed basic keys
        config_ws.clear()
        config_ws.append_row(["Clave", "Valor", "Descripción"])
        for b in basic_defaults:
            k = b["Clave"]
            val = existing_basic[k]["Valor"] if k in existing_basic else b["Valor"]
            desc = existing_basic[k]["Descripción"] if k in existing_basic else b["Descripción"]
            config_ws.append_row(clean_row_values([k, val, desc]), value_input_option="USER_ENTERED")

        # Physically delete the Config técnica worksheet if it was present
        try:
            tech_ws = spreadsheet.worksheet("Config técnica")
            spreadsheet.del_worksheet(tech_ws)
            logger.info("Deleted old Config técnica sheet.")
        except gspread.exceptions.WorksheetNotFound:
            pass

        # Check and update existing technical limits in Config if they are too low
        try:
            from app.services.logger_service import logger_service
            config_all = config_ws.get_all_values()
            if config_all:
                config_headers = config_all[0]
                if "Clave" in config_headers and "Valor" in config_headers:
                    col_clave = config_headers.index("Clave") + 1
                    col_valor = config_headers.index("Valor") + 1
                    for r_idx, row in enumerate(config_all[1:], start=2):
                        if col_clave - 1 < len(row) and col_valor - 1 < len(row):
                            clave = row[col_clave - 1]
                            valor_str = row[col_valor - 1]
                            try:
                                valor_int = int(valor_str)
                            except ValueError:
                                continue
                            clave_clean = str(clave).strip()
                            if clave_clean == "MAX_QUERIES_PER_BOOK" and valor_int < 12:
                                config_ws.update_cell(r_idx, col_valor, 12)
                                logger.info(f"Updated MAX_QUERIES_PER_BOOK from {valor_int} to 12 in Config")
                                logger_service.log("WARNING", "CONFIG_LOW_QUERY_LIMIT_WARNING", f"Advertencia: MAX_QUERIES_PER_BOOK tiene un límite bajo de {valor_int}", sheet_id=sheet_id)
                                logger_service.log("INFO", "CONFIG_QUERY_LIMITS_AUTO_UPDATED", f"Límites de consulta actualizados automáticamente: MAX_QUERIES_PER_BOOK: {valor_int} -> 12", sheet_id=sheet_id)
                            if clave_clean == "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES" and valor_int < 10:
                                config_ws.update_cell(r_idx, col_valor, 10)
                                logger.info(f"Updated DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES from {valor_int} to 10 in Config")
                                logger_service.log("WARNING", "CONFIG_LOW_QUERY_LIMIT_WARNING", f"Advertencia: DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES tiene un límite bajo de {valor_int}", sheet_id=sheet_id)
                                logger_service.log("INFO", "CONFIG_QUERY_LIMITS_AUTO_UPDATED", f"Límites de consulta actualizados automáticamente: DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES: {valor_int} -> 10", sheet_id=sheet_id)
        except Exception as e_upd:
            logger.warning(f"Error checking/updating low query limits in Config: {e_upd}")

        # Initialise Fuentes tab with default domains append-only
        try:
            appended = self.append_default_sources(sheet_id)
            if appended > 0:
                logger.info(f"ensure_sheet: Appended {appended} default sources.")
        except Exception as e_fuentes:
            logger.warning(f"Error appending default sources in ensure_sheet: {e_fuentes}")

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
            "created_tabs": created_tabs,
            "panel_recreated": panel_recreated,
            "modo_prueba_added": modo_prueba_added,
            "modo_prueba_value": modo_prueba_value
        }

    def get_config_dict(self, sheet_id: str) -> Dict[str, Any]:
        """
        Reads configurations from Config sheet, falls back to env settings.
        """
        client = self.get_client()
        try:
            spreadsheet = client.open_by_key(sheet_id)
            
            # Read config keys from Config sheet
            config_dict = {}
            try:
                config_ws = spreadsheet.worksheet("Config")
                config_records = config_ws.get_all_records()
                for r in config_records:
                    key = r.get("Clave")
                    val = r.get("Valor")
                    if key and val is not None:
                        config_dict[str(key).strip()] = val
            except Exception as e_user:
                logger.warning(f"Could not read Config tab: {e_user}")

            logger.info(f"CONFIG_LOADED: read {len(config_dict)} keys from Config")

            return {
                "MODO_PRUEBA": parse_bool(config_dict.get("MODO_PRUEBA"), False),
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
                "DOMAIN_INDEX_MIN_SCORE": parse_int(config_dict.get("DOMAIN_INDEX_MIN_SCORE"), settings.DOMAIN_INDEX_MIN_SCORE),
                "DOMAIN_INDEX_DB_PATH": parse_str(config_dict.get("DOMAIN_INDEX_DB_PATH"), settings.DOMAIN_INDEX_DB_PATH),
                "DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES": parse_int(config_dict.get("DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES"), settings.DOMAIN_INDEX_NEWS_COMPLEMENT_MAX_QUERIES),
                "GOOGLE_NEWS_BROAD_MAX_QUERIES": parse_int(config_dict.get("GOOGLE_NEWS_BROAD_MAX_QUERIES"), 10),
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
                "MODO_PRUEBA": False,
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
                "GOOGLE_NEWS_BROAD_MAX_QUERIES": 10,
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
            
        full_row = clean_row_values(full_row)
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
        worksheet.append_row(clean_row_values(descarte_data))

    def add_log(self, sheet_id: str, log_data: List[Any]):
        """
        Appends a single row to Logs. Use add_log_batch for multiple rows.
        """
        self.add_log_batch(sheet_id, [log_data])

    def add_log_batch(self, sheet_id: str, log_rows: List[List[Any]]) -> Dict[str, Any]:
        """
        Appends multiple rows to Logs in a single API call (reduces quota usage).
        Calculates the next row using column A, and writes to a fixed range A:G.
        """
        if not log_rows:
            return {"success": True, "range": "", "rows_written": 0}

        cleaned_log_rows = [clean_row_values(r) for r in log_rows]
        logger.info(f"[LOG_BATCH_WRITE_ATTEMPT] Attempting to write batch of {len(cleaned_log_rows)} logs to sheet_id={sheet_id}")

        with self._log_lock:
            def perform_write():
                self.ensure_logs_sheet_structure(sheet_id)
                client = self.get_client()
                spreadsheet = client.open_by_key(sheet_id)
                worksheet = spreadsheet.worksheet("Logs")
                
                col_a_vals = worksheet.col_values(1)
                start_row = len(col_a_vals) + 1
                end_row = start_row + len(cleaned_log_rows) - 1
                
                # Check row count and resize if necessary
                try:
                    current_rows = int(worksheet.row_count)
                except Exception:
                    current_rows = 1000  # Fallback for MagicMocks in unit tests
                    
                if end_row > current_rows:
                    margen_seguridad = max(100, len(cleaned_log_rows))
                    new_rows_count = end_row + margen_seguridad
                    logger.info(f"[LOG_SHEET_RESIZE_BEFORE_WRITE] Resizing Logs worksheet from {current_rows} to {new_rows_count} rows")
                    worksheet.resize(rows=new_rows_count, cols=7)
                    # Refresh worksheet after resize before writing as required by condition 1
                    worksheet = spreadsheet.worksheet("Logs")
                    
                write_range = f"A{start_row}:G{end_row}"
                worksheet.update(write_range, cleaned_log_rows)
                return write_range

            try:
                write_range = perform_write()
                logger.info(f"[LOG_BATCH_WRITE_SUCCESS] Successfully wrote batch of {len(cleaned_log_rows)} logs to range {write_range}")
                return {
                    "success": True,
                    "range": write_range,
                    "rows_written": len(log_rows)
                }
            except gspread.exceptions.APIError as e:
                err_msg = str(e)
                if "exceeds grid limits" in err_msg.lower() or "grid_limits" in err_msg.lower():
                    logger.warning(f"[LOG_BATCH_WRITE_RETRY_AFTER_GRID_LIMIT] APIError limits exceeded during write. Attempting resize retry. Error: {e}")
                    try:
                        # Retry flow: fetch latest, resize and retry
                        client = self.get_client()
                        spreadsheet = client.open_by_key(sheet_id)
                        worksheet = spreadsheet.worksheet("Logs")
                        col_a_vals = worksheet.col_values(1)
                        start_row = len(col_a_vals) + 1
                        end_row = start_row + len(cleaned_log_rows) - 1
                        
                        try:
                            current_rows = int(worksheet.row_count)
                        except Exception:
                            current_rows = 1000
                            
                        margen_seguridad = max(100, len(cleaned_log_rows))
                        new_rows_count = max(end_row + margen_seguridad, current_rows + margen_seguridad)
                        logger.info(f"[LOG_SHEET_RESIZE_BEFORE_WRITE] Retry resizing Logs worksheet from {current_rows} to {new_rows_count} rows")
                        worksheet.resize(rows=new_rows_count, cols=7)
                        # Refresh worksheet after resize before writing
                        worksheet = spreadsheet.worksheet("Logs")
                        
                        write_range = f"A{start_row}:G{end_row}"
                        worksheet.update(write_range, cleaned_log_rows)
                        logger.info(f"[LOG_BATCH_WRITE_SUCCESS] Successfully wrote batch of {len(cleaned_log_rows)} logs after retry to range {write_range}")
                        return {
                            "success": True,
                            "range": write_range,
                            "rows_written": len(log_rows)
                        }
                    except Exception as retry_err:
                        logger.error(f"[LOG_BATCH_WRITE_FAILED] Failed to batch-write logs on retry: {retry_err}")
                        return {
                            "success": False,
                            "error": str(retry_err),
                            "range": "",
                            "rows_written": 0
                        }
                elif "10000000" in err_msg or "above the limit" in err_msg or "cell" in err_msg.lower():
                    logger.warning(f"[LOG_BATCH_WRITE_RETRY_AFTER_CELL_LIMIT] Cell limit reached. Compact sheet and retry. Error: {e}")
                    try:
                        self.compact_sheet(sheet_id)
                        write_range = perform_write()
                        return {
                            "success": True,
                            "range": write_range,
                            "rows_written": len(log_rows)
                        }
                    except Exception as retry_err:
                        logger.error(f"[LOG_BATCH_WRITE_FAILED] Failed to batch-write logs after cell limit compaction retry: {retry_err}")
                        return {
                            "success": False,
                            "error": str(retry_err),
                            "range": "",
                            "rows_written": 0
                        }
                else:
                    logger.error(f"[LOG_BATCH_WRITE_FAILED] Failed to batch-write logs: {e}")
                    return {
                        "success": False,
                        "error": str(e),
                        "range": "",
                        "rows_written": 0
                    }
            except Exception as e:
                logger.error(f"[LOG_BATCH_WRITE_FAILED] Failed to batch-write logs due to unexpected error: {e}")
                return {
                    "success": False,
                    "error": str(e),
                    "range": "",
                    "rows_written": 0
                }

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
            hardcoded = DEFAULT_DOMAIN_CONFIGS.get(domain, {})
            sitemap_url = str(row.get("Sitemap URL", "")).strip() or hardcoded.get("sitemap_url", "")
            rss_url = str(row.get("RSS URL", "")).strip() or hardcoded.get("rss_url", "")
            buscador_interno = str(row.get("Buscador interno", "")).strip() or hardcoded.get("buscador_interno", "")

            sources.append({
                "domain": domain,
                "active": True,
                "tipo": str(row.get("Tipo", "cultural")).strip(),
                "sitemap_url": sitemap_url,
                "rss_url": rss_url,
                "buscador_interno": buscador_interno,
                "row_index": i,
            })
        return sources

    def update_source_index_status(
        self,
        sheet_id: str,
        domain: str,
        last_indexed: str,
        urls_indexed: int,
        errors: Any,
    ) -> None:
        """
        Updates Última indexación, URLs indexadas, Errores columns for a domain in Fuentes tab.
        Columns: H=Última indexación, I=URLs indexadas, J=Errores
        """
        from app.services.logger_service import logger_service
        import json

        # Log SOURCE_SHEET_UPDATE_STARTED
        logger_service.log(
            level="INFO",
            action="SOURCE_SHEET_UPDATE_STARTED",
            message=f"Iniciando actualización de hoja Fuentes para el dominio: {domain}",
            sheet_id=sheet_id,
            detail=json.dumps({
                "domain": domain,
                "last_indexed": last_indexed,
                "urls_indexed": urls_indexed,
                "errors": errors
            })
        )

        try:
            if isinstance(errors, list):
                errors_str = ", ".join(str(e) for e in errors)
            else:
                errors_str = str(errors or "")

            client = self.get_client()
            spreadsheet = client.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet("Fuentes")
            records = worksheet.get_all_records()

            updated = False
            for i, row in enumerate(records, start=2):
                row_dom_raw = str(row.get("Dominio", "")).strip()
                if clean_domain_string(row_dom_raw) == clean_domain_string(domain):
                    worksheet.update(f"E{i}:G{i}", [clean_row_values([last_indexed, urls_indexed, errors_str])])
                    updated = True

                    # Log SOURCE_SHEET_UPDATE_ROW
                    logger_service.log(
                        level="INFO",
                        action="SOURCE_SHEET_UPDATE_ROW",
                        message=f"Fila {i} actualizada en la pestaña Fuentes para el dominio {domain}",
                        sheet_id=sheet_id,
                        detail=json.dumps({
                            "domain": domain,
                            "row": i,
                            "last_indexed": last_indexed,
                            "urls_indexed": urls_indexed,
                            "errors_count": len(errors) if isinstance(errors, list) else (1 if errors else 0)
                        })
                    )
                    break

            if not updated:
                logger_service.log(
                    level="WARNING",
                    action="SOURCE_SHEET_UPDATE_FAILED",
                    message=f"No se encontró el dominio {domain} en la pestaña Fuentes",
                    sheet_id=sheet_id
                )
            else:
                # Log SOURCE_SHEET_UPDATE_COMPLETED
                logger_service.log(
                    level="INFO",
                    action="SOURCE_SHEET_UPDATE_COMPLETED",
                    message=f"Actualización completada para el dominio {domain}",
                    sheet_id=sheet_id
                )

        except Exception as e:
            # Log SOURCE_SHEET_UPDATE_FAILED
            logger_service.log(
                level="ERROR",
                action="SOURCE_SHEET_UPDATE_FAILED",
                message=f"Error actualizando el dominio {domain} en la pestaña Fuentes: {str(e)}",
                sheet_id=sheet_id
            )
            import logging
            logging.getLogger("encuentro-noticias").warning(f"update_source_index_status error: {e}")

    def sync_sources_status(self, sheet_id: str) -> Dict[str, Any]:
        """
        Reads all domain statuses from SQLite and updates the entire Fuentes sheet in a single batch.
        """
        from app.services.logger_service import logger_service
        from app.services.cache_service import cache_service
        from app.config import settings
        import json

        # Ensure SQLite database is initialized
        cache_service.init_db(settings.DOMAIN_INDEX_DB_PATH)

        try:
            total_urls = cache_service.get_total_urls()
            db_statuses = cache_service.get_all_domain_statuses()
            db_statuses_map = {clean_domain_string(s["domain"]): s for s in db_statuses}

            db_stats = cache_service.get_all_domains_stats()
            db_stats_map = {clean_domain_string(d["domain"]): d for d in db_stats}

            client = self.get_client()
            spreadsheet = client.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet("Fuentes")
            records = worksheet.get_all_records()

            cells_to_update = []
            updated_count = 0

            for i, row in enumerate(records, start=2):
                row_dom_raw = str(row.get("Dominio", "")).strip()
                if not row_dom_raw:
                    continue
                dom_clean = clean_domain_string(row_dom_raw)

                # Retrieve stats
                status = db_statuses_map.get(dom_clean, {})
                stats_grp = db_stats_map.get(dom_clean, {})

                urls = status.get("urls_count")
                if urls is None:
                    urls = stats_grp.get("cnt", 0)

                last_indexed = status.get("last_indexed")
                if not last_indexed:
                    last_indexed = stats_grp.get("last_indexed", "")

                errors = status.get("errors_count", 0)
                last_error = status.get("last_error", "")

                # Format errors string
                errors_str = ""
                if last_error:
                    errors_str = last_error
                elif errors > 0:
                    errors_str = f"{errors} errores"

                # We update the row if we have either index date or urls
                if last_indexed or urls > 0:
                    # Column E (5): Última indexación
                    cells_to_update.append(gspread.Cell(row=i, col=5, value=clean_sheet_value(last_indexed)))
                    # Column F (6): URLs indexadas
                    cells_to_update.append(gspread.Cell(row=i, col=6, value=clean_sheet_value(urls)))
                    # Column G (7): Errores
                    cells_to_update.append(gspread.Cell(row=i, col=7, value=clean_sheet_value(errors_str)))

                    updated_count += 1

                    # Log SOURCE_SHEET_UPDATE_ROW
                    logger_service.log(
                        level="INFO",
                        action="SOURCE_SHEET_UPDATE_ROW",
                        message=f"Preparada actualización para fila {i} en la pestaña Fuentes para el dominio {row_dom_raw}",
                        sheet_id=sheet_id,
                        detail=json.dumps({
                            "domain": row_dom_raw,
                            "row": i,
                            "last_indexed": last_indexed,
                            "urls_indexed": urls,
                            "errors_count": errors
                        })
                    )

            if cells_to_update:
                worksheet.update_cells(cells_to_update, value_input_option='USER_ENTERED')

            return {
                "success": True,
                "sources_updated": updated_count,
                "total_urls": total_urls
            }

        except Exception as e:
            import logging
            logging.getLogger("encuentro-noticias").warning(f"sync_sources_status error: {e}")
            raise e

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
        rows_data = [clean_row_values([clean_val(item) for item in row]) for row in rows_data]
        
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

    def get_sheet_size_report(self, sheet_id: str) -> Dict[str, Any]:
        """
        Audits the size of the Google Sheet, returning a report detailing row/col/cell counts,
        last populated data row/col, and excess cells for all worksheets.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        
        expected_headers_map = {
            "Libros": [
                "¿Incluir en búsqueda?", "ISBN", "Título del libro", "Autor del libro", 
                "Estado", "Última ejecución", "Reseñas encontradas", "Observaciones"
            ],
            "Reseñas por publicar": [
                "¿Publicar?", "Estado publicación", "Fecha intento publicación", "Error publicación", "ISBN", "Título del libro",
                "Autor del libro", "URL", "Título para Web", "Autor para Web",
                "Medio de publicación", "Fecha de publicación",
                "Idioma original", "Categoría", "Resumen", "Score de coincidencia",
                "Tipo de contenido", "Fecha de extracción", "Hash deduplicación", "Query"
            ],
            "Reseñas publicadas": [
                "Fecha publicación", "WordPress ID", "WordPress URL", "ISBN", "Título del libro",
                "Autor del libro", "URL", "Título para Web", "Autor para Web",
                "Medio de publicación", "Fecha de publicación",
                "Idioma original", "Categoría", "Resumen", "Score de coincidencia",
                "Tipo de contenido", "Fecha de extracción", "Hash deduplicación", "Query"
            ],
            "Descartes": [
                "ISBN", "Título del libro", "Autor del libro", "Query", "URL", 
                "Título detectado", "Motivo de descarte", "Score de coincidencia", "Fecha de extracción"
            ],
            "Fuentes": [
                "Dominio", "Activo", "Tipo", "Notas", "Última indexación", "URLs indexadas", "Errores"
            ],
            "Logs": [
                "Fecha", "Nivel", "Acción", "ISBN", "Mensaje", "Detalle", "Run ID"
            ],
            "Config": [
                "Clave", "Valor", "Descripción"
            ],
            "Panel": [
                "Encuentro Noticias — Panel de control", ""
            ]
        }
        
        tabs_to_audit = list(expected_headers_map.keys())
        
        tab_reports = []
        spreadsheet_total_cells = 0
        
        ROW_BUFFER = 100
        min_rows_map = {
            "Logs": max(500, settings.LOG_MAX_ROWS + 200),
            "Descartes": 200,
            "Reseñas por publicar": 200,
            "Reseñas publicadas": 200,
            "Fuentes": 200,
            "Libros": 200,
            "Config": 50,
            "Config técnica": 50,
            "Panel": 50
        }
        
        for ws_name in tabs_to_audit:
            try:
                worksheet = spreadsheet.worksheet(ws_name)
            except Exception:
                continue
                
            current_rows = worksheet.row_count
            current_cols = worksheet.col_count
            current_cells = current_rows * current_cols
            spreadsheet_total_cells += current_cells
            
            # Fetch values
            values = worksheet.get_all_values()
            
            last_data_row = len(values)
            last_data_col = 0
            if values:
                # Find the maximum index of populated columns in any row
                for row in values:
                    # filter out trailing empty values in row to find real col count
                    real_row_len = len(row)
                    while real_row_len > 0 and str(row[real_row_len - 1]).strip() == "":
                        real_row_len -= 1
                    if real_row_len > last_data_col:
                        last_data_col = real_row_len
                        
            # Map expected headers length
            expected_headers = expected_headers_map[ws_name]
            header_len = len(expected_headers)
            
            # Recommended rows and columns
            min_rows = min_rows_map.get(ws_name, 100)
            recommended_rows = max(last_data_row + ROW_BUFFER, min_rows)
            recommended_cols = max(last_data_col, header_len)
            
            # Excess calculation
            excess_rows = max(current_rows - recommended_rows, 0)
            excess_cols = max(current_cols - recommended_cols, 0)
            
            tab_reports.append({
                "title": ws_name,
                "rows": current_rows,
                "cols": current_cols,
                "cells": current_cells,
                "last_data_row": last_data_row,
                "last_data_col": last_data_col,
                "recommended_rows": recommended_rows,
                "recommended_cols": recommended_cols,
                "excess_rows": excess_rows,
                "excess_cols": excess_cols
            })
            
        return {
            "success": True,
            "spreadsheet_total_cells": spreadsheet_total_cells,
            "tabs": tab_reports
        }

    def compact_sheet(self, sheet_id: str, dry_run: bool = False) -> Dict[str, Any]:
        """
        Compacts the Google Sheet by shrinking tabs with excess rows or columns.
        """
        report = self.get_sheet_size_report(sheet_id)
        if not report.get("success"):
            return report
            
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        
        compacted_tabs = []
        total_cells_freed = 0
        
        for tab in report["tabs"]:
            ws_name = tab["title"]
            current_rows = tab["rows"]
            current_cols = tab["cols"]
            target_rows = min(current_rows, tab["recommended_rows"])
            target_cols = min(current_cols, tab["recommended_cols"])
            
            # We only compact if the target is smaller than current
            if target_rows < current_rows or target_cols < current_cols:
                try:
                    worksheet = spreadsheet.worksheet(ws_name)
                    
                    if not dry_run:
                        if ws_name == "Logs":
                            try:
                                self.ensure_logs_sheet_structure(sheet_id)
                            except Exception as e_logs_struct:
                                logger.error(f"Error ensuring Logs structure during compact: {e_logs_struct}")
                        worksheet.resize(rows=target_rows, cols=target_cols)
                        
                    cells_before = current_rows * current_cols
                    cells_after = target_rows * target_cols
                    cells_freed = max(cells_before - cells_after, 0)
                    total_cells_freed += cells_freed
                    
                    compacted_tabs.append({
                        "title": ws_name,
                        "old_rows": current_rows,
                        "old_cols": current_cols,
                        "new_rows": target_rows,
                        "new_cols": target_cols,
                        "cells_freed": cells_freed
                    })
                except Exception as e:
                    logger.warning(f"Failed to compact tab '{ws_name}': {e}")
                    
        return {
            "success": True,
            "dry_run": dry_run,
            "total_cells_freed": total_cells_freed,
            "compacted_tabs": compacted_tabs,
            "report_before": report
        }

    def ensure_logs_sheet_structure(self, sheet_id: str):
        """
        Ensures the Logs worksheet has the correct header order and columns count (exactly 7).
        If old structure exists with data, migrates old rows:
          old: [Run ID, Fecha, Nivel, ISBN, Acción, Mensaje, Detalle]
          new: [Fecha, Nivel, Acción, ISBN, Mensaje, Detalle, Run ID]
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        
        new_headers = ["Fecha", "Nivel", "Acción", "ISBN", "Mensaje", "Detalle", "Run ID"]
        
        try:
            worksheet = spreadsheet.worksheet("Logs")
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title="Logs", rows="1000", cols="7")
            worksheet.update("A1", [new_headers])
            return

        # Read all rows
        all_rows = worksheet.get_all_values()
        if not all_rows:
            worksheet.resize(rows=1000, cols=7)
            worksheet.update("A1", [new_headers])
            return
            
        current_headers = all_rows[0]
        data_rows = all_rows[1:] if len(all_rows) > 1 else []
        
        # If headers are already correct, just ensure columns count is 7
        if current_headers == new_headers:
            if worksheet.col_count != 7:
                worksheet.resize(rows=max(1000, len(all_rows) + 50), cols=7)
            return
            
        # Map values to dictionary if possible
        new_data_rows = []
        for row in data_rows:
            row_dict = {}
            for idx, header in enumerate(current_headers):
                if idx < len(row):
                    row_dict[header] = row[idx]
            
            # Map old columns to new
            run_id = row_dict.get("Run ID", row[0] if len(row) > 0 else "")
            fecha = row_dict.get("Fecha", row[1] if len(row) > 1 else "")
            nivel = row_dict.get("Nivel", row[2] if len(row) > 2 else "")
            isbn = row_dict.get("ISBN", row[3] if len(row) > 3 else "")
            accion = row_dict.get("Acción", row[4] if len(row) > 4 else "")
            mensaje = row_dict.get("Mensaje", row[5] if len(row) > 5 else "")
            detalle = row_dict.get("Detalle", row[6] if len(row) > 6 else "")
            
            new_data_rows.append([fecha, nivel, accion, isbn, mensaje, detalle, run_id])
            
        # Clear and overwrite
        worksheet.clear()
        worksheet.resize(rows=max(1000, len(new_data_rows) + 100), cols=7)
        cleaned_headers = clean_row_values(new_headers)
        cleaned_data_rows = [clean_row_values(r) for r in new_data_rows]
        worksheet.update("A1", [cleaned_headers] + cleaned_data_rows)

    def append_default_sources(self, sheet_id: str) -> int:
        """
        Appends default sources (cultural, religious, press) to the Fuentes tab,
        ensuring no duplicates are created based on clean domain name match.
        Returns the number of appended sources.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        fuentes_ws = spreadsheet.worksheet("Fuentes")
        
        # Read existing sources to get a set of already present domains
        records = fuentes_ws.get_all_records()
        existing_domains = set()
        for r in records:
            dom = str(r.get("Dominio") or "").strip().lower()
            if dom:
                existing_domains.add(clean_domain_string(dom))
                
        # Recommended list:
        # Columns in sheet: Dominio, Activo, Tipo, Sitemap URL, RSS URL, Buscador interno, Notas, Última indexación, URLs indexadas, Errores
        recommended = [
            ("revistadelibros.com", "true", "cultural", "https://www.revistadelibros.com/sitemap.xml", "https://www.revistadelibros.com/feed/", "https://www.revistadelibros.com/?s={query}", "Revista de Libros"),
            ("nueva-revista.net", "true", "cultural", "https://www.nueva-revista.net/sitemap.xml", "https://www.nueva-revista.net/feed/", "https://www.nueva-revista.net/?s={query}", "Nueva Revista"),
            ("aceprensa.com", "true", "cultural", "", "https://www.aceprensa.com/feed/", "", "Aceprensa"),
            ("elcultural.com", "true", "cultural", "", "", "", "El Cultural"),
            ("zendalibros.com", "true", "cultural", "https://www.zendalibros.com/sitemap.xml", "https://www.zendalibros.com/feed/", "https://www.zendalibros.com/?s={query}", "Zenda Libros"),
            ("wmagazin.com", "true", "cultural", "", "", "", "WMagazín"),
            ("theobjective.com", "true", "prensa", "", "", "", "The Objective"),
            ("ethic.es", "true", "cultural", "", "", "", "Ethic"),
            ("eldebate.com", "true", "prensa", "", "", "", "El Debate"),
            ("larazon.es", "true", "prensa", "", "", "", "La Razón"),
            ("abc.es", "true", "prensa", "", "", "", "ABC"),
            ("elespanol.com", "true", "prensa", "", "", "", "El Español"),
            ("librujula.com", "true", "cultural", "", "", "", "Librujula"),
            ("todostuslibros.com", "true", "libros", "", "", "", "Todos tus libros"),
            ("todoliteratura.es", "true", "libros", "", "", "", "Todo Literatura"),
            ("alfayomega.es", "true", "religión", "", "", "", "Alfa y Omega"),
            ("religionenlibertad.com", "true", "religión", "", "https://www.religionenlibertad.com/rss/", "https://www.religionenlibertad.com/buscar/{query}", "Religión en Libertad"),
            ("aciprensa.com", "true", "religión", "", "https://www.aciprensa.com/rss/", "https://www.aciprensa.com/buscar/{query}", "ACI Prensa"),
            ("es.aleteia.org", "true", "religión", "", "", "", "Aleteia"),
            ("infocatolica.com", "true", "religión", "", "", "", "InfoCatólica"),
            ("revistaecclesia.es", "true", "religión", "", "", "", "Revista Ecclesia"),
            ("vidanuevadigital.com", "true", "religión", "", "", "", "Vida Nueva Digital"),
            ("omnesmag.com", "true", "religión", "", "", "", "Omnes Mag"),
            ("opusdei.org", "true", "religión", "", "", "", "Opus Dei"),
            ("clonline.org", "true", "religión", "", "", "", "Comunión y Liberación"),
            ("catholic.net", "true", "religión", "", "", "", "Catholic.net"),
            ("religiondigital.org", "true", "religión", "", "", "", "Religión Digital"),
            ("exaudi.org", "true", "religión", "", "", "", "Exaudi"),
            ("iglesia.cl", "true", "religión", "", "", "", "Iglesia.cl"),
            ("acncolombia.org", "true", "religión", "", "", "", "ACN Colombia"),
            ("romereports.com", "true", "religión", "", "", "", "Rome Reports"),
            ("elpais.com", "true", "prensa", "", "", "", "El País"),
            ("elmundo.es", "true", "prensa", "", "", "", "El Mundo"),
            ("lavanguardia.com", "true", "prensa", "", "", "", "La Vanguardia"),
            ("elconfidencial.com", "true", "prensa", "", "", "", "El Confidencial"),
            ("publico.es", "true", "prensa", "", "", "", "Público")
        ]
        
        to_append = []
        for dom_raw, active, tipo, sitemap, rss, buscador, notas in recommended:
            cleaned = clean_domain_string(dom_raw)
            if cleaned and cleaned not in existing_domains:
                row = clean_row_values([cleaned, active, tipo, notas, "", "", ""])
                to_append.append(row)
                existing_domains.add(cleaned)
                
        if to_append:
            fuentes_ws.append_rows(to_append, value_input_option="USER_ENTERED")
            
        return len(to_append)

sheets_service = SheetsService()
