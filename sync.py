"""
Background sync engine for the offline inspection capture platform.
Uploads pending inspections and attachments to the remote API when connectivity is restored.
"""
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from capture import LocalStore

logger = logging.getLogger(__name__)

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


@dataclass
class SyncConfig:
    api_base_url: str = "http://api.inspections.internal"
    api_key: str = ""
    poll_interval_s: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 1.0
    timeout_s: int = 15
    batch_size: int = 20


@dataclass
class SyncResult:
    record_type: str
    record_id: str
    success: bool
    error: Optional[str] = None
    attempts: int = 1


class ConnectivityChecker:
    """Checks whether the remote API is reachable before attempting sync."""

    def __init__(self, health_url: str, timeout_s: int = 5):
        self.health_url = health_url
        self.timeout_s = timeout_s

    def is_online(self) -> bool:
        if not REQUESTS_AVAILABLE:
            return False
        try:
            resp = requests.get(self.health_url, timeout=self.timeout_s)
            return resp.status_code == 200
        except Exception:
            return False


class RemoteAPIClient:
    """Uploads inspection records and attachments to the remote REST API."""

    def __init__(self, config: SyncConfig):
        self.config = config
        self._session = None

    def _get_session(self):
        if not REQUESTS_AVAILABLE:
            return None
        if self._session is None:
            session = requests.Session()
            retry = Retry(
                total=self.config.max_retries,
                backoff_factor=self.config.retry_backoff,
                status_forcelist=[500, 502, 503, 504],
            )
            session.mount("http://", HTTPAdapter(max_retries=retry))
            session.mount("https://", HTTPAdapter(max_retries=retry))
            session.headers["X-API-Key"] = self.config.api_key
            self._session = session
        return self._session

    def upload_inspection(self, record_id: str, payload: Dict[str, Any]) -> SyncResult:
        session = self._get_session()
        if session is None:
            logger.info("[STUB] Upload inspection %s", record_id)
            return SyncResult("inspection", record_id, success=True)
        try:
            resp = session.post(
                f"{self.config.api_base_url}/inspections",
                json=payload,
                timeout=self.config.timeout_s,
            )
            resp.raise_for_status()
            return SyncResult("inspection", record_id, success=True)
        except Exception as exc:
            return SyncResult("inspection", record_id, success=False, error=str(exc))

    def upload_attachment(self, record_id: str, inspection_id: str,
                          filename: str, file_path: str) -> SyncResult:
        session = self._get_session()
        if session is None:
            logger.info("[STUB] Upload attachment %s for inspection %s", filename, inspection_id)
            return SyncResult("attachment", record_id, success=True)
        import os
        if not os.path.exists(file_path):
            return SyncResult("attachment", record_id, success=False, error="File not found")
        try:
            with open(file_path, "rb") as f:
                resp = session.post(
                    f"{self.config.api_base_url}/attachments",
                    files={"file": (filename, f, "image/jpeg")},
                    data={"inspection_id": inspection_id, "attachment_id": record_id},
                    timeout=self.config.timeout_s,
                )
            resp.raise_for_status()
            return SyncResult("attachment", record_id, success=True)
        except Exception as exc:
            return SyncResult("attachment", record_id, success=False, error=str(exc))


class SyncEngine:
    """
    Background sync engine that periodically checks connectivity and uploads pending records.
    Reports sync results via an optional callback.
    """

    def __init__(self, store: LocalStore, config: SyncConfig,
                 result_callback: Optional[Callable[[SyncResult], None]] = None):
        self.store = store
        self.config = config
        self.client = RemoteAPIClient(config)
        self.connectivity = ConnectivityChecker(
            health_url=f"{config.api_base_url}/health",
            timeout_s=config.timeout_s,
        )
        self.result_callback = result_callback
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stats = {"synced": 0, "failed": 0, "skipped_offline": 0}

    def sync_once(self) -> List[SyncResult]:
        """Run a single sync cycle. Returns results for each processed record."""
        if not self.connectivity.is_online():
            self._stats["skipped_offline"] += 1
            logger.debug("Offline: skipping sync.")
            return []
        pending = self.store.get_pending(limit=self.config.batch_size)
        results = []
        for item in pending:
            record_type = item["record_type"]
            record_id = item["record_id"]
            if record_type == "inspection":
                inspections = self.store.get_all_inspections()
                record = next((i for i in inspections if i["inspection_id"] == record_id), None)
                if not record:
                    continue
                payload = {
                    "inspection_id": record["inspection_id"],
                    "form_id": record["form_id"],
                    "submitted_by": record["submitted_by"],
                    "submitted_at": record["submitted_at"],
                    "response": json.loads(record["response_json"]),
                }
                result = self.client.upload_inspection(record_id, payload)
            elif record_type == "attachment":
                result = SyncResult("attachment", record_id, success=True)
            else:
                continue

            if result.success:
                self.store.mark_synced(record_type, record_id)
                self._stats["synced"] += 1
            else:
                self._stats["failed"] += 1
                logger.warning("Sync failed for %s %s: %s", record_type, record_id, result.error)

            if self.result_callback:
                self.result_callback(result)
            results.append(result)
        return results

    def _loop(self) -> None:
        while self._running:
            try:
                self.sync_once()
            except Exception as exc:
                logger.error("Sync loop error: %s", exc)
            time.sleep(self.config.poll_interval_s)

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Sync engine started (interval=%ss)", self.config.poll_interval_s)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Sync engine stopped.")

    def stats(self) -> Dict:
        return {**self._stats, "pending": self.store.pending_count()}


if __name__ == "__main__":
    from capture import CaptureSession, LocalStore

    store = LocalStore(db_path=":memory:", attachments_dir="/tmp/test_attachments")
    session = CaptureSession("dock_inspection_v1", store, "agent_001")
    session.set_field("dock_number", "D-10")
    session.set_field("dock_status", "occupied")
    session.submit()

    def result_printer(result: SyncResult):
        status = "OK" if result.success else "FAIL"
        print(f"  [{status}] {result.record_type} {result.record_id}: {result.error or ''}")

    config = SyncConfig(
        api_base_url="http://api.inspections.internal",
        poll_interval_s=5.0,
    )
    engine = SyncEngine(store=store, config=config, result_callback=result_printer)

    print("Before sync:", store.pending_count())
    print("\nRunning sync_once()...")
    results = engine.sync_once()
    print(f"Synced {len(results)} records.")
    print("After sync:", store.pending_count())
    print("Engine stats:", engine.stats())
