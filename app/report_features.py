"""Immutable report evidence, manual revisions and explicitly linked work items."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import uuid


class ReportFeatures:
    def setup_report_schema(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS report_details (
                    report_id TEXT PRIMARY KEY, evidence TEXT NOT NULL, items TEXT NOT NULL,
                    template_version TEXT NOT NULL, task_timeline TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS report_revisions (
                    report_id TEXT PRIMARY KEY, root_report_id TEXT NOT NULL,
                    parent_report_id TEXT, version INTEGER NOT NULL,
                    review_status TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                    UNIQUE(root_report_id,version)
                );
                CREATE TABLE IF NOT EXISTS report_review_metadata (
                    report_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    included_count INTEGER NOT NULL DEFAULT 0, excluded_count INTEGER NOT NULL DEFAULT 0,
                    manual_count INTEGER NOT NULL DEFAULT 0, rewritten_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS report_compositions (
                    report_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_groups (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, sender_id TEXT NOT NULL,
                    title TEXT NOT NULL, chat_ids TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_links (
                    group_id TEXT NOT NULL, message_id TEXT NOT NULL, event TEXT NOT NULL,
                    PRIMARY KEY(group_id,message_id)
                );
            """)

    @staticmethod
    def _save_report_composition(db, report_id, composition):
        if not isinstance(composition, dict):
            raise ValueError("报告组合快照应为对象")
        try:
            snapshot = json.dumps(composition, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError("报告组合快照格式无效") from None
        db.execute("INSERT INTO report_compositions(report_id,snapshot) VALUES(?,?)", (report_id, snapshot))

    def attach_report_details(self, report_id, evidence, items, template_version="2", task_timeline=None):
        """Insert once: resyncs and profile changes must never rewrite old evidence."""
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM reports WHERE id=?", (report_id,)).fetchone():
                raise ValueError("报告不存在")
            db.execute("""INSERT OR IGNORE INTO report_details
                (report_id,evidence,items,template_version,task_timeline) VALUES(?,?,?,?,?)""",
                (report_id, json.dumps(evidence, ensure_ascii=False), json.dumps(items, ensure_ascii=False),
                 str(template_version), json.dumps(task_timeline or [], ensure_ascii=False)))

    def enrich_report(self, report, include_snapshot=True):
        if report is None:
            return None
        result = dict(report)
        with self.connect() as db:
            detail_columns = "*" if include_snapshot else "template_version,length(items)>2 AS has_items"
            details = db.execute(f"SELECT {detail_columns} FROM report_details WHERE report_id=?", (result["id"],)).fetchone()
            revision = db.execute("SELECT * FROM report_revisions WHERE report_id=?", (result["id"],)).fetchone()
            review = db.execute("SELECT * FROM report_review_metadata WHERE report_id=?", (result["id"],)).fetchone()
            if include_snapshot:
                composition = db.execute("SELECT snapshot FROM report_compositions WHERE report_id=?", (result["id"],)).fetchone()
            else:
                # History needs labels only; never bring report materials or
                # potentially large immutable layout/evidence into the list.
                fields = (("schema_version", "schema_version"), ("renderer_version", "renderer_version"),
                          ("template.id", "template_id"), ("template.label", "template_label"),
                          ("framework.id", "framework_id"), ("framework.label", "framework_label"),
                          ("purpose.id", "purpose_id"), ("purpose.label", "purpose_label"))
                columns = ",".join("json_extract(snapshot,'$." + path + "') AS " + alias for path, alias in fields)
                composition = db.execute("SELECT " + columns + " FROM report_compositions WHERE report_id=?",
                                         (result["id"],)).fetchone()
        if composition and include_snapshot:
            result["composition"] = json.loads(composition[0])
        elif composition:
            result["composition"] = {"schema_version": composition["schema_version"],
                "renderer_version": composition["renderer_version"],
                "config": {"template_id": composition["template_id"], "framework_id": composition["framework_id"],
                           "purpose": composition["purpose_id"]},
                **{key: {"id": composition[key + "_id"], "label": composition[key + "_label"]}
                   for key in ("template", "framework", "purpose")}}
        else:
            result["composition"] = None
        result["composition_status"] = "snapshot" if composition else "legacy"
        if details:
            from .reporting import review_items
            result.pop("evidence_notice", None)
            result.update(evidence=json.loads(details["evidence"]) if include_snapshot else [],
                          items=review_items(json.loads(details["items"])) if include_snapshot else [],
                          template_version=details["template_version"],
                          task_timeline=json.loads(details["task_timeline"]) if include_snapshot else [],
                          evidence_status="snapshot")
        else:
            # Never reconstruct historical evidence from today's mutable message cache.
            result.update(evidence=[], items=[], task_timeline=[], evidence_status="legacy_unavailable",
                          evidence_notice="此历史报告没有结构化来源快照，请核对其原始消息来源附录。")
        result["revision"] = dict(revision) if revision else {
            "report_id": result["id"], "root_report_id": result["id"], "parent_report_id": None,
            "version": 1, "review_status": "draft", "notes": ""}
        kind = review["kind"] if review else ("freeform" if result["revision"]["review_status"] == "manual_revision" else "generated")
        result["review_kind"] = kind
        result["revision"]["review_kind"] = kind
        has_items = bool(result["items"]) if include_snapshot else bool(details and details["has_items"])
        if result.get("mode") != "extractive":
            allowed, reason = False, "AI 报告的消息分项不代表 AI 正文结构，请使用自由编辑另存修订稿。"
        elif not details or not has_items:
            allowed, reason = False, "此报告没有可用的结构化条目快照，请使用自由编辑另存修订稿。"
        elif kind == "freeform":
            allowed, reason = False, "此报告已经过自由文本编辑，原始分项不能代表当前正文，请继续使用自由编辑。"
        else:
            allowed, reason = True, "可按条确认保留、分类及人工补充，另存新版本。"
        result["reviewability"] = {"allowed": allowed, "reason": reason}
        result["recomposeability"] = {"allowed": allowed, "reason": (
            "可使用原报告的来源快照调整模板或表达框架，预览后另存新版本。" if allowed else reason)}
        result["review_summary"] = {key: review[key] for key in (
            "included_count", "excluded_count", "manual_count", "rewritten_count")} if review and kind in {"structured_review", "composition_revision"} else None
        return result

    def revise_report(self, report_id, markdown, notes=""):
        if not isinstance(markdown, str) or not markdown.strip() or len(markdown) > 200000:
            raise ValueError("修订正文应为非空文本，最多 200000 字符")
        if not isinstance(notes, str) or len(notes) > 2000:
            raise ValueError("修订备注最多 2000 字符")
        original = self.report(report_id)
        if not original:
            raise ValueError("报告不存在")
        original = self.enrich_report(original)
        markdown = "> 人工修订稿，可能含人工补充，请以原稿及来源快照核对；引用编号不代表新增文字已获自动证实。\n\n" + markdown.strip()
        return self._persist_revision(original, markdown, notes, original["items"], "freeform")

    def _persist_revision(self, original, markdown, notes, items, kind, candidate=None):
        """Save a revision, source snapshots and review state in one transaction."""
        root_id = original["revision"]["root_report_id"]
        root = self.report(root_id) or original
        with self.connect() as db:
            # A revision and its immutable snapshots/lineage commit together.
            # Serializing version assignment also prevents concurrent v2 siblings.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT MAX(version) FROM report_revisions WHERE root_report_id=?", (root_id,)).fetchone()
            version = max(row[0] or 1, 1) + 1
            title = (candidate["title"] if candidate is not None else
                     re.sub(r"[<>\x00-\x1f]", "", root["title"])[:180] + f"（修订 v{version}）")
            if kind == "structured_review":
                markdown = re.sub(r"(?m)^# [^\n]*", "# " + title, markdown, count=1)
            saved = dict(original, id=candidate["id"] if candidate is not None else uuid.uuid4().hex, title=title,
                         created_at=candidate["created_at"] if candidate is not None else datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         markdown=markdown, items=deepcopy(items))
            saved["composition_status"] = "snapshot" if saved.get("composition") is not None else "legacy"
            columns = ("id", "title", "period", "start_date", "end_date", "source", "mode", "audience",
                       "chat_ids", "sender_ids", "message_count", "markdown", "created_at", "coverage")
            values = [json.dumps(saved.get(column, {}), ensure_ascii=False) if column in {"chat_ids", "sender_ids", "coverage"}
                      else saved[column] for column in columns]
            db.execute(f"INSERT INTO reports({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)
            if original["evidence_status"] == "snapshot":
                db.execute("""INSERT INTO report_details(report_id,evidence,items,template_version,task_timeline)
                    VALUES(?,?,?,?,?)""", (saved["id"], json.dumps(original["evidence"], ensure_ascii=False),
                    json.dumps(items, ensure_ascii=False), original.get("template_version", "2"),
                    json.dumps(original.get("task_timeline", []), ensure_ascii=False)))
            if original.get("composition") is not None:
                self._save_report_composition(db, saved["id"], original["composition"])
            db.execute("""INSERT OR IGNORE INTO report_revisions
                (report_id,root_report_id,parent_report_id,version,review_status,notes) VALUES(?,?,NULL,1,'draft','')""",
                (root_id, root_id))
            db.execute("""INSERT INTO report_revisions
                (report_id,root_report_id,parent_report_id,version,review_status,notes) VALUES(?,?,?,?,?,?)""",
                (saved["id"], root_id, original["id"], version, "manual_revision", notes.strip()))
            included = [item for item in items if item.get("included", True)]
            db.execute("""INSERT INTO report_review_metadata
                (report_id,kind,included_count,excluded_count,manual_count,rewritten_count) VALUES(?,?,?,?,?,?)""",
                (saved["id"], kind, len(included), len(items) - len(included),
                 sum(item.get("origin") == "manual" for item in included),
                 sum(item.get("origin", "message") == "message" and item.get("manual_text", False) for item in included)))
            saved.update(revision={"report_id": saved["id"], "root_report_id": root_id,
                "parent_report_id": original["id"], "version": version, "review_status": "manual_revision",
                "notes": notes.strip(), "review_kind": kind}, review_kind=kind)
            saved["review_summary"] = ({"included_count": len(included), "excluded_count": len(items) - len(included),
                "manual_count": sum(item.get("origin") == "manual" for item in included),
                "rewritten_count": sum(item.get("origin", "message") == "message" and item.get("manual_text", False) for item in included)}
                if kind in {"structured_review", "composition_revision"} else None)
            if kind == "freeform":
                reason = "此报告已经过自由文本编辑，原始分项不能代表当前正文，请继续使用自由编辑。"
                saved["reviewability"] = saved["recomposeability"] = {"allowed": False, "reason": reason}
        try:
            return self.enrich_report(saved)
        except Exception:
            # The write already committed. Return its known ID and immutable data
            # so callers can acknowledge success instead of creating a duplicate.
            saved["operation_warning"] = "报告已保存，但刷新报告详情未完成；请按报告标识核对，避免重复另存。"
            return saved

    def recompose_report(self, report_id, composition):
        """Build an unsaved candidate using only the immutable parent report."""
        from .composition import normalize_composition, render_composed_report
        original = self.report(report_id)
        if not original:
            raise ValueError("报告不存在")
        if not original["recomposeability"]["allowed"]:
            raise ValueError(original["recomposeability"]["reason"])
        snapshot = normalize_composition(composition)
        if original.get("composition"):
            layout = deepcopy(original["composition"].get("layout", {}))
        else:
            # The compatibility branch is only for immutable classic reports.
            # New layouts must never be inferred from user-customizable headings.
            markdown = original["markdown"]
            start = re.search(r"(?m)^## 本期成果\s*$", markdown)
            appendix = re.search(r"(?m)^## 统计与覆盖说明\s*$", markdown)
            if not start or not appendix or start.start() >= appendix.start():
                raise ValueError("原报告缺少可核对的经典模板结构，请使用自由编辑另存")
            timeline = re.search(r"(?m)^## 事项进展时间线\s*$", markdown[start.start():appendix.start()])
            timeline_start = start.start() + timeline.start() if timeline else appendix.start()
            layout = {"header": markdown[:start.start()].rstrip(),
                      "appendix": markdown[appendix.start():].rstrip(),
                      "legacy_body": markdown[start.start():timeline_start].rstrip(),
                      "timeline": markdown[timeline_start:appendix.start()].rstrip()}
        layout.setdefault("source_item_baseline", {item["item_id"]: {
            "text": item["text"], "category": item["category"], "refs": list(item.get("refs", []))}
            for item in original["items"]})
        snapshot["layout"] = layout
        candidate = deepcopy(original)
        root = self.report(original["revision"]["root_report_id"]) or original
        title = re.sub(r"[<>\x00-\x1f]", "", root["title"])[:180] + "（配置修订）"
        snapshot["layout"]["header"] = re.sub(r"(?m)^# [^\n]*", "# " + title, layout["header"], count=1)
        candidate.update(id=uuid.uuid4().hex, title=title,
                         created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         composition=snapshot, composition_status="snapshot", _composition_parent_id=report_id)
        reviewed = any(not item.get("included", True) or item.get("manual_text", False)
                       or item.get("category") != item.get("original_category", item.get("category"))
                       for item in original["items"])
        candidate["markdown"] = render_composed_report(candidate, reviewed=reviewed)
        return candidate

    def save_composition_revision(self, parent_report_id, candidate, notes=""):
        """Persist a server-owned preview without refreshing its frozen evidence."""
        if not isinstance(notes, str) or len(notes) > 2000:
            raise ValueError("修订备注最多 2000 字符")
        parent = self.report(parent_report_id)
        if not parent:
            raise ValueError("报告不存在")
        if not parent["recomposeability"]["allowed"]:
            raise ValueError(parent["recomposeability"]["reason"])
        if not isinstance(candidate, dict) or candidate.get("_composition_parent_id") != parent_report_id:
            raise ValueError("组合预览与原报告不匹配，请重新预览")
        if not isinstance(candidate.get("id"), str) or not re.fullmatch(r"[a-f0-9]{32}", candidate["id"]) or candidate["id"] == parent_report_id:
            raise ValueError("组合预览标识无效，请重新预览")
        immutable = ("source", "mode", "audience", "period", "start_date", "end_date", "chat_ids",
                     "sender_ids", "message_count", "coverage", "evidence", "items", "task_timeline", "template_version")
        if any(candidate.get(key) != parent.get(key) for key in immutable):
            raise ValueError("组合预览不能改动原报告的来源、作者或事项，请重新预览")
        if (not isinstance(candidate.get("composition"), dict) or not isinstance(candidate.get("markdown"), str)
                or not candidate["markdown"].strip() or not isinstance(candidate.get("title"), str)
                or not isinstance(candidate.get("created_at"), str)):
            raise ValueError("组合预览缺少完整正文或配置，请重新预览")
        existing = self.report(candidate["id"])
        if existing:
            if (existing["markdown"] == candidate["markdown"] and existing["composition"] == candidate["composition"]
                    and existing["revision"]["parent_report_id"] == parent_report_id
                    and existing["review_kind"] == "composition_revision"):
                return existing
            raise ValueError("组合预览标识已被其他报告使用，请重新预览")
        frozen = dict(parent, composition=deepcopy(candidate["composition"]))
        return self._persist_revision(frozen, candidate["markdown"], notes, parent["items"], "composition_revision", candidate)

    def review_report(self, report_id, changes, additions, notes=""):
        from .reporting import REVIEW_CATEGORIES, attribution_warning, render_review_report, review_items
        if not isinstance(changes, list) or len(changes) > 500 or not isinstance(additions, list) or len(additions) > 100:
            raise ValueError("审阅修改最多 500 条，人工新增最多 100 条")
        if not isinstance(notes, str) or len(notes) > 2000:
            raise ValueError("修订备注最多 2000 字符")
        original = self.report(report_id)
        if not original:
            raise ValueError("报告不存在")
        original = self.enrich_report(original)
        if not original["reviewability"]["allowed"]:
            raise ValueError(original["reviewability"]["reason"])
        items = review_items(original["items"])
        by_id = {item["item_id"]: item for item in items}
        changed, seen = False, set()

        def category_value(value):
            if not isinstance(value, str) or value not in REVIEW_CATEGORIES:
                raise ValueError("条目分类须为 completed、progress、risk、plan 或 other")
            return REVIEW_CATEGORIES[value]

        def reviewed_status(category):
            return {"完成与交付": "completed", "进行中的工作": "in_progress", "风险与待协调": "blocked",
                    "后续计划": "planned", "其他工作记录": "unspecified"}[category]

        def text_value(value):
            if not isinstance(value, str) or not value.strip() or len(value) > 10000:
                raise ValueError("条目正文应为非空文本，最多 10000 字符")
            if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
                raise ValueError("条目正文不能包含控制字符")
            return value.strip()

        for change in changes:
            if not isinstance(change, dict) or set(change) - {"item_id", "included", "category", "text"}:
                raise ValueError("条目修改字段无效，不能修改作者或来源引用")
            identity = change.get("item_id")
            if not isinstance(identity, str) or identity not in by_id or identity in seen:
                raise ValueError("条目标识不存在或重复")
            seen.add(identity)
            if type(change.get("included")) is not bool:
                raise ValueError("included 应为布尔值")
            category = category_value(change.get("category"))
            item = by_id[identity]
            text = text_value(change["text"]) if "text" in change else item["text"]
            changed = changed or (item["included"], item["category"], item["text"]) != (change["included"], category, text)
            item.update(included=change["included"], category=category, text=text,
                        review_category=change["category"], manual_text=item["origin"] == "manual" or text != item["original_text"],
                        attribution_warning=attribution_warning(text), status=reviewed_status(category))
        for addition in additions:
            if not isinstance(addition, dict) or set(addition) != {"text", "category"}:
                raise ValueError("人工新增条目只接受正文和分类")
            text, category = text_value(addition["text"]), category_value(addition["category"])
            items.append({"item_id": "manual-" + uuid.uuid4().hex, "included": True, "text": text,
                          "original_text": text, "category": category, "original_category": category,
                          "review_category": addition["category"], "origin": "manual", "manual_text": True,
                          "status": reviewed_status(category),
                          "refs": [], "ref": "", "sender_id": "", "sender_name": "人工补充", "chat_id": "",
                          "attribution_warning": attribution_warning(text)})
            changed = True
        if not changed:
            raise ValueError("未检测到实际修改，请调整条目后另存")
        if not any(item["included"] for item in items):
            raise ValueError("至少保留或新增一条纳入的条目，不能生成空白审阅稿")
        if sum(len(item["text"]) for item in items) > 200000:
            raise ValueError("审阅条目正文合计最多 200000 字符")
        from .reporting import REPORT_SECTIONS
        labels = dict(REPORT_SECTIONS)
        for item in items:
            item["category_label"] = labels[item["category"]]
        return self._persist_revision(original, render_review_report(original, items), notes, items, "structured_review")

    def create_task_group(self, source, chat_ids, sender_id, title, message_ids):
        if source not in {"demo", "feishu"}:
            raise ValueError("数据来源应为 demo 或 feishu")
        if not isinstance(chat_ids, list) or not chat_ids or len(chat_ids) > 50 or any(not isinstance(c, str) for c in chat_ids):
            raise ValueError("请明确选择事项所属群")
        if not isinstance(sender_id, str) or not sender_id or sender_id == "unknown":
            raise ValueError("请明确选择同一位发言人")
        if not isinstance(title, str) or not title.strip() or len(title) > 100 or re.search(r"[<>\x00-\x1f]", title):
            raise ValueError("事项标题须为 1–100 字普通文本")
        if not isinstance(message_ids, list) or any(not isinstance(mid, str) for mid in message_ids):
            raise ValueError("请选择至少两条不同消息")
        ids = list(dict.fromkeys(message_ids))
        if not 2 <= len(ids) <= 500:
            raise ValueError("请选择 2–500 条不同消息")
        available = {m["message_id"]: m for m in self.messages(source, chat_ids=chat_ids)}
        if any(mid not in available for mid in ids):
            raise ValueError("所选消息不属于当前数据来源或所选群")
        messages = [available[mid] for mid in ids]
        if any(m["sender_id"] != sender_id for m in messages):
            raise ValueError("不能跨发言人合并事项；请分别选择同一人的消息")
        from .reporting import classify, event_status, readable_members
        messages = readable_members(sorted(messages, key=lambda m: (m["created_at"], m["message_id"])))
        events = [dict(message_id=m["message_id"], chat_id=m["chat_id"], chat_name=m["chat_name"],
                       sender_id=m["sender_id"], sender_name=m["sender_name"], created_at=m["created_at"],
                       text=m["text"], category=classify(m["text"]), status=event_status(m["text"])) for m in messages]
        group_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO task_groups(id,source,sender_id,title,chat_ids,created_at) VALUES(?,?,?,?,?,?)",
                (group_id, source, sender_id, title.strip(), json.dumps(list(dict.fromkeys(chat_ids))),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            db.executemany("INSERT INTO task_links(group_id,message_id,event) VALUES(?,?,?)",
                [(group_id, event["message_id"], json.dumps(event, ensure_ascii=False)) for event in events])
        return next(group for group in self.task_groups(source)["groups"] if group["id"] == group_id)

    def task_groups(self, source, chat_ids=None):
        if source not in {"demo", "feishu"}:
            raise ValueError("数据来源应为 demo 或 feishu")
        selected = set(chat_ids or [])
        with self.connect() as db:
            rows = db.execute("SELECT * FROM task_groups WHERE source=? ORDER BY created_at,id", (source,)).fetchall()
            groups = []
            for row in rows:
                group = dict(row)
                group["chat_ids"] = json.loads(group["chat_ids"])
                if selected and not selected.intersection(group["chat_ids"]):
                    continue
                events = [json.loads(link["event"]) for link in db.execute(
                    "SELECT event FROM task_links WHERE group_id=?", (group["id"],))]
                group["events"] = sorted((e for e in events if not selected or e["chat_id"] in selected),
                                         key=lambda e: (e["created_at"], e["message_id"]))
                group["sender_name"] = group["events"][0]["sender_name"] if group["events"] else "未命名成员"
                group["active"] = bool(group["active"])
                groups.append(group)
        return {"groups": groups}

    def deactivate_task_group(self, group_id):
        with self.connect() as db:
            result = db.execute("UPDATE task_groups SET active=0 WHERE id=?", (group_id,))
            if not result.rowcount:
                raise ValueError("事项不存在")
        return {"id": group_id, "active": False}
