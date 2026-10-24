from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
import unicodedata
import uuid

from .report_features import ReportFeatures
from .workspace_features import WorkspaceFeatures


class Storage(WorkspaceFeatures, ReportFeatures):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_before_upgrade()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS chats (
                    source TEXT NOT NULL, chat_id TEXT NOT NULL,
                    name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(source, chat_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    source TEXT NOT NULL, message_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL, text TEXT NOT NULL,
                    created_at INTEGER NOT NULL, message_type TEXT NOT NULL DEFAULT 'text',
                    source_url TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(source, message_id)
                );
                CREATE INDEX IF NOT EXISTS messages_range ON messages(source, created_at, chat_id);
                CREATE TABLE IF NOT EXISTS sync_ranges (
                    source TEXT NOT NULL, chat_id TEXT NOT NULL,
                    start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL, synced_at INTEGER NOT NULL,
                    PRIMARY KEY(source,chat_id,start_ts,end_ts)
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, period TEXT NOT NULL,
                    start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                    source TEXT NOT NULL, mode TEXT NOT NULL, audience TEXT NOT NULL,
                    chat_ids TEXT NOT NULL, sender_ids TEXT NOT NULL,
                    message_count INTEGER NOT NULL, markdown TEXT NOT NULL,
                    created_at TEXT NOT NULL, coverage TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    source TEXT PRIMARY KEY, sender_id TEXT NOT NULL, nickname TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    source TEXT PRIMARY KEY, chat_ids TEXT NOT NULL,
                    period TEXT NOT NULL, audience TEXT NOT NULL, sender_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)
            # Existing local databases predate coverage snapshots.
            if "coverage" not in {row["name"] for row in db.execute("PRAGMA table_info(reports)")}:
                db.execute("ALTER TABLE reports ADD COLUMN coverage TEXT NOT NULL DEFAULT '{}'")
        self.setup_workspace_schema()
        self.setup_report_schema()
        with self.connect() as db:
            db.execute("PRAGMA user_version=4")

    def _backup_before_upgrade(self):
        if not self.path.exists() or not self.path.stat().st_size:
            return
        with closing(sqlite3.connect(self.path)) as source:
            version = source.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version > 4:
                raise ValueError("数据文件来自更新的工程版本，请先更新程序；未修改该数据库")
            if version == 4 or not {"messages", "reports", "chats"}.issubset(tables):
                return
            if version in {2, 3}:
                self.create_backup()
                return
            directory = self.path.parent / "backups"
            directory.mkdir(exist_ok=True)
            with closing(sqlite3.connect(directory / (uuid.uuid4().hex + ".sqlite3"))) as target:
                source.backup(target)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_chats(self, source, chats):
        with self.connect() as db:
            self._save_chats(db, source, chats)

    @staticmethod
    def _save_chats(db, source, chats):
        db.executemany("""INSERT INTO chats(source,chat_id,name,description) VALUES(?,?,?,?)
            ON CONFLICT(source,chat_id) DO UPDATE SET name=excluded.name,description=excluded.description""",
            [(source, c["chat_id"], c["name"], c.get("description", "")) for c in chats])

    def chats(self, source):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT chat_id,name,description FROM chats WHERE source=? ORDER BY name", (source,))]

    def save_messages(self, source, messages, chats=(), replace_range=None, coverage=None):
        with self.connect() as db:
            self._save_chats(db, source, chats)
            if replace_range:
                chat_ids, start, end = replace_range
                for chat_id in chat_ids:
                    db.execute("DELETE FROM messages WHERE source=? AND chat_id=? AND created_at>=? AND created_at<?",
                               (source, chat_id, start, end))
            db.executemany("""INSERT INTO messages
                (source,message_id,chat_id,sender_id,sender_name,text,created_at,message_type,source_url)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source,message_id) DO UPDATE SET
                chat_id=excluded.chat_id,sender_id=excluded.sender_id,sender_name=excluded.sender_name,
                text=excluded.text,created_at=excluded.created_at,message_type=excluded.message_type,
                source_url=excluded.source_url""", [(
                    source, m["message_id"], m["chat_id"], m["sender_id"],
                    m.get("sender_name") or m["sender_id"], m["text"], int(m["created_at"]),
                    m.get("message_type", "text"), m.get("source_url") or ""
                ) for m in messages])
            if coverage or replace_range:
                chat_ids, start, end = coverage or replace_range
                db.executemany("""INSERT INTO sync_ranges(source,chat_id,start_ts,end_ts,synced_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(source,chat_id,start_ts,end_ts)
                    DO UPDATE SET synced_at=excluded.synced_at""",
                    [(source, chat_id, start, end, int(time.time())) for chat_id in chat_ids])

    def coverage(self, source, chat_ids, start, end, now=None):
        """Coverage means successfully queried at a point in time, not future completeness."""
        now = int(time.time()) if now is None else int(now)
        chat_ids = list(dict.fromkeys(chat_ids))
        missing, latest, cursors = [], None, []
        with self.connect() as db:
            for chat_id in chat_ids:
                rows = db.execute("""SELECT start_ts,end_ts,synced_at FROM sync_ranges
                    WHERE source=? AND chat_id=? AND end_ts>? AND start_ts<? ORDER BY start_ts""",
                    (source, chat_id, start, end)).fetchall()
                cursor, requested_cursor, query_moment = start, start, None
                for row in rows:
                    latest = max(latest or 0, row["synced_at"])
                    query_moment = max(query_moment or 0, min(row["synced_at"], now))
                    if row["start_ts"] <= requested_cursor:
                        requested_cursor = max(requested_cursor, row["end_ts"])
                    if row["start_ts"] <= cursor:
                        # A query whose requested end is in the future has only
                        # observed messages through its successful query time.
                        cursor = max(cursor, min(row["end_ts"], row["synced_at"], now))
                cursor = min(cursor, end)
                cursors.append(cursor)
                target = min(end, query_moment) if end > now and query_moment is not None else end
                if requested_cursor < end or cursor < target or start >= target:
                    missing.append(chat_id)
            if chat_ids:
                placeholders = ",".join("?" for _ in chat_ids)
                observed = db.execute(f"""SELECT COUNT(*) AS message_count,
                    COUNT(DISTINCT date(created_at,'unixepoch','+8 hours')) AS active_day_count,
                    MIN(created_at) AS observed_first_at,MAX(created_at) AS observed_last_at
                    FROM messages WHERE source=? AND chat_id IN ({placeholders})
                    AND created_at>=? AND created_at<?""", [source, *chat_ids, start, end]).fetchone()
                stats = dict(observed)
            else:
                stats = {"message_count": 0, "active_day_count": 0,
                         "observed_first_at": None, "observed_last_at": None}
        common_cursor = min(cursors) if cursors else start
        return {"complete": bool(chat_ids) and not missing, "missing_chat_ids": missing,
                "last_synced_at": latest,
                "checked_through_ts": common_cursor if common_cursor > start else None,
                "period_start_ts": start, "period_end_ts": end, "period_has_future": end > now,
                "period_not_started": start >= now,
                "chat_count": len(chat_ids), "observed_message_count": stats["message_count"],
                "active_day_count": stats["active_day_count"],
                "observed_first_at": stats["observed_first_at"], "observed_last_at": stats["observed_last_at"]}

    def profile(self, source):
        with self.connect() as db:
            row = db.execute("SELECT sender_id,nickname FROM profiles WHERE source=?", (source,)).fetchone()
            return dict(row) if row else {"sender_id": "", "nickname": ""}

    def has_sender(self, source, sender_id):
        if not isinstance(sender_id, str) or not sender_id or sender_id == "unknown" or len(sender_id) > 200:
            return False
        with self.connect() as db:
            return db.execute("SELECT 1 FROM messages WHERE source=? AND sender_id=? LIMIT 1",
                              (source, sender_id)).fetchone() is not None

    def preferences(self, source):
        with self.connect() as db:
            row = db.execute("SELECT chat_ids,period,audience,sender_id FROM preferences WHERE source=?",
                             (source,)).fetchone()
        if not row:
            return {"chat_ids": [], "period": "daily", "audience": "project", "sender_id": ""}
        result = dict(row)
        result["chat_ids"] = json.loads(result["chat_ids"])
        return result

    def last_source(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM local_state WHERE key='last_source'").fetchone()
        return row["value"] if row and row["value"] in {"demo", "feishu"} else None

    def has_preferences(self, source):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM preferences WHERE source=?", (source,)).fetchone() is not None

    def save_preferences(self, source, chat_ids, period, audience, sender_id):
        if not isinstance(source, str) or source not in {"demo", "feishu"}:
            raise ValueError("数据来源应为 demo 或 feishu")
        if not isinstance(chat_ids, list) or len(chat_ids) > 20 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200 for item in chat_ids
        ):
            raise ValueError("群组应为不超过 20 项的非空标识数组")
        if not isinstance(period, str) or period not in {"daily", "weekly", "monthly"}:
            raise ValueError("报告周期须为 daily、weekly 或 monthly")
        if not isinstance(audience, str) or audience not in {"personal", "project"}:
            raise ValueError("报告类型须为 personal 或 project")
        if not isinstance(sender_id, str) or (sender_id and not self.has_sender(source, sender_id)):
            raise ValueError("请选择已同步的本人发言人，或清空本人身份")
        preferences = {"chat_ids": list(dict.fromkeys(chat_ids)), "period": period,
                       "audience": audience, "sender_id": sender_id}
        with self.connect() as db:
            db.execute("""INSERT INTO preferences(source,chat_ids,period,audience,sender_id) VALUES(?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET chat_ids=excluded.chat_ids,period=excluded.period,
                audience=excluded.audience,sender_id=excluded.sender_id""",
                (source, json.dumps(preferences["chat_ids"]), period, audience, sender_id))
            db.execute("""INSERT INTO local_state(key,value) VALUES('last_source',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (source,))
        return preferences

    def composition_preferences(self, source):
        if not isinstance(source, str) or source not in {"demo", "feishu"}:
            raise ValueError("数据来源应为 demo 或 feishu")
        with self.connect() as db:
            row = db.execute("SELECT config FROM composition_preferences WHERE source=?", (source,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_composition_preferences(self, source, config):
        if not isinstance(source, str) or source not in {"demo", "feishu"}:
            raise ValueError("数据来源应为 demo 或 feishu")
        from .composition import preference_config
        config = preference_config(config)
        with self.connect() as db:
            db.execute("""INSERT INTO composition_preferences(source,config) VALUES(?,?)
                ON CONFLICT(source) DO UPDATE SET config=excluded.config""",
                (source, json.dumps(config, ensure_ascii=False)))
        return config

    def save_profile(self, source, sender_id, nickname):
        if not isinstance(source, str) or source not in {"feishu", "demo"}:
            raise ValueError("数据来源应为 demo 或 feishu")
        if not isinstance(nickname, str):
            raise ValueError("昵称应为文本")
        # Reject control/format characters before trimming so line breaks cannot
        # become invisible saved input or override displayed attribution.
        if any(unicodedata.category(char).startswith("C") for char in nickname):
            raise ValueError("昵称不能包含控制字符")
        nickname = nickname.strip()
        if not nickname:
            with self.connect() as db:
                db.execute("""DELETE FROM identity_settings WHERE source=? AND sender_id IN
                    (SELECT sender_id FROM profiles WHERE source=?)""", (source, source))
                db.execute("DELETE FROM profiles WHERE source=?", (source,))
            return {"sender_id": "", "nickname": ""}
        if len(nickname) > 30:
            raise ValueError("昵称应为 1–30 个字符")
        if not isinstance(sender_id, str) or not sender_id or sender_id == "unknown" or len(sender_id) > 200:
            raise ValueError("请选择已同步的本人发言人")
        with self.connect() as db:
            known = db.execute("SELECT 1 FROM messages WHERE source=? AND sender_id=? LIMIT 1",
                               (source, sender_id)).fetchone()
            if not known:
                raise ValueError("请选择已同步的本人发言人")
            db.execute("""INSERT INTO profiles(source,sender_id,nickname) VALUES(?,?,?)
                ON CONFLICT(source) DO UPDATE SET sender_id=excluded.sender_id,nickname=excluded.nickname""",
                (source, sender_id, nickname))
            db.execute("""INSERT INTO identity_settings(source,sender_id,mode,nickname) VALUES(?,?,'nickname',?)
                ON CONFLICT(source,sender_id) DO UPDATE SET mode='nickname',nickname=excluded.nickname""",
                (source, sender_id, nickname))
        return {"sender_id": sender_id, "nickname": nickname}

    def messages(self, source, start=None, end=None, chat_ids=(), sender_ids=(), limit=None):
        clauses, params = ["m.source=?"], [source]
        if start is not None:
            clauses.append("m.created_at>=?")
            params.append(start)
        if end is not None:
            clauses.append("m.created_at<?")
            params.append(end)
        for col, ids in (("chat_id", chat_ids), ("sender_id", sender_ids)):
            if ids:
                clauses.append(f"m.{col} IN ({','.join('?' for _ in ids)})")
                params.extend(ids)
        sql = """SELECT m.*,COALESCE(c.name,m.chat_id) AS chat_name FROM messages m
                 LEFT JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id WHERE """
        sql += " AND ".join(clauses) + " ORDER BY m.created_at,m.message_id"
        if limit is not None:
            # Preview the latest messages while reports always use the complete period.
            sql = "SELECT * FROM (" + sql.rsplit(" ORDER BY", 1)[0] + " ORDER BY m.created_at DESC,m.message_id DESC LIMIT ?) ORDER BY created_at,message_id"
            params.append(limit)
        with self.connect() as db:
            result = [dict(r) for r in db.execute(sql, params)]
        labels = {}
        for message in result:
            key = (message["sender_id"], message["sender_name"])
            if key not in labels:
                labels[key] = self.name_details(source, *key)
            message.update({field: labels[key][field] for field in ("display_name", "official_name", "name_source", "mode", "notice")})
        return result

    def senders(self, source, chat_ids=()):
        clause, args = "source=?", [source]
        if chat_ids:
            clause += " AND chat_id IN (" + ",".join("?" for _ in chat_ids) + ")"
            args.extend(chat_ids)
        with self.connect() as db:
            result = [dict(r) for r in db.execute(f"""SELECT sender_id,
                COALESCE(MAX(CASE WHEN sender_name<>sender_id AND sender_name<>'' THEN sender_name END),
                         MAX(sender_name)) AS sender_name,
                COUNT(*) AS message_count FROM messages WHERE {clause} GROUP BY sender_id ORDER BY sender_name""", args)]
        for sender in result:
            detail = self.name_details(source, sender["sender_id"], sender["sender_name"])
            sender.update({field: detail[field] for field in ("display_name", "official_name", "name_source", "mode", "notice")})
        return result

    def count(self, source):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM messages WHERE source=?", (source,)).fetchone()[0]

    def save_report(self, report):
        report = dict(report, id=uuid.uuid4().hex,
                      created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        report.setdefault("coverage", {})
        columns = ("id", "title", "period", "start_date", "end_date", "source", "mode", "audience",
                   "chat_ids", "sender_ids", "message_count", "markdown", "created_at", "coverage")
        values = [json.dumps(report[c], ensure_ascii=False) if c in {"chat_ids", "sender_ids", "coverage"}
                  else report[c] for c in columns]
        with self.connect() as db:
            db.execute(f"INSERT INTO reports({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)
            if "evidence" in report:
                db.execute("""INSERT INTO report_details(report_id,evidence,items,template_version,task_timeline)
                    VALUES(?,?,?,?,?)""", (report["id"], json.dumps(report["evidence"], ensure_ascii=False),
                    json.dumps(report.get("items", []), ensure_ascii=False), str(report.get("template_version", "2")),
                    json.dumps(report.get("task_timeline", []), ensure_ascii=False)))
            if report.get("composition") is not None:
                self._save_report_composition(db, report["id"], report["composition"])
        return report

    @staticmethod
    def _report(row):
        result = dict(row)
        for col in ("chat_ids", "sender_ids", "coverage"):
            result[col] = json.loads(result[col])
        return result

    def reports(self):
        with self.connect() as db:
            reports = [self._report(r) for r in db.execute(
                "SELECT id,title,period,start_date,end_date,source,mode,audience,chat_ids,sender_ids,message_count,created_at,coverage FROM reports ORDER BY created_at DESC,rowid DESC LIMIT 100")]
        return [self.enrich_report(report, include_snapshot=False) for report in reports]

    def report(self, report_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            report = self._report(row) if row else None
        return self.enrich_report(report) if report else None
