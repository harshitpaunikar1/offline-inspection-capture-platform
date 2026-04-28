"""
Offline inspection capture platform - local data capture module.
Stores form submissions and photos locally with queue for background sync.
"""
import hashlib
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    inspection_id TEXT PRIMARY KEY,
    form_id TEXT NOT NULL,
    submitted_by TEXT,
    submitted_at REAL,
    response_json TEXT,
    sync_status TEXT DEFAULT 'pending',
    synced_at REAL,
    checksum TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,
    inspection_id TEXT NOT NULL,
    filename TEXT,
    file_path TEXT,
    file_size INTEGER,
    mime_type TEXT,
    captured_at REAL,
    sync_status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    priority INTEGER DEFAULT 5,
    created_at REAL,
    attempts INTEGER DEFAULT 0,
    last_attempt_at REAL
);
"""


@dataclass
class InspectionRecord:
    inspection_id: str
    form_id: str
    submitted_by: str
    response: Dict[str, Any]
    submitted_at: float = field(default_factory=time.time)
    sync_status: str = "pending"

    def checksum(self) -> str:
        payload = json.dumps(self.response, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class AttachmentRecord:
    attachment_id: str
    inspection_id: str
    filename: str
    file_path: str
    file_size: int
    mime_type: str = "image/jpeg"
    captured_at: float = field(default_factory=time.time)
    sync_status: str = "pending"


class LocalStore:
    """SQLite-backed local store for offline inspection data."""

    def __init__(self, db_path: str = "inspections.db",
                 attachments_dir: str = "attachments"):
        self.db_path = db_path
        self.attachments_dir = attachments_dir
        os.makedirs(attachments_dir, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def save_inspection(self, record: InspectionRecord) -> bool:
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO inspections
                       (inspection_id, form_id, submitted_by, submitted_at, response_json, sync_status, checksum)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (record.inspection_id, record.form_id, record.submitted_by,
                     record.submitted_at, json.dumps(record.response),
                     record.sync_status, record.checksum()),
                )
                self._enqueue("inspection", record.inspection_id)
                self._conn.commit()
                return True
            except Exception as exc:
                logger.error("save_inspection failed: %s", exc)
                return False

    def save_attachment(self, record: AttachmentRecord, data: bytes) -> bool:
        dest = os.path.join(self.attachments_dir, record.filename)
        try:
            with open(dest, "wb") as f:
                f.write(data)
        except Exception as exc:
            logger.error("Cannot write attachment %s: %s", dest, exc)
            return False
        record.file_path = dest
        record.file_size = len(data)
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO attachments
                       (attachment_id, inspection_id, filename, file_path, file_size, mime_type, captured_at, sync_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (record.attachment_id, record.inspection_id, record.filename,
                     record.file_path, record.file_size, record.mime_type,
                     record.captured_at, record.sync_status),
                )
                self._enqueue("attachment", record.attachment_id, priority=3)
                self._conn.commit()
                return True
            except Exception as exc:
                logger.error("save_attachment failed: %s", exc)
                return False

    def _enqueue(self, record_type: str, record_id: str, priority: int = 5) -> None:
        self._conn.execute(
            "INSERT INTO sync_queue (record_type, record_id, priority, created_at) VALUES (?, ?, ?, ?)",
            (record_type, record_id, priority, time.time()),
        )

    def get_pending(self, limit: int = 50) -> List[Dict]:
        rows = self._conn.execute(
            """SELECT sq.*, i.sync_status
               FROM sync_queue sq
               LEFT JOIN inspections i ON sq.record_id = i.inspection_id
               WHERE i.sync_status = 'pending' OR sq.record_type = 'attachment'
               ORDER BY sq.priority ASC, sq.created_at ASC
               LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_synced(self, record_type: str, record_id: str) -> None:
        now = time.time()
        with self._lock:
            if record_type == "inspection":
                self._conn.execute(
                    "UPDATE inspections SET sync_status = 'synced', synced_at = ? WHERE inspection_id = ?",
                    (now, record_id)
                )
            elif record_type == "attachment":
                self._conn.execute(
                    "UPDATE attachments SET sync_status = 'synced' WHERE attachment_id = ?",
                    (record_id,)
                )
            self._conn.execute(
                "DELETE FROM sync_queue WHERE record_id = ? AND record_type = ?",
                (record_id, record_type)
            )
            self._conn.commit()

    def pending_count(self) -> Dict:
        inspections = self._conn.execute(
            "SELECT COUNT(*) FROM inspections WHERE sync_status = 'pending'"
        ).fetchone()[0]
        attachments = self._conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE sync_status = 'pending'"
        ).fetchone()[0]
        return {"pending_inspections": inspections, "pending_attachments": attachments}

    def get_all_inspections(self, form_id: Optional[str] = None) -> List[Dict]:
        if form_id:
            rows = self._conn.execute(
                "SELECT * FROM inspections WHERE form_id = ? ORDER BY submitted_at DESC",
                (form_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM inspections ORDER BY submitted_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


class CaptureSession:
    """
    Manages a single inspection session: field data, attachments, and submission.
    """

    def __init__(self, form_id: str, store: LocalStore, user_id: str = "field_agent"):
        self.form_id = form_id
        self.store = store
        self.user_id = user_id
        self.inspection_id = f"{form_id}_{int(time.time() * 1000)}"
        self._responses: Dict[str, Any] = {}
        self._attachments: List[str] = []

    def set_field(self, field_id: str, value: Any) -> None:
        self._responses[field_id] = value

    def attach_photo(self, filename: str, data: bytes) -> bool:
        import hashlib
        attachment_id = hashlib.md5(f"{self.inspection_id}_{filename}".encode()).hexdigest()[:16]
        record = AttachmentRecord(
            attachment_id=attachment_id,
            inspection_id=self.inspection_id,
            filename=filename,
            file_path="",
            file_size=len(data),
        )
        success = self.store.save_attachment(record, data)
        if success:
            self._attachments.append(attachment_id)
        return success

    def submit(self) -> bool:
        record = InspectionRecord(
            inspection_id=self.inspection_id,
            form_id=self.form_id,
            submitted_by=self.user_id,
            response=self._responses,
        )
        return self.store.save_inspection(record)

    def get_summary(self) -> Dict:
        return {
            "inspection_id": self.inspection_id,
            "form_id": self.form_id,
            "fields_filled": len(self._responses),
            "attachments": len(self._attachments),
        }


if __name__ == "__main__":
    store = LocalStore(db_path=":memory:", attachments_dir="/tmp/test_attachments")
    session = CaptureSession(form_id="dock_inspection_v1", store=store, user_id="agent_001")
    session.set_field("dock_number", "D-07")
    session.set_field("dock_status", "clear")
    session.set_field("inspection_date", "2024-04-15")
    dummy_photo = b"JPEG_FAKE_DATA_" * 1000
    session.attach_photo("dock_D07_photo.jpg", dummy_photo)
    submitted = session.submit()
    print(f"Submitted: {submitted}")
    print("Session summary:", session.get_summary())
    print("Pending sync:", store.pending_count())
    pending = store.get_pending()
    print(f"Pending queue items: {len(pending)}")
    store.mark_synced("inspection", session.inspection_id)
    print("After sync:", store.pending_count())
