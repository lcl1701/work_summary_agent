from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
import sys
from threading import Lock
import time
import uuid
from urllib.parse import parse_qs, quote, urlsplit

from .config import load_settings
from .demo import seed_demo
from .feishu import FeishuClient, FeishuError
from .jobs import JobManager, OperationBusy
from .operations import OperationLog
from .periods import SHANGHAI, parse_date, period_range, timestamp
from .reporting import ModelError, build_report
from .storage import Storage
from .workspace_features import refresh_members
from .versioning import VERSION, CHANGES, backend_build, page_build
from .composition import catalog, normalize_composition

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 1_000_000


def source_value(value):
    if not isinstance(value, str) or value not in {"demo", "feishu"}:
        raise ValueError("数据来源应为 demo 或 feishu")
    return value


def ids_value(value, label, required=False):
    if not isinstance(value, list) or len(value) > 20 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 200 for item in value
    ):
        raise ValueError(f"{label}应为不超过 20 项的非空标识数组")
    if required and not value:
        raise ValueError(f"请先选择{label}")
    return list(dict.fromkeys(value))


class Application:
    def __init__(self, settings):
        self.settings = settings
        self.storage = Storage(settings.data_dir / "work_summary.sqlite3")
        self.feishu = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret) if settings.feishu_configured else None
        self.operation_lock = Lock()
        self.jobs = JobManager(self.operation_lock)
        self.member_attempts = {}
        self.operations = OperationLog(settings.data_dir / "operations.sqlite3")
        self.instance_id = uuid.uuid4().hex
        self.running_build = backend_build()
        self.previews = {}

    def get(self, path, query):
        if path == "/api/composition/catalog":
            return catalog()
        if path == "/api/composition/preferences":
            source = source_value(query.get("source", ["demo"])[0])
            return {"composition": self.storage.composition_preferences(source), "source": source}
        if path == "/api/version":
            return {"version": VERSION, "build": page_build(), "backend_build": backend_build(),
                    "running_build": self.running_build, "instance_id": self.instance_id,
                    "environment": "feishu" if self.settings.feishu_configured else "demo",
                    "settings_revision": self.operations.revision(), "changes": CHANGES}
        if path == "/api/operations":
            try:
                limit = int(query.get("limit", ["50"])[0])
            except (ValueError, TypeError):
                raise ValueError("操作记录条数无效") from None
            return self.operations.list(limit)
        if path == "/api/jobs/current":
            return {"job": self.jobs.current()}
        job_match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})", path)
        if job_match:
            job = self.jobs.get(job_match.group(1))
            return {"job": job} if job else None
        if path == "/api/backups":
            with self.operation_lock:
                return {"backups": self.storage.backups()}
        if path == "/api/status":
            return {"demo_mode": not self.settings.feishu_configured,
                    "feishu_configured": self.settings.feishu_configured, "llm_configured": self.settings.llm_configured,
                    "mode": "feishu" if self.settings.feishu_configured else "demo", "timezone": "Asia/Shanghai",
                    "today": datetime.now(SHANGHAI).date().isoformat(), "demo_seeded": bool(self.storage.count("demo")),
                    "preferred_source": self.storage.last_source()}
        if path == "/api/reports":
            return {"reports": self.storage.reports()}
        if path.startswith("/api/reports/"):
            match = re.fullmatch(r"/api/reports/([a-f0-9]{32})", path)
            if not match:
                return None
            report = self.storage.report(match.group(1))
            return {"report": report} if report else None
        source = source_value(query.get("source", ["demo"])[0])
        if path == "/api/profile":
            return {"profile": self.storage.profile(source)}
        if path == "/api/identity":
            return {"identity": self.storage.identity(source, query.get("sender_id", [""])[0])}
        if path == "/api/preferences":
            return {"preferences": self.storage.preferences(source), "last_source": self.storage.last_source(),
                    "has_saved": self.storage.has_preferences(source)}
        chat_ids = ids_value(query.get("chat_ids", []), "群组")
        if path == "/api/members":
            return {"members": self.storage.members(source, chat_ids)}
        if path == "/api/tasks":
            return self.storage.task_groups(source, chat_ids)
        if path == "/api/audit":
            chat_ids = ids_value(chat_ids, "群组", required=True)
            if "start_date" in query or "end_date" in query:
                start = parse_date(query.get("start_date", [None])[0])
                end = parse_date(query.get("end_date", [None])[0]) + timedelta(days=1)
            else:
                start, end = period_range(query.get("period", ["daily"])[0], query.get("anchor_date", [None])[0])
            sender_ids = ids_value(query.get("sender_ids", []), "发言人")
            return {"audit": self.storage.audit(source, chat_ids, timestamp(start), timestamp(end), sender_ids)}
        if path == "/api/coverage":
            chat_ids = ids_value(chat_ids, "群组", required=True)
            start, end = period_range(query.get("period", ["daily"])[0],
                                      query.get("anchor_date", [None])[0])
            return {"coverage": self.storage.coverage(source, chat_ids, timestamp(start), timestamp(end))}
        if path == "/api/chats":
            if source == "feishu":
                self.require_feishu()
                chats = self.feishu.list_chats()
                with self.operation_lock:
                    self.storage.save_chats(source, chats)
                return {"chats": chats}
            return {"chats": self.storage.chats(source)}
        if path == "/api/messages":
            try:
                limit = int(query.get("limit", ["50"])[0])
            except (ValueError, TypeError):
                raise ValueError("消息预览条数无效") from None
            if not 1 <= limit <= 500:
                raise ValueError("消息预览条数应在 1–500 之间")
            return {"messages": self.storage.messages(source, chat_ids=chat_ids, limit=limit)}
        if path == "/api/senders":
            return {"senders": self.storage.senders(source, chat_ids)}
        return None

    def require_feishu(self):
        if not self.feishu:
            raise ValueError("尚未配置飞书应用。请在本地 .env 填写 FEISHU_APP_ID、FEISHU_APP_SECRET 并重启。")

    def sync(self, source, chat_ids, start, last, progress=None):
        if last < start or (last - start).days > 365:
            raise ValueError("同步日期应按先后顺序，且一次不超过 366 天")
        begin, end = timestamp(start), timestamp(last + timedelta(days=1))
        if source == "demo":
            count = len(self.storage.messages(source, begin, end, chat_ids))
            # Query the existing, explicitly labelled fictional records without
            # generating more data or replacing them with an empty refresh.
            self.storage.save_messages(source, [], coverage=(chat_ids, begin, end))
        else:
            self.require_feishu()
            messages = []
            # Fetch everything first: one failed chat must not leave a partial refresh.
            for index, chat_id in enumerate(chat_ids, 1):
                def page_progress(event):
                    if progress:
                        retry = event.get("phase") == "retry"
                        progress(stage="retry" if retry else "reading",
                                 message=(f"正在重试第 {index}/{len(chat_ids)} 个群，稍后继续。" if retry else f"正在读取第 {index}/{len(chat_ids)} 个群，第 {event.get('page', 1)} 页。"),
                                 completed=index - 1, total=len(chat_ids), **{k: event[k] for k in ("page", "attempt", "wait") if k in event})
                if progress:
                    progress(stage="reading", message=f"正在读取第 {index}/{len(chat_ids)} 个群。", completed=index - 1, total=len(chat_ids))
                fetched = (self.feishu.list_messages(chat_id, begin, end, progress=page_progress)
                           if progress and isinstance(self.feishu, FeishuClient) else self.feishu.list_messages(chat_id, begin, end))
                for message in fetched:
                    if begin <= message["created_at"] < end:
                        messages.append(message)
            self.storage.save_messages(source, messages, replace_range=(chat_ids, begin, end))
            count = len(messages)
        warnings = []
        if source == "feishu" and isinstance(self.feishu, FeishuClient):
            due = [chat for chat in chat_ids if time.monotonic() - self.member_attempts.get(chat, -100000) >= 600]
            if due:
                if progress:
                    progress(stage="members", message="消息已同步，正在核对成员名称。", completed=len(chat_ids), total=len(chat_ids))
                names = refresh_members(self.storage, self.feishu, source, due)
                warnings = names["warnings"]
                for chat in due:
                    self.member_attempts[chat] = time.monotonic()
        return {"synced_count": count, "total_count": self.storage.count(source), "warnings": warnings}

    def report_options(self, body, source, chat_ids, require_known_sender=False):
        use_llm = body.get("use_llm", False)
        if not isinstance(use_llm, bool):
            raise ValueError("use_llm 应为布尔值")
        sender_ids = ids_value(body.get("sender_ids", []), "发言人")
        audience = body.get("audience", "project")
        if not isinstance(audience, str) or audience not in {"personal", "project"}:
            raise ValueError("报告类型须为 personal 或 project")
        if audience == "personal":
            if not sender_ids:
                raise ValueError("个人汇报需要先选择本人的发言人身份")
            if "unknown" in sender_ids:
                raise ValueError("无法确认未知发言人的身份，不能用于个人汇报")
            if require_known_sender and any(not self.storage.has_sender(source, sender) for sender in sender_ids):
                raise ValueError("请先同步消息并选择已同步的本人发言人身份")
        period, anchor_date = body.get("period", "daily"), body.get("anchor_date")
        start, end = period_range(period, anchor_date)
        # Resolve timestamps before the first remote request, including date limits.
        timestamp(start)
        timestamp(end)
        if use_llm and not self.settings.llm_configured:
            raise ValueError("尚未配置模型。请先配置模型，或关闭 AI 使用免费规则整理。")
        composition = normalize_composition(body.get("composition"), use_llm=use_llm)
        preview_only = body.get("preview_only", False)
        if type(preview_only) is not bool:
            raise ValueError("preview_only 应为布尔值")
        return {"period": period, "anchor_date": anchor_date, "source": source, "chat_ids": list(chat_ids),
                "audience": audience, "sender_ids": list(sender_ids), "use_llm": use_llm,
                "composition": composition, "resolved_range": (start, end)}, start, end

    def _preview(self, report, parent_id=None):
        """Keep the exact server-generated candidate; previewing never writes a report."""
        now = datetime.now(timezone.utc)
        self.previews = {key: entry for key, entry in self.previews.items() if entry["expires"] > now}
        while len(self.previews) >= 20:
            self.previews.pop(next(iter(self.previews)))
        token = uuid.uuid4().hex
        expires = now + timedelta(minutes=30)
        candidate = deepcopy(report)
        # Bound in-memory drafts independently of the report DB and request size.
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > 4_000_000:
            raise ValueError("本次预览材料过多，请缩小消息范围后重试。")
        self.previews[token] = {"candidate": candidate, "parent_id": parent_id,
                                "expires": expires, "saved_id": None}
        presented = deepcopy(candidate)
        presented["is_preview"] = True
        presented["evidence_status"] = "snapshot"
        presented["reviewability"] = {"allowed": False, "reason": "请先保存此预览，再按条审阅。"}
        presented["recomposeability"] = {"allowed": False, "reason": "请先保存此预览。"}
        presented.setdefault("revision", {"version": 1, "review_status": "draft", "review_kind": "generated"})
        return {"preview": {"id": token, "expires_at": expires.isoformat(timespec="seconds"),
                            "report": presented}}

    def _preview_entry(self, body):
        token = body.get("preview_id")
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            raise ValueError("预览标识无效，请重新预览。")
        entry = self.previews.get(token)
        if not entry or entry["expires"] <= datetime.now(timezone.utc):
            raise ValueError("预览已过期或服务已重启，请重新预览后保存。")
        notes = body.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 2000:
            raise ValueError("修订说明最多 2000 字符")
        return entry

    def _saved_preview(self, body):
        entry = self._preview_entry(body)
        if entry["saved_id"]:
            report = self.storage.report(entry["saved_id"])
            if not report:
                raise ValueError("预览所保存的报告已不在当前工作空间，请重新预览。")
            return {"report": self.storage.enrich_report(report), "already_saved": True}
        return None

    def post(self, path, body):
        if path == "/api/jobs":
            action = body.get("action")
            if not isinstance(action, str) or action not in {"run", "sync"}:
                raise ValueError("任务应为 run 或 sync")
            source = source_value(body.get("source", "demo"))
            chats = ids_value(body.get("chat_ids", []), "群组", required=True)
            if action == "run":
                options, _, _ = self.report_options(body, source, chats, require_known_sender=True)
            else:
                start, last = parse_date(body.get("start_date")), parse_date(body.get("end_date"))
                if last < start or (last - start).days > 365:
                    raise ValueError("同步日期应按先后顺序，且一次不超过 366 天")
            payload = deepcopy(body)
            if action == "run":
                # Freeze values after validation, before the thread can yield to
                # other UI settings or a clock/date change. Never trust a
                # client-supplied definition/layout snapshot.
                payload.update(source=source, chat_ids=options["chat_ids"], sender_ids=options["sender_ids"],
                               audience=options["audience"], period=options["period"],
                               anchor_date=options["resolved_range"][0].isoformat(), use_llm=options["use_llm"],
                               composition=deepcopy(options["composition"]["config"]))
            return {"job": self.jobs.start(action, lambda progress: self._perform_post("/api/" + action, payload, progress))}
        if not self.operation_lock.acquire(blocking=False):
            raise OperationBusy(self.jobs.active_id)
        try:
            if path == "/api/composition/save":
                saved = self._saved_preview(body)
                if saved:
                    return saved
            return self._perform_post(path, body)
        finally:
            self.operation_lock.release()

    @staticmethod
    def _operation_action(path):
        actions = {"/api/identity": "save_identity", "/api/profile": "save_profile",
            "/api/preferences": "save_preferences", "/api/members/alias": "save_remark",
            "/api/members/refresh": "refresh_names", "/api/sync": "sync_messages",
            "/api/run": "sync_and_generate", "/api/reports": "generate_report",
            "/api/tasks": "link_task", "/api/backups": "create_backup",
            "/api/backups/restore": "restore_backup", "/api/demo/seed": "seed_demo",
            "/api/composition/preferences": "save_composition_preferences",
            "/api/composition/save": "save_report_preview"}
        if path in actions:
            return actions[path]
        if re.fullmatch(r"/api/reports/[a-f0-9]{32}/revisions", path):
            return "revise_report"
        if re.fullmatch(r"/api/reports/[a-f0-9]{32}/review", path):
            return "review_report"
        if re.fullmatch(r"/api/tasks/[a-f0-9]{32}/deactivate", path):
            return "unlink_task"
        return None

    def _operation_metadata(self, path, body, result=None):
        result = result if isinstance(result, dict) else {}
        data = {key: body.get(key) for key in ("source", "period", "audience", "start_date", "end_date")}
        data["chat_ids"] = body.get("chat_ids", [])
        if path == "/api/composition/preferences":
            data["source"] = body.get("source", "demo")
        if path == "/api/composition/save":
            entry = self._preview_entry(body)
            data.update({key: entry["candidate"].get(key) for key in
                         ("source", "chat_ids", "period", "audience", "start_date", "end_date")})
            if entry["parent_id"]:
                data["related_id"] = entry["parent_id"]
        if path == "/api/demo/seed":
            data["source"] = "demo"
        if path in {"/api/sync", "/api/run", "/api/reports", "/api/identity", "/api/profile", "/api/preferences",
                    "/api/members/alias", "/api/members/refresh", "/api/tasks"}:
            data["source"] = body.get("source", "demo")
        parent = re.fullmatch(r"/api/reports/([a-f0-9]{32})/(?:revisions|review)", path)
        if parent:
            data["related_id"] = parent.group(1)
            with self.storage.connect() as db:
                row = db.execute("SELECT source,chat_ids,period,audience,start_date,end_date FROM reports WHERE id=?", (parent.group(1),)).fetchone()
            if row:
                data.update(dict(row))
                data["chat_ids"] = json.loads(row["chat_ids"])
        report = result.get("report", {})
        if isinstance(report, dict) and report:
            data.update({key: report.get(key) for key in ("source", "chat_ids", "period", "audience", "start_date", "end_date")})
            data["report_id"] = report.get("id")
        elif path in {"/api/run", "/api/reports"}:
            try:
                start, end = period_range(body.get("period", "daily"), body.get("anchor_date"))
                data.update(start_date=start.isoformat(), end_date=(end - timedelta(days=1)).isoformat())
            except (ValueError, TypeError):
                pass
        composition = (report.get("composition") or {}).get("config", {}) if report else body.get("composition", {})
        if isinstance(composition, dict):
            for key in ("template_id", "framework_id"):
                data[key] = composition.get(key)
        source = data.get("source")
        if isinstance(source, str) and source in {"feishu", "demo"}:
            known_chats = {row["chat_id"] for row in self.storage.chats(source)}
            chats = data.get("chat_ids")
            data["chat_ids"] = [chat for chat in chats if isinstance(chat, str) and chat in known_chats] if isinstance(chats, list) else []
        else:
            data["source"], data["chat_ids"] = None, []
        counts = {key: result[key] for key in ("synced_count", "total_count", "message_count", "members_refreshed") if key in result}
        if report:
            counts["message_count"] = report.get("message_count")
            items = report.get("items", [])
            if isinstance(items, list):
                counts["review_item_count"] = sum(item.get("included", True) is True for item in items)
                counts["manual_item_count"] = sum(1 for item in items if item.get("included", True) is True and (item.get("origin") == "manual" or item.get("manual_text")))
        for key in ("backup", "restored"):
            backup = result.get(key)
            if isinstance(backup, dict):
                counts.update({k: v for k, v in backup.get("counts", {}).items() if k in {"messages", "reports"}})
                data["related_id"] = backup.get("id")
        group = result.get("group")
        if isinstance(group, dict):
            data["related_id"] = group.get("id")
        deactivated = re.fullmatch(r"/api/tasks/([a-f0-9]{32})/deactivate", path)
        if deactivated:
            data["related_id"] = deactivated.group(1)
        data["counts"] = counts
        if self.jobs.active_id:
            data["job_id"] = self.jobs.active_id
        if path == "/api/identity":
            data["display_mode"] = body.get("mode")
        return data

    def _perform_post(self, path, body, progress=None):
        action = self._operation_action(path)
        if path == "/api/run" and body.get("preview_only") is True:
            action = "sync_messages"
        if action is None:
            return self._post_unlocked(path, body, progress)
        scope = self._operation_metadata(path, body)
        try:
            receipt_id = self.operations.begin(action, scope)
        except (OSError, sqlite3.Error):
            raise ValueError("操作记录暂不可写，未开始本次操作。请检查本机数据目录。") from None
        try:
            result = self._post_unlocked(path, body, progress)
        except Exception as exc:
            code = exc.code if isinstance(exc, FeishuError) and type(exc.code) is int else (
                "invalid_input" if isinstance(exc, ValueError) else "model_error" if isinstance(exc, ModelError) else "internal_error")
            try:
                self.operations.finish(receipt_id, "failed", {**scope, "error_code": code})
            except (OSError, sqlite3.Error):
                print("本次操作未完成，操作回执未能保存。", file=sys.stderr)
            raise
        try:
            metadata = self._operation_metadata(path, body, result)
            status = "succeeded"
            if action == "refresh_names" and result.get("warnings"):
                status = "failed"
                metadata["error_code"] = result["warnings"][0].get("code") or "name_permission"
            self.operations.finish(receipt_id, status, metadata)
        except (OSError, sqlite3.Error):
            if isinstance(result, dict):
                result["operation_warning"] = "操作已经完成，但最终回执未能保存；请先核对当前结果，避免重复执行。"
        return result

    def _post_unlocked(self, path, body, progress=None):
        if path == "/api/composition/preferences":
            source = source_value(body.get("source", "demo"))
            validated = normalize_composition(body.get("composition"))
            config = self.storage.save_composition_preferences(source, validated["config"])
            return {"composition": config, "source": source}
        if path == "/api/composition/preview":
            source = source_value(body.get("source", "demo"))
            chats = ids_value(body.get("chat_ids", []), "群组", required=True)
            options, _, _ = self.report_options(body, source, chats)
            return self._preview(build_report(self.storage, self.settings, **options, persist=False))
        recompose = re.fullmatch(r"/api/reports/([a-f0-9]{32})/composition/preview", path)
        if recompose:
            candidate = self.storage.recompose_report(recompose.group(1), body.get("composition"))
            return self._preview(candidate, recompose.group(1))
        if path == "/api/composition/save":
            entry = self._preview_entry(body)
            if entry["parent_id"]:
                report = self.storage.save_composition_revision(entry["parent_id"], deepcopy(entry["candidate"]),
                                                               body.get("notes", ""))
                entry["saved_id"] = report["id"]
            else:
                report = self.storage.save_report(deepcopy(entry["candidate"]))
                # The commit has happened. Preserve idempotency even if the
                # following optional read-back temporarily fails.
                entry["saved_id"] = report["id"]
                try:
                    report = self.storage.enrich_report(report)
                except (OSError, sqlite3.Error):
                    report = dict(report, evidence_status="snapshot", is_preview=False,
                                  operation_warning="报告已保存，详情暂未读回；请刷新汇报记录核对，不要重复生成。")
            return {"report": report}
        if path == "/api/backups":
            return {"backup": self.storage.create_backup()}
        if path == "/api/backups/restore":
            restored = self.storage.restore_backup(body.get("backup_id"))
            self.member_attempts.clear()
            self.jobs.clear_finished()
            self.previews.clear()
            return {"restored": restored, "safety_backup": restored.get("safety_backup")}
        revision = re.fullmatch(r"/api/reports/([a-f0-9]{32})/revisions", path)
        if revision:
            return {"report": self.storage.revise_report(revision.group(1), body.get("markdown"), body.get("notes", ""))}
        review = re.fullmatch(r"/api/reports/([a-f0-9]{32})/review", path)
        if review:
            return {"report": self.storage.review_report(review.group(1), body.get("changes"), body.get("additions"), body.get("notes", ""))}
        deactivate = re.fullmatch(r"/api/tasks/([a-f0-9]{32})/deactivate", path)
        if deactivate:
            return self.storage.deactivate_task_group(deactivate.group(1))
        if path == "/api/demo/seed":
            return {"message_count": seed_demo(self.storage)}
        if path not in {"/api/sync", "/api/reports", "/api/profile", "/api/identity", "/api/preferences", "/api/run", "/api/members/refresh", "/api/members/alias", "/api/tasks"}:
            return None
        source = source_value(body.get("source", "demo"))
        if path == "/api/members/alias":
            return {"member": self.storage.save_member_alias(source, body.get("sender_id"), body.get("alias", ""))}
        if path == "/api/profile":
            return {"profile": self.storage.save_profile(source, body.get("sender_id"), body.get("nickname"))}
        if path == "/api/identity":
            return {"identity": self.storage.save_identity(source, body.get("sender_id"), body.get("mode"), body.get("nickname"))}
        if path == "/api/preferences":
            preferences = self.storage.save_preferences(source, body.get("chat_ids", []),
                body.get("period", "daily"), body.get("audience", "project"), body.get("sender_id", ""))
            return {"preferences": preferences, "last_source": self.storage.last_source(), "has_saved": True}
        chat_ids = ids_value(body.get("chat_ids", []), "群组", required=True)
        if path == "/api/members/refresh":
            result = refresh_members(self.storage, self.feishu, source, chat_ids)
            for chat in chat_ids:
                self.member_attempts[chat] = time.monotonic()
            return result
        if path == "/api/tasks":
            return {"group": self.storage.create_task_group(source, chat_ids, body.get("sender_id"), body.get("title"), body.get("message_ids"))}
        if path == "/api/sync":
            start = parse_date(body.get("start_date"))
            last = parse_date(body.get("end_date"))
            return self.sync(source, chat_ids, start, last, progress)
        options, start, end = self.report_options(body, source, chat_ids, require_known_sender=path == "/api/run")
        if path == "/api/run":
            synced = self.sync(source, chat_ids, start, end - timedelta(days=1), progress)
            if progress:
                progress(stage="reporting", message="同步完成，正在生成带来源的工作汇报。", completed=len(chat_ids), total=len(chat_ids))
            try:
                report = build_report(self.storage, self.settings, **options, persist=not body.get("preview_only", False))
            except ModelError as exc:
                raise ModelError(f"消息同步成功，但报告未完成：{exc} 可关闭 AI 后重新生成，或稍后重试。") from None
            except ValueError as exc:
                raise ValueError(f"消息同步成功，但报告未完成：{exc}") from None
            return {**(self._preview(report) if body.get("preview_only", False) else {"report": report}), **synced}
        report = build_report(self.storage, self.settings, **options, persist=not body.get("preview_only", False))
        return self._preview(report) if body.get("preview_only", False) else {"report": report}


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WorkSummary/" + VERSION

        def safe_host(self):
            port = self.server.server_port
            return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def send_bytes(self, status, content, content_type, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(content)

        def json_response(self, status, data):
            self.send_bytes(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def dispatch(self):
            if not self.safe_host():
                return self.json_response(403, {"error": "仅允许从本机地址访问"})
            parsed = urlsplit(self.path)
            if self.command == "GET":
                files = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
                         "/styles.css": ("styles.css", "text/css"), "/app.js": ("app.js", "text/javascript"),
                         "/features.js": ("features.js", "text/javascript"),
                         "/composition.js": ("composition.js", "text/javascript"),
                         "/workspace.js": ("workspace.js", "text/javascript")}
                if parsed.path in files:
                    name, mime = files[parsed.path]
                    content = (STATIC / name).read_bytes()
                    if name == "index.html":
                        content = content.replace(b"__WORK_SUMMARY_PAGE_BUILD__", page_build().encode("ascii"))
                        content = content.replace(b"__WORK_SUMMARY_INSTANCE_ID__", app.instance_id.encode("ascii"))
                    return self.send_bytes(200, content, mime + "; charset=utf-8")
                download = re.fullmatch(r"/api/reports/([a-f0-9]{32})/download", parsed.path)
                if download:
                    report = app.storage.report(download.group(1))
                    if not report:
                        return self.json_response(404, {"error": "报告不存在"})
                    filename = report["title"].replace("｜", "-") + ".md"
                    return self.send_bytes(200, report["markdown"].encode("utf-8"), "text/markdown; charset=utf-8",
                                           {"Content-Disposition": "attachment; filename=report.md; filename*=UTF-8''" + quote(filename)})
                result = app.get(parsed.path, parse_qs(parsed.query))
            else:
                origin = self.headers.get("Origin")
                allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
                if (origin and origin not in allowed) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                    return self.json_response(403, {"error": "不允许跨站操作"})
                if self.headers.get_content_type() != "application/json":
                    return self.json_response(415, {"error": "请求需要使用 application/json"})
                if self.headers.get("Transfer-Encoding"):
                    return self.json_response(400, {"error": "不支持该传输格式"})
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    return self.json_response(413, {"error": "请求内容为空或过大"})
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求内容应为 JSON 对象")
                result = app.post(parsed.path, body)
            return self.json_response(200 if result is not None else 404,
                                      result if result is not None else {"error": "未找到该接口"})

        def run_dispatch(self):
            try:
                self.dispatch()
            except OperationBusy as exc:
                self.json_response(409, {"error": str(exc), "job": app.jobs.get(exc.job_id) if exc.job_id else None})
            except (ValueError, OverflowError):
                # JSON decoder errors and validation messages contain no secrets.
                error = sys.exc_info()[1]
                message = "请求 JSON 格式无效" if isinstance(error, json.JSONDecodeError) else str(error)
                self.json_response(400, {"error": message or "请求参数无效"})
            except (FeishuError, ModelError) as exc:
                self.json_response(502, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception:
                print("服务内部错误，请检查本地配置或稍后重试。", file=sys.stderr)
                self.json_response(500, {"error": "服务内部错误，未完成本次操作。请检查本地配置或稍后重试。"})

        do_GET = run_dispatch
        do_POST = run_dispatch

    return Handler


def main():
    try:
        settings = load_settings()
        application = Application(settings)
        server = ThreadingHTTPServer(("127.0.0.1", settings.port), handler_for(application))
        # Only the process that successfully owns the listening socket may
        # recover old receipts. A duplicate launch must not alter live work.
        try:
            application.operations.recover_interrupted()
        except Exception:
            server.server_close()
            raise
    except (OSError, ValueError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"拾光 · 工作汇总已启动：http://127.0.0.1:{settings.port}", flush=True)
    print("仅本机访问；按 Ctrl+C 退出。首次使用请在页面载入示例数据。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
