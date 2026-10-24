"""Allowlisted operation receipts, separate from the restorable workspace DB."""

from contextlib import closing
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .versioning import VERSION


ACTIONS = frozenset({"save_identity", "save_profile", "save_remark", "save_preferences", "refresh_names",
    "sync_messages", "sync_and_generate", "generate_report", "revise_report", "review_report",
    "link_task", "unlink_task", "create_backup", "restore_backup", "seed_demo",
    "save_composition_preferences", "save_report_preview"})
_IDS = re.compile(r"^[a-f0-9]{32}$")
_COUNT_KEYS = frozenset({"synced_count", "total_count", "message_count", "members_refreshed",
    "review_item_count", "manual_item_count", "messages", "reports"})
_ERROR_CODES = frozenset({"invalid_input", "model_error", "internal_error", "interrupted", "name_permission"})


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _metadata(raw):
    """Never persist free-form request values, report text, notes or errors."""
    data = {}
    if isinstance(raw.get("source"), str) and raw["source"] in {"feishu", "demo"}:
        data["source"] = raw["source"]
    chats = raw.get("chat_ids", [])
    if "chat_ids" in raw and isinstance(chats, list):
        # Application supplies only chat IDs already known to the workspace.
        data["chat_ids"] = list(dict.fromkeys(c for c in chats if isinstance(c, str) and 0 < len(c) <= 200))[:20]
    for key in ("period", "audience", "display_mode"):
        allowed = {"period": {"daily", "weekly", "monthly"}, "audience": {"personal", "project"},
                   "display_mode": {"official", "nickname"}}[key]
        if isinstance(raw.get(key), str) and raw[key] in allowed:
            data[key] = raw[key]
    for key, allowed in (("template_id", {"classic", "ppp", "project", "reflection"}),
                         ("framework_id", {"none", "bluf", "prep", "scqa", "sbar", "wswnw"})):
        if isinstance(raw.get(key), str) and raw[key] in allowed:
            data[key] = raw[key]
    for key in ("start_date", "end_date"):
        value = raw.get(key)
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                date.fromisoformat(value)
                data[key] = value
            except ValueError:
                pass
    for key in ("report_id", "related_id", "job_id"):
        value = raw.get(key)
        if isinstance(value, str) and _IDS.fullmatch(value):
            data[key] = value
    counts = raw.get("counts", {})
    if "counts" in raw and isinstance(counts, dict):
        data["counts"] = {k: v for k, v in counts.items() if k in _COUNT_KEYS and type(v) is int and 0 <= v <= 10**9}
    code = raw.get("error_code")
    if type(code) is int and 0 <= code <= 10**9 or isinstance(code, str) and code in _ERROR_CODES:
        data["error_code"] = code
    return data


class OperationLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS operations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    time TEXT NOT NULL, finished_at TEXT, action TEXT NOT NULL,
                    status TEXT NOT NULL, version TEXT NOT NULL, metadata TEXT NOT NULL
                );""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def recover_interrupted(self):
        with closing(self._connect()) as db, db:
            rows = db.execute("SELECT id,metadata FROM operations WHERE status='running'").fetchall()
            for row in rows:
                data = json.loads(row["metadata"])
                data["error_code"] = "interrupted"
                db.execute("UPDATE operations SET status='interrupted',finished_at=?,metadata=? WHERE id=?",
                           (_now(), json.dumps(data, ensure_ascii=False), row["id"]))

    def begin(self, action, metadata):
        if action not in ACTIONS:
            raise ValueError("操作记录类型无效")
        receipt_id = uuid.uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO operations(id,time,action,status,version,metadata) VALUES(?,?,?,'running',?,?)",
                       (receipt_id, _now(), action, VERSION, json.dumps(_metadata(metadata), ensure_ascii=False)))
        return receipt_id

    def finish(self, receipt_id, status, metadata):
        if status not in {"succeeded", "failed"}:
            raise ValueError("操作记录状态无效")
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT metadata FROM operations WHERE id=?", (receipt_id,)).fetchone()
            if not row:
                raise ValueError("操作记录不存在")
            data = json.loads(row[0])
            # Empty finish fields must not discard the original operation scope.
            data.update(_metadata(metadata))
            db.execute("UPDATE operations SET status=?,finished_at=?,metadata=? WHERE id=?",
                       (status, _now(), json.dumps(data, ensure_ascii=False), receipt_id))

    def list(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("操作记录条数应为 1—100")
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM operations ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            total = db.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        return {"operations": [dict(id=row["id"], time=row["time"], finished_at=row["finished_at"],
            action=row["action"], status=row["status"], version=row["version"], **json.loads(row["metadata"]))
            for row in rows], "total": total}

    def revision(self):
        with closing(self._connect()) as db:
            return str(db.execute("SELECT COALESCE(MAX(seq),0) FROM operations WHERE status='succeeded' AND action<>'create_backup'").fetchone()[0])
