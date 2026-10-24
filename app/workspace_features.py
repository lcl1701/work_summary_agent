"""Local member labels, consistent backups, and message reconciliation."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
import uuid

from .feishu import FeishuError
from .periods import SHANGHAI, timestamp


def _source(source):
    if not isinstance(source, str) or source not in {"demo", "feishu"}:
        raise ValueError("数据来源应为 demo 或 feishu")
    return source


def _ids(values):
    if not isinstance(values, (list, tuple)) or len(values) > 20 or any(
        not isinstance(value, str) or not value.strip() or len(value) > 200 for value in values
    ):
        raise ValueError("群组或发言人应为不超过 20 项的有效标识")
    return list(dict.fromkeys(values))


def _label(value, limit):
    if not isinstance(value, str) or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("名称应为不含控制字符的文本")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"名称不能超过 {limit} 个字符")
    return value


_CORE_COLUMNS = {
    "chats": {"source", "chat_id", "name", "description"},
    "messages": {"source", "message_id", "chat_id", "sender_id", "sender_name", "text", "created_at", "message_type", "source_url"},
    "sync_ranges": {"source", "chat_id", "start_ts", "end_ts", "synced_at"},
    "reports": {"id", "title", "period", "start_date", "end_date", "source", "mode", "audience", "chat_ids", "sender_ids", "message_count", "markdown", "created_at"},
}


def _validate_database(db):
    db.execute("PRAGMA trusted_schema=OFF")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > 4:
        raise ValueError("备份来自更新的工程版本，请先更新程序；未覆盖当前数据")
    checks = db.execute("PRAGMA quick_check").fetchall()
    if len(checks) != 1 or checks[0][0] != "ok":
        raise ValueError("备份数据库校验失败，未覆盖当前数据")
    schema = db.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
    if any(kind in {"view", "trigger"} or "CREATE VIRTUAL TABLE" in (sql or "").upper()
           for kind, _, sql in schema):
        raise ValueError("备份包含不支持的数据库对象，未覆盖当前数据")
    tables = {name for kind, name, _ in schema if kind == "table"}
    for table, required in _CORE_COLUMNS.items():
        if table not in tables:
            raise ValueError("备份缺少工作汇报数据表，未覆盖当前数据")
        columns = {row[1] for row in db.execute('PRAGMA table_info("' + table + '")')}
        if not required <= columns:
            raise ValueError("备份数据结构不兼容，未覆盖当前数据")
    if version >= 4:
        for table, required in {"report_compositions": {"report_id", "snapshot"},
                                "composition_preferences": {"source", "config"}}.items():
            if table not in tables or not required <= {row[1] for row in db.execute('PRAGMA table_info("' + table + '")')}:
                raise ValueError("备份缺少报告组合配置，未覆盖当前数据")
    counts = {}
    for table in ("chats", "messages", "reports", "profiles", "preferences", "member_names", "member_aliases", "identity_settings",
                  "report_compositions", "composition_preferences"):
        if table in tables:
            counts[table] = db.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]
    return counts


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class WorkspaceFeatures:
    def setup_workspace_schema(self):
        with self.connect() as db:
            has_identity_settings = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='identity_settings'").fetchone() is not None
            db.executescript("""
                CREATE TABLE IF NOT EXISTS member_names (
                    source TEXT NOT NULL, chat_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                    official_name TEXT NOT NULL, updated_at INTEGER NOT NULL,
                    PRIMARY KEY(source,chat_id,sender_id)
                );
                CREATE INDEX IF NOT EXISTS member_names_sender ON member_names(source,sender_id,updated_at);
                CREATE TABLE IF NOT EXISTS member_aliases (
                    source TEXT NOT NULL, sender_id TEXT NOT NULL, alias TEXT NOT NULL,
                    PRIMARY KEY(source,sender_id)
                );
                CREATE TABLE IF NOT EXISTS identity_settings (
                    source TEXT NOT NULL, sender_id TEXT NOT NULL,
                    mode TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(source,sender_id)
                );
                CREATE TABLE IF NOT EXISTS composition_preferences (
                    source TEXT PRIMARY KEY, config TEXT NOT NULL
                );
            """)
            if not has_identity_settings:
                # The existing nickname text stays available, but upgrading must
                # not silently keep it enabled as though it were an official name.
                db.execute("""INSERT INTO identity_settings(source,sender_id,mode,nickname)
                    SELECT source,sender_id,'official',nickname FROM profiles WHERE sender_id<>''""")

    def save_member_names(self, source, chat_id, members):
        _source(source)
        _ids([chat_id])
        if not isinstance(members, list):
            raise ValueError("成员列表无效")
        values = []
        for member in members:
            if not isinstance(member, dict):
                raise ValueError("成员列表无效")
            sender_id = member.get("sender_id", member.get("member_id"))
            if not isinstance(sender_id, str) or not sender_id or len(sender_id) > 200 or sender_id == "unknown":
                raise ValueError("成员标识无效")
            name = _label(member.get("name", member.get("official_name", "")), 255)
            values.append((source, chat_id, sender_id, name, int(time.time())))
        with self.connect() as db:
            db.executemany("""INSERT INTO member_names(source,chat_id,sender_id,official_name,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(source,chat_id,sender_id) DO UPDATE SET
                official_name=excluded.official_name,updated_at=excluded.updated_at""", values)
        return len(values)

    def name_details(self, source, sender_id, original_name=""):
        _source(source)
        profile = self.profile(source)
        with self.connect() as db:
            setting = db.execute("SELECT mode,nickname FROM identity_settings WHERE source=? AND sender_id=?",
                                 (source, sender_id)).fetchone()
            alias = db.execute("SELECT alias FROM member_aliases WHERE source=? AND sender_id=?",
                               (source, sender_id)).fetchone()
            official = db.execute("""SELECT official_name FROM member_names WHERE source=? AND sender_id=?
                AND official_name<>'' ORDER BY updated_at DESC,chat_id LIMIT 1""", (source, sender_id)).fetchone()
        mode = setting["mode"] if setting else "official"
        nickname = setting["nickname"] if setting else profile["nickname"] if profile["sender_id"] == sender_id else ""
        official_name = official[0] if official else ""
        selected_mode = setting is not None or profile["sender_id"] == sender_id
        if selected_mode and mode == "nickname" and nickname:
            display, name_source = nickname, "nickname"
        elif selected_mode and official_name:
            display, name_source = official_name, "official"
        elif alias:
            display, name_source = alias[0], "alias"
        elif official_name:
            display, name_source = official_name, "official"
        elif original_name and original_name != sender_id and not re.fullmatch(r"(?:ou|on|cli)_[A-Za-z0-9]+", original_name):
            display, name_source = original_name, "original"
        else:
            tail = hashlib.sha256((source + "\0" + sender_id).encode("utf-8")).hexdigest()[:6]
            display, name_source = "未命名成员·" + tail, "unnamed"
        notice = ""
        if mode == "official" and not official_name:
            fallback = {"alias": "本机备注", "original": "原消息名称", "unnamed": "稳定未命名标识"}.get(name_source, "本机名称")
            notice = f"尚未读取飞书官方名称，当前使用{fallback}；此显示不代表官方姓名已验证。"
        elif mode == "official" and name_source == "alias":
            notice = "当前成员沿用本机备注；显式保存官方名称模式后使用飞书名称。"
        return {"sender_id": sender_id, "nickname": nickname, "mode": mode,
                "official_name": official_name, "display_name": display, "name_source": name_source, "notice": notice}

    def resolve_display_name(self, source, sender_id, original_name):
        return self.name_details(source, sender_id, original_name)["display_name"]

    def identity(self, source, sender_id=""):
        _source(source)
        if not isinstance(sender_id, str):
            raise ValueError("发言人标识无效")
        explicit_sender = bool(sender_id)
        sender_id = sender_id or self.profile(source)["sender_id"]
        if not sender_id:
            return {"sender_id": "", "nickname": "", "mode": "official", "official_name": "",
                    "display_name": "", "name_source": "unnamed", "notice": "请先选择已同步的本人发言人。"}
        if explicit_sender and not self.has_sender(source, sender_id):
            raise ValueError("请选择已同步的本人发言人")
        with self.connect() as db:
            row = db.execute("""SELECT MAX(CASE WHEN sender_name<>sender_id THEN sender_name END)
                FROM messages WHERE source=? AND sender_id=?""", (source, sender_id)).fetchone()
        return self.name_details(source, sender_id, row[0] or "")

    def save_identity(self, source, sender_id, mode, nickname=None):
        _source(source)
        if not isinstance(mode, str) or mode not in {"official", "nickname"}:
            raise ValueError("名称显示方式应为 official 或 nickname")
        if not self.has_sender(source, sender_id):
            raise ValueError("请选择已同步的本人发言人")
        current = self.identity(source, sender_id)
        nickname = current["nickname"] if nickname is None else _label(nickname, 30)
        if mode == "nickname" and not nickname:
            raise ValueError("使用本机昵称时，请填写 1–30 个字符")
        with self.connect() as db:
            db.execute("""INSERT INTO identity_settings(source,sender_id,mode,nickname) VALUES(?,?,?,?)
                ON CONFLICT(source,sender_id) DO UPDATE SET mode=excluded.mode,nickname=excluded.nickname""",
                (source, sender_id, mode, nickname))
            db.execute("""INSERT INTO profiles(source,sender_id,nickname) VALUES(?,?,?)
                ON CONFLICT(source) DO UPDATE SET sender_id=excluded.sender_id,nickname=excluded.nickname""",
                (source, sender_id, nickname))
        return self.identity(source, sender_id)

    def members(self, source, chat_ids=()):
        _source(source)
        chat_ids = _ids(chat_ids)
        clause, params = "source=?", [source]
        if chat_ids:
            clause += " AND chat_id IN (" + ",".join("?" for _ in chat_ids) + ")"
            params.extend(chat_ids)
        with self.connect() as db:
            rows = db.execute("SELECT sender_id,COUNT(*),MAX(CASE WHEN sender_name<>sender_id THEN sender_name END) FROM messages WHERE " + clause + " GROUP BY sender_id", params).fetchall()
            counts = {row[0]: (row[1], row[2]) for row in rows}
            name_rows = db.execute("SELECT sender_id,official_name FROM member_names WHERE " + clause + " ORDER BY updated_at,chat_id", params).fetchall()
            names = {row[0]: row[1] for row in name_rows}
            aliases = {row[0]: row[1] for row in db.execute("SELECT sender_id,alias FROM member_aliases WHERE source=?", (source,))}
        result = []
        for sender_id in sorted(set(counts) | set(names)):
            count, original = counts.get(sender_id, (0, ""))
            result.append({**self.name_details(source, sender_id, original),
                           "alias": aliases.get(sender_id, ""), "message_count": count})
        return result

    def save_member_alias(self, source, sender_id, alias):
        _source(source)
        alias = _label(alias, 30)
        if not isinstance(sender_id, str) or not sender_id or sender_id == "unknown" or len(sender_id) > 200:
            raise ValueError("请选择具有明确身份的已同步成员")
        with self.connect() as db:
            known = db.execute("SELECT 1 FROM messages WHERE source=? AND sender_id=? LIMIT 1", (source, sender_id)).fetchone()
            cached = db.execute("SELECT 1 FROM member_names WHERE source=? AND sender_id=? LIMIT 1", (source, sender_id)).fetchone()
            if alias and not (known or cached):
                raise ValueError("请选择已同步或已读取姓名的成员")
            if alias:
                db.execute("INSERT INTO member_aliases VALUES(?,?,?) ON CONFLICT(source,sender_id) DO UPDATE SET alias=excluded.alias", (source, sender_id, alias))
            else:
                db.execute("DELETE FROM member_aliases WHERE source=? AND sender_id=?", (source, sender_id))
        return next((member for member in self.members(source) if member["sender_id"] == sender_id),
                    {"sender_id": sender_id, "alias": "", "official_name": "", "message_count": 0,
                     "display_name": self.resolve_display_name(source, sender_id, "")})

    def _backup_dir(self):
        directory = Path(self.path).resolve().parent / "backups"
        if directory.exists() and directory.resolve() != directory:
            raise ValueError("备份目录必须位于本机数据目录内")
        directory.mkdir(exist_ok=True)
        return directory

    def create_backup(self):
        directory, identity = self._backup_dir(), uuid.uuid4().hex
        temporary = directory / (identity + ".pending.sqlite3")
        target = directory / (identity + ".sqlite3")
        try:
            with self.connect() as source, closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
            with closing(sqlite3.connect(temporary.as_uri() + "?mode=ro", uri=True)) as db:
                counts = _validate_database(db)
            temporary.replace(target)
            metadata = {"id": identity, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "size": target.stat().st_size, "counts": counts, "sha256": _digest(target)}
            pending = directory / (identity + ".pending.json")
            pending.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            pending.replace(directory / (identity + ".json"))
            return {key: value for key, value in metadata.items() if key != "sha256"}
        finally:
            temporary.unlink(missing_ok=True)

    def _verified_backup(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise ValueError("备份标识无效")
        directory = self._backup_dir()
        target, manifest = directory / (identity + ".sqlite3"), directory / (identity + ".json")
        if not target.is_file() or not manifest.is_file() or target.resolve().parent != directory or manifest.resolve().parent != directory:
            raise ValueError("备份不存在或不位于本机备份目录")
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            if (not isinstance(metadata, dict) or not isinstance(metadata.get("created_at"), str)
                    or type(metadata.get("size")) is not int or metadata["size"] <= 0
                    or not isinstance(metadata.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
                    or not isinstance(metadata.get("counts"), dict)
                    or any(not isinstance(key, str) or type(value) is not int or value < 0
                           for key, value in metadata["counts"].items())):
                raise ValueError("备份清单无效，未覆盖当前数据")
            datetime.fromisoformat(metadata["created_at"])
            if metadata["id"] != identity or metadata["size"] != target.stat().st_size or metadata["sha256"] != _digest(target):
                raise ValueError("备份文件已损坏或发生变化，未覆盖当前数据")
            with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as db:
                counts = _validate_database(db)
            if metadata["counts"] != counts:
                raise ValueError("备份记录校验失败，未覆盖当前数据")
        except (OSError, sqlite3.Error, KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("备份读取或校验失败，未覆盖当前数据") from None
        return target, {key: value for key, value in metadata.items() if key != "sha256"}

    def backups(self):
        result = []
        for path in self._backup_dir().glob("*.json"):
            if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                continue
            try:
                result.append(self._verified_backup(path.stem)[1])
            except ValueError:
                pass
        return sorted(result, key=lambda item: (item["created_at"], item["id"]), reverse=True)

    def restore_backup(self, identity):
        target, metadata = self._verified_backup(identity)
        safety = self.create_backup()
        try:
            with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as source:
                _validate_database(source)
                with self.connect() as destination:
                    source.backup(destination)
            # Restore old database versions through the normal migration path.
            self.__class__(self.path)
        except Exception:
            rollback, _ = self._verified_backup(safety["id"])
            try:
                with closing(sqlite3.connect(rollback.as_uri() + "?mode=ro", uri=True)) as source, self.connect() as destination:
                    source.backup(destination)
            except Exception:
                raise ValueError("恢复未完成，恢复前安全备份已保存；请停止操作并使用安全备份恢复") from None
            raise ValueError("恢复未完成，已保留恢复前的数据和安全备份") from None
        return dict(metadata, safety_backup=safety)

    def audit(self, source, chat_ids, start, end, sender_ids=()):
        _source(source)
        chat_ids, sender_ids = _ids(chat_ids), _ids(sender_ids)
        if not chat_ids:
            raise ValueError("请先选择要对账的群组")
        if type(start) is not int or type(end) is not int or end <= start or end - start > 366 * 86400:
            raise ValueError("对账日期应按先后顺序且不超过 366 天")
        placeholders = ",".join("?" for _ in chat_ids)
        with self.connect() as db:
            rows = db.execute(f"""SELECT chat_id,sender_id,MAX(sender_name),
                date(created_at,'unixepoch','+8 hours'),COUNT(*) FROM messages
                WHERE source=? AND chat_id IN ({placeholders}) AND created_at>=? AND created_at<?
                GROUP BY chat_id,sender_id,date(created_at,'unixepoch','+8 hours')""", [source, *chat_ids, start, end]).fetchall()
            names = {row[0]: row[1] for row in db.execute("SELECT chat_id,name FROM chats WHERE source=?", (source,))}
        counts, selected_counts, authors, active_dates, selected_dates = {}, {}, {}, set(), set()
        for chat, sender, original, day, count in rows:
            key = (day, chat)
            counts[key] = counts.get(key, 0) + count
            selected = not sender_ids or sender in sender_ids
            selected_counts[key] = selected_counts.get(key, 0) + (count if selected else 0)
            if sender not in authors:
                authors[sender] = {"sender_id": sender, "display_name": self.resolve_display_name(source, sender, original),
                                   "message_count": 0, "selected": selected}
            author = authors[sender]
            author["message_count"] += count
            active_dates.add(day)
            if selected:
                selected_dates.add(day)
        days = []
        day = datetime.fromtimestamp(start, SHANGHAI).date()
        while timestamp(day) < end:
            next_day = day + timedelta(days=1)
            begin, finish = max(start, timestamp(day)), min(end, timestamp(next_day))
            for index, chat in enumerate(chat_ids, 1):
                key = (day.isoformat(), chat)
                days.append({"date": day.isoformat(), "chat_id": chat, "chat_name": names.get(chat, f"群聊 {index}"),
                             "message_count": counts.get(key, 0), "selected_sender_count": selected_counts.get(key, 0),
                             "coverage": self.coverage(source, [chat], begin, finish)})
            day = next_day
        return {"days": days, "senders": sorted(authors.values(), key=lambda row: row["sender_id"]),
                "message_count": sum(counts.values()), "selected_message_count": sum(selected_counts.values()),
                "active_days": len(active_dates), "selected_active_days": len(selected_dates),
                "coverage": self.coverage(source, chat_ids, start, end),
                "note": "零消息不代表没有开展工作；对账仅统计本机已同步的可读取消息，不等于飞书全部记录。"}


def refresh_members(storage, feishu, source, chat_ids):
    _source(source)
    chat_ids = _ids(chat_ids)
    result = {"members_refreshed": 0, "warnings": []}
    if source == "demo":
        return result
    for chat_id in chat_ids:
        try:
            if feishu is None:
                raise FeishuError("尚未配置飞书应用，姓名未刷新；可继续使用本地备注。")
            result["members_refreshed"] += storage.save_member_names(source, chat_id, feishu.list_members(chat_id))
        except FeishuError as exc:
            result["warnings"].append({"chat_id": chat_id, "code": exc.code, "message": str(exc)})
        except Exception:
            result["warnings"].append({"chat_id": chat_id, "code": None, "message": "成员姓名暂未刷新；消息仍可同步，请继续使用本地备注。"})
    return result
