import logging
import datetime
from app.services.sheets_service import sheets_service

class LoggerService:
    def __init__(self):
        self.logger = logging.getLogger("encuentro-noticias")
        self.logger.setLevel(logging.INFO)
        
        # Verify if handlers are already set to prevent duplicate logs in Uvicorn environment
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(logging.INFO)
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def log(self, level: str, action: str, message: str, isbn: str = "", detail: str = "", sheet_id: str = "", run_id: str = ""):
        """
        Logs a message to the console and to the Google Sheets 'Logs' tab if sheet_id and run_id are set.
        """
        level_upper = level.upper()
        log_text = f"[{action}]"
        if isbn:
            log_text += f" [ISBN: {isbn}]"
        log_text += f" {message}"
        if detail:
            log_text += f" (Detail: {detail})"

        # Output to terminal/file
        if level_upper == "ERROR":
            self.logger.error(log_text)
        elif level_upper == "WARNING":
            self.logger.warning(log_text)
        elif level_upper == "DEBUG":
            self.logger.debug(log_text)
        else:
            self.logger.info(log_text)

        # Write to Google Sheet 'Logs' tab
        if sheet_id and run_id:
            try:
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # Format: Run ID, Fecha, Nivel, ISBN, Acción, Mensaje, Detalle
                log_row = [run_id, timestamp, level_upper, isbn, action, message, detail]
                sheets_service.add_log(sheet_id, log_row)
            except Exception as e:
                self.logger.error(f"Failed to append log row to Google Sheets: {e}")

logger_service = LoggerService()
