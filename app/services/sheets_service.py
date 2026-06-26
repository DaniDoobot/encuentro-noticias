import gspread
from google.oauth2.service_account import Credentials
from app.config import settings
from typing import List, Dict, Any, Optional, Tuple
import datetime

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

        # Tab schemas definition
        tabs = {
            "Libros": [
                "ISBN", "Título del libro", "Autor del libro", 
                "Estado", "Última ejecución", "Reseñas encontradas", "Observaciones"
            ],
            "Reseñas": [
                "ISBN", "Título del libro", "Autor del libro", "Query", "URL", 
                "URL normalizada", "Título del artículo", "Título del libro detectado por IA", 
                "Autor del libro detectado por IA", "Medio de publicación", "Autor de la publicación", 
                "Fecha de publicación", "Idioma original", "Categoría", "Resumen", 
                "Score de coincidencia", "Tipo de contenido", "Fecha de extracción", 
                "Hash deduplicación", "Estado"
            ],
            "Descartes": [
                "ISBN", "Título del libro", "Autor del libro", "Query", "URL", 
                "Título detectado", "Motivo de descarte", "Score de coincidencia", "Fecha de extracción"
            ],
            "Logs": [
                "Run ID", "Fecha", "Nivel", "ISBN", "Acción", "Mensaje", "Detalle"
            ],
            "Config": [
                "Clave", "Valor", "Descripción"
            ]
        }

        created_tabs = []
        for tab_name, headers in tabs.items():
            try:
                worksheet = spreadsheet.worksheet(tab_name)
            except gspread.exceptions.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(title=tab_name, rows="1000", cols=str(len(headers) + 5))
                created_tabs.append(tab_name)

            # Ensure headers are correct
            existing_headers = worksheet.row_values(1)
            if not existing_headers or len(existing_headers) < len(headers):
                worksheet.insert_row(headers, index=1)
            else:
                # Fill missing headers if any
                for i, header in enumerate(headers):
                    if i >= len(existing_headers) or existing_headers[i] != header:
                        worksheet.update_cell(1, i + 1, header)

        # Initialize Config defaults if empty
        config_ws = spreadsheet.worksheet("Config")
        config_rows = config_ws.get_all_records()
        existing_keys = {row["Clave"] for row in config_rows if "Clave" in row}
        
        default_configs = [
            {"Clave": "MAX_BOOKS_PER_RUN", "Valor": str(settings.MAX_BOOKS_PER_RUN), "Descripción": "Cantidad máxima de libros a procesar por ejecución"},
            {"Clave": "MAX_SEARCH_PAGES_PER_QUERY", "Valor": str(settings.MAX_SEARCH_PAGES_PER_QUERY), "Descripción": "Páginas máximas del buscador a escanear por query"},
            {"Clave": "MAX_CANDIDATES_PER_BOOK", "Valor": str(settings.MAX_CANDIDATES_PER_BOOK), "Descripción": "Cantidad máxima de URLs candidatas a evaluar por libro"},
            {"Clave": "MIN_MATCH_SCORE", "Valor": str(settings.MIN_MATCH_SCORE), "Descripción": "Score mínimo de validación de OpenAI para aceptar una reseña (0-100)"},
            {"Clave": "OPENAI_MODEL", "Valor": settings.OPENAI_MODEL, "Descripción": "Modelo de OpenAI a usar para análisis"},
            {"Clave": "REVIEW_DOMAINS", "Valor": "revistadelibros.com,nueva-revista.net,aceprensa.com,elcultural.com,zendalibros.com,babelia.elpais.com", "Descripción": "Dominios culturales/literarios recomendados para búsquedas específicas (separados por coma)"},
            {"Clave": "SEARCH_DELAY_SECONDS", "Valor": str(settings.SEARCH_DELAY_SECONDS), "Descripción": "Espera en segundos entre cada búsqueda para evitar bloqueos"},
            {"Clave": "SEARCH_BACKOFF_SECONDS", "Valor": str(settings.SEARCH_BACKOFF_SECONDS), "Descripción": "Espera de enfriamiento en segundos si se detecta rate limit o error"},
            {"Clave": "MAX_QUERIES_PER_BOOK", "Valor": str(settings.MAX_QUERIES_PER_BOOK), "Descripción": "Límite máximo de búsquedas por libro"},
            {"Clave": "ENABLE_GOOGLE_NEWS_RSS", "Valor": str(settings.ENABLE_GOOGLE_NEWS_RSS).lower(), "Descripción": "Activar búsqueda complementaria mediante Google News RSS (true/false)"}
        ]

        for config in default_configs:
            if config["Clave"] not in existing_keys:
                config_ws.append_row([config["Clave"], config["Valor"], config["Descripción"]])

        return {
            "success": True,
            "sheet_id": sheet_id,
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}",
            "created_tabs": created_tabs
        }

    def get_config_dict(self, sheet_id: str) -> Dict[str, Any]:
        """
        Reads configurations from Config tab, falls back to env settings.
        """
        client = self.get_client()
        try:
            spreadsheet = client.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet("Config")
            records = worksheet.get_all_records()
            
            config_dict = {}
            for r in records:
                key = r.get("Clave")
                val = r.get("Valor")
                if key and val is not None:
                    config_dict[key] = val

            return {
                "MAX_BOOKS_PER_RUN": int(config_dict.get("MAX_BOOKS_PER_RUN", settings.MAX_BOOKS_PER_RUN)),
                "MAX_SEARCH_PAGES_PER_QUERY": int(config_dict.get("MAX_SEARCH_PAGES_PER_QUERY", settings.MAX_SEARCH_PAGES_PER_QUERY)),
                "MAX_CANDIDATES_PER_BOOK": int(config_dict.get("MAX_CANDIDATES_PER_BOOK", settings.MAX_CANDIDATES_PER_BOOK)),
                "MIN_MATCH_SCORE": int(config_dict.get("MIN_MATCH_SCORE", settings.MIN_MATCH_SCORE)),
                "OPENAI_MODEL": config_dict.get("OPENAI_MODEL", settings.OPENAI_MODEL),
                "REVIEW_DOMAINS": config_dict.get("REVIEW_DOMAINS", ""),
                "SEARCH_DELAY_SECONDS": float(config_dict.get("SEARCH_DELAY_SECONDS", settings.SEARCH_DELAY_SECONDS)),
                "SEARCH_BACKOFF_SECONDS": float(config_dict.get("SEARCH_BACKOFF_SECONDS", settings.SEARCH_BACKOFF_SECONDS)),
                "MAX_QUERIES_PER_BOOK": int(config_dict.get("MAX_QUERIES_PER_BOOK", settings.MAX_QUERIES_PER_BOOK)),
                "ENABLE_GOOGLE_NEWS_RSS": str(config_dict.get("ENABLE_GOOGLE_NEWS_RSS", settings.ENABLE_GOOGLE_NEWS_RSS)).lower() == "true"
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
                "ENABLE_GOOGLE_NEWS_RSS": settings.ENABLE_GOOGLE_NEWS_RSS
            }

    def get_pending_books(self, sheet_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Reads Libros tab. Treats a row as pending if it has ISBN, title, and author,
        and its status is empty or 'pendiente'.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Libros")
        records = worksheet.get_all_records()
        
        pending_books = []
        for index, row in enumerate(records, start=2): # Headers are row 1, records start at row 2
            isbn = str(row.get("ISBN", "")).strip()
            title = str(row.get("Título del libro", "")).strip()
            author = str(row.get("Autor del libro", "")).strip()
            status = str(row.get("Estado", "")).strip().lower()

            if isbn and title and author:
                if status in ("", "pendiente", "none"):
                    pending_books.append({
                        "row_index": index,
                        "isbn": isbn,
                        "title": title,
                        "author": author,
                        "status": "pendiente"
                    })
                    if len(pending_books) >= limit:
                        break
        return pending_books

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
        Uses a range update to do this in a single API call.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Libros")
        
        # Cols mapping:
        # Col D (4): Estado
        # Col E (5): Última ejecución
        # Col F (6): Reseñas encontradas
        # Col G (7): Observaciones
        range_name = f"D{row_index}:G{row_index}"
        values = [[status, last_run, reviews_found, observations]]
        worksheet.update(range_name, values)

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
                    # Treat unexpected status as pendiente or error? Treat as pendiente if empty, otherwise count it as error/other
                    counts["pendiente"] += 1

        counts["total"] = total
        return counts

    def get_all_reviews(self, sheet_id: str) -> List[Dict[str, Any]]:
        """
        Reads all items from Reseñas.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Reseñas")
        return worksheet.get_all_records()

    def add_review(self, sheet_id: str, review_data: List[Any]):
        """
        Appends a row to Reseñas.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Reseñas")
        worksheet.append_row(review_data)

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
        Appends a row to Logs.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Logs")
        worksheet.append_row(log_data)

    def update_reviews_hashes(self, sheet_id: str, updates: List[Tuple[int, str]]):
        """
        Updates the deduplication hash for multiple rows.
        updates: list of (row_index, hash_value)
        We write these row-by-row or using range if they are contiguous, but row-by-row for sparse updates.
        Col S (19) is the 'Hash deduplicación' column.
        """
        client = self.get_client()
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet("Reseñas")
        for row_idx, hash_val in updates:
            worksheet.update_cell(row_idx, 19, hash_val)

sheets_service = SheetsService()
