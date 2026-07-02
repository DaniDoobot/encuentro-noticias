import logging
import datetime
import threading
from typing import List, Any, Optional
from app.services.sheets_service import sheets_service, get_now_madrid_str


class LoggerService:
    def __init__(self):
        self.logger = logging.getLogger("encuentro-noticias")
        self.logger.setLevel(logging.INFO)

        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(logging.INFO)
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

        # Batching state: keyed by (sheet_id, run_id)
        self._batch: List[List[Any]] = []
        self._batch_key: Optional[str] = None
        self._batch_lock = threading.Lock()
        self._batch_size = 15  # flush every N rows

    def log(
        self,
        level: str,
        action: str,
        message: str,
        isbn: str = "",
        detail: str = "",
        sheet_id: str = "",
        run_id: str = "",
    ):
        """
        Logs to console + batches rows for Google Sheets.
        Call flush_log_batch(sheet_id, run_id) at the end of each book to drain the buffer.
        """
        level_upper = level.upper()
        log_text = f"[{action}]"
        if isbn:
            log_text += f" [ISBN: {isbn}]"
        log_text += f" {message}"
        if detail:
            log_text += f" (Detail: {detail})"

        # Console output
        if level_upper == "ERROR":
            self.logger.error(log_text)
        elif level_upper == "WARNING":
            self.logger.warning(log_text)
        elif level_upper == "DEBUG":
            self.logger.debug(log_text)
        else:
            self.logger.info(log_text)

        # Batch for Sheets
        if sheet_id:
            timestamp = get_now_madrid_str()
            actual_run_id = run_id or ""
            log_row = [timestamp, level_upper, action, isbn, message, detail, actual_run_id]
            batch_key = sheet_id
            with self._batch_lock:
                if self._batch_key != batch_key:
                    # Flush previous batch if context switched
                    self._flush_locked(self._batch_key)
                    self._batch_key = batch_key
                    self._batch = []
                self._batch.append(log_row)
                if len(self._batch) >= self._batch_size:
                    self._flush_locked(batch_key)

    def flush_log_batch(self, sheet_id: str, run_id: str = ""):
        """Flush any remaining buffered log rows for this sheet to Google Sheets."""
        batch_key = sheet_id
        with self._batch_lock:
            self._flush_locked(batch_key)

    def _flush_locked(self, batch_key: Optional[str]):
        """Must be called while holding self._batch_lock."""
        if not batch_key or not self._batch or self._batch_key != batch_key:
            return
        rows = list(self._batch)
        self._batch = []
        try:
            res = sheets_service.add_log_batch(batch_key, rows)
            if isinstance(res, dict) and not res.get("success"):
                self.logger.error(f"Failed to batch-write log rows to Google Sheets: {res.get('error')}. Log rows: {rows}")
        except Exception as e:
            self.logger.error(f"Failed to batch-write log rows to Google Sheets: {e}. Log rows: {rows}")


logger_service = LoggerService()
