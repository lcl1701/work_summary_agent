"""Built-in report composition: saved definitions, explicit human statements, no inference."""

from copy import deepcopy
import re


SCHEMA_VERSION = 1
RENDERER_VERSION = 1
_CATEGORIES = {"completed": "完成与交付", "progress": "进行中的工作", "risk": "风险与待协调",
               "plan": "后续计划", "other": "其他工作记录"}
_PURPOSES = [
    ("standard", "常规汇报", "整理当前记录，不补造分析或承诺。", []),
    ("update", "进展同步", "说明目前进展与后续工作。", []),
    ("recommendation", "提出建议", "人工说明建议、条件与希望采取的行动。", ["desired_action"]),
    ("decision", "请求决策", "明确需要对方决定什么，不自动推断审批结论。", ["desired_action"]),
    ("escalation", "风险升级", "说明需要协调的事项与明确请求。", ["desired_action"]),
    ("reflection", "经验复盘", "区分实际经过、人工解释与未来行动。", []),
]
_FIELD_LABELS = {
    "recipient": "沟通对象", "familiarity": "对背景的熟悉程度", "desired_action": "希望对方采取的行动",
    "analysis_condition": "分析前提与适用条件", "counter_evidence": "反面证据与待验证事项",
    "key_message": "核心结论或请求", "point": "观点", "reason": "理由", "example": "例证",
    "situation": "共同背景／现状", "complication": "变化与困难", "question": "需要回答的问题",
    "answer": "建议答案", "alternatives": "其他选项", "constraints": "限制条件",
    "background": "必要背景", "assessment": "当前判断", "request": "建议或协调请求", "deadline": "希望完成或响应的时间",
    "expectation": "原先目标或预期", "meaning": "意义、学习与其他解释", "next_action": "下一步改进动作",
    "owner": "行动负责人", "status": "项目状态的人工判断", "milestones": "里程碑说明",
    "dependencies": "依赖与协作条件", "decisions": "待决策事项",
}
_COMMON = ["recipient", "familiarity", "desired_action", "analysis_condition", "counter_evidence"]


def _section(identity, label, categories=None):
    return {"id": identity, "label": label, "categories": categories or [_CATEGORIES[identity]],
            "required": identity == "risk", "hideable": identity == "other"}


_TEMPLATES = [
    {"id": "classic", "label": "经典四节", "description": "沿用成果、进展、风险、计划的既有报告。", "field_ids": [],
     "sections": [_section("completed", "本期成果"), _section("progress", "工作进展"), _section("risk", "风险与待协调"),
                  _section("plan", "下一步计划"), _section("other", "补充记录")]},
    {"id": "ppp", "label": "PPP 进展／计划／问题", "description": "将明确成果和进行中记录放在进展中，原状态与原话保持。", "field_ids": [],
     "sections": [_section("progress", "进展与成果", [_CATEGORIES["completed"], _CATEGORIES["progress"]]),
                  _section("plan", "后续计划"), _section("risk", "问题与风险"), _section("other", "补充记录")]},
    {"id": "project", "label": "项目进展", "description": "围绕交付、进展、风险与行动整理；状态等项目材料由使用者填写。",
     "field_ids": ["status", "milestones", "dependencies", "decisions"],
     "sections": [_section("completed", "里程碑与交付"), _section("progress", "当前进展"), _section("risk", "风险与依赖"),
                  _section("plan", "下一步行动"), _section("other", "补充记录")]},
    {"id": "reflection", "label": "工作复盘", "description": "保留经过和结果；目标、解释、负责人和改进计划均为人工材料。",
     "field_ids": ["expectation", "meaning", "next_action", "owner", "deadline"],
     "sections": [_section("completed", "本期结果"), _section("progress", "过程与尝试"), _section("risk", "问题与风险"),
                  _section("plan", "后续行动"), _section("other", "补充记录")]},
]
_FRAMEWORKS = [
    {"id": "none", "label": "不附加表达框架", "description": "直接展示选定内容模板。", "field_ids": [], "required_field_ids": []},
    {"id": "bluf", "label": "BLUF 先给要点", "description": "先给人工确认的核心消息，再核对事实材料。",
     "field_ids": ["key_message"], "required_field_ids": ["key_message"]},
    {"id": "prep", "label": "PREP 观点／理由／例证", "description": "观点、理由和例证由使用者提供；不自动证明因果或收益。",
     "field_ids": ["point", "reason", "example"], "required_field_ids": ["point", "reason", "example"]},
    {"id": "scqa", "label": "SCQA 背景／困难／问题／答案", "description": "组织建议材料，其他选项与限制不会从群消息自动推断。",
     "field_ids": ["situation", "complication", "question", "answer", "alternatives", "constraints"],
     "required_field_ids": ["situation", "complication", "question", "answer"]},
    {"id": "sbar", "label": "SBAR 现状／背景／判断／请求", "description": "用于协调和及时响应，判断与请求均需使用者核对。",
     "field_ids": ["situation", "background", "assessment", "request", "deadline"],
     "required_field_ids": ["situation", "background", "assessment", "request", "deadline"]},
    {"id": "wswnw", "label": "What／So What／Now What", "description": "事实材料与人工反思分开，行动责任和时间须明确填写。",
     "field_ids": ["expectation", "meaning", "next_action", "owner", "deadline"],
     "required_field_ids": ["expectation", "meaning", "next_action", "owner", "deadline"]},
]
_STEPS = {
    "none": [],
    "bluf": [{"label": "核心结论或请求", "field_ids": ["key_message"]}],
    "prep": [{"label": "观点（P）", "field_ids": ["point"]}, {"label": "理由（R）", "field_ids": ["reason"]},
             {"label": "例证（E）", "field_ids": ["example"]}, {"label": "重申观点（P）", "field_ids": ["point"]}],
    "scqa": [{"label": "共同背景（S）", "field_ids": ["situation"]}, {"label": "变化与困难（C）", "field_ids": ["complication"]},
             {"label": "需要回答的问题（Q）", "field_ids": ["question"]}, {"label": "建议答案（A）", "field_ids": ["answer"]},
             {"label": "其他选项与限制", "field_ids": ["alternatives", "constraints"], "optional": True}],
    "sbar": [{"label": "现状（S）", "field_ids": ["situation"]}, {"label": "必要背景（B）", "field_ids": ["background"]},
             {"label": "当前判断（A）", "field_ids": ["assessment"]}, {"label": "建议或请求（R）", "field_ids": ["request", "deadline"]}],
    "wswnw": [{"label": "What：发生了什么", "field_ids": ["expectation"],
                "notice": "实际经过、结果与作者见下方本期事实记录；本节不从消息自动推断成效或原因。"},
               {"label": "So What：意义与学习", "field_ids": ["meaning"]},
               {"label": "Now What：后续改进", "field_ids": ["next_action", "owner", "deadline"]}],
}
_ALLOWED_PURPOSES = {
    "none": [p[0] for p in _PURPOSES],
    "bluf": ["standard", "update", "recommendation", "decision", "escalation"],
    "prep": ["recommendation", "decision"], "scqa": ["recommendation", "decision"],
    "sbar": ["escalation", "decision"], "wswnw": ["reflection", "update"],
}


def catalog():
    fields = [{"id": identity, "label": label, "description": "由使用者填写的陈述，不自动视为消息证实的结论。",
               "placeholder": "请填写；缺少必要材料时报告显示待补充", "required": False,
               "max_length": 2000, "origin": "manual"} for identity, label in _FIELD_LABELS.items()]
    templates = [dict(template, version=1, required_field_ids=template["field_ids"] if template["id"] in {"project", "reflection"} else []) for template in _TEMPLATES]
    frameworks = [dict(framework, version=1, steps=_STEPS[framework["id"]]) for framework in _FRAMEWORKS]
    return deepcopy({"schema_version": SCHEMA_VERSION, "renderer_version": RENDERER_VERSION,
        "templates": templates, "frameworks": frameworks,
        "purposes": [{"id": identity, "version": 1, "label": label, "description": description,
                      "field_ids": [], "required_field_ids": required} for identity, label, description, required in _PURPOSES],
        "fields": fields, "common_field_ids": _COMMON,
        "combinations": [{"template_id": template["id"], "framework_id": framework["id"],
                          "purposes": _ALLOWED_PURPOSES[framework["id"]]} for template in _TEMPLATES for framework in _FRAMEWORKS],
        "limits": {"max_field_length": 2000, "max_fields_total": 16000, "max_evidence_items": 20},
        "combination_note": "首批采用目录列出的框架与用途组合；所有内容模板可使用这些组合，未列组合暂不支持。"})


def is_snapshot(value):
    return (isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION
            and value.get("renderer_version") == RENDERER_VERSION
            and all(isinstance(value.get(key), dict) for key in ("config", "template", "framework", "purpose", "layout"))
            and isinstance(value.get("field_definitions"), list))


def normalize_composition(value, use_llm=False):
    if value is None:
        value = {}
    allowed = {"schema_version", "template_id", "framework_id", "purpose", "fields", "section_order", "hidden_sections", "evidence_item_ids"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("表达配置字段无效，只接受内置模板、框架、用途与指定材料字段")
    if type(value.get("schema_version", 1)) is not int or value.get("schema_version", 1) != 1:
        raise ValueError("表达配置版本不支持")
    directory = catalog()
    selected = []
    for key, collection, default in (("template_id", "templates", "classic"), ("framework_id", "frameworks", "none"), ("purpose", "purposes", "standard")):
        identity = value.get(key, default)
        if not isinstance(identity, str):
            raise ValueError("模板、框架与用途须使用目录中的标识")
        definition = next((entry for entry in directory[collection] if entry["id"] == identity), None)
        if definition is None:
            raise ValueError("模板、框架或用途不在当前内置目录中")
        selected.append(definition)
    template, framework, purpose = selected
    if purpose["id"] not in _ALLOWED_PURPOSES[framework["id"]]:
        raise ValueError("此框架与用途组合暂不支持，请按目录选择")
    field_ids = list(dict.fromkeys(_COMMON + template["field_ids"] + framework["field_ids"] + purpose["field_ids"]))
    fields = value.get("fields", {})
    if not isinstance(fields, dict) or set(fields) - set(field_ids):
        raise ValueError("人工材料含有不属于当前模板或框架的字段")
    for text in fields.values():
        if not isinstance(text, str) or len(text) > 2000 or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
            raise ValueError("每个人工材料字段须为最多 2000 字符的文本，不含控制字符")
    fields = {identity: fields.get(identity, "").strip() for identity in field_ids}
    if sum(map(len, fields.values())) > 16000:
        raise ValueError("人工材料合计最多 16000 字符")
    default_order = [section["id"] for section in template["sections"]]
    order = value.get("section_order", default_order)
    if not isinstance(order, list) or any(not isinstance(entry, str) for entry in order) or len(order) != len(default_order) or set(order) != set(default_order):
        raise ValueError("章节顺序必须是该模板全部章节的唯一排列")
    hidden = value.get("hidden_sections", [])
    if not isinstance(hidden, list) or any(entry != "other" for entry in hidden) or len(hidden) > 1:
        raise ValueError("首版仅允许隐藏补充记录，风险章节不可隐藏")
    evidence_ids = value.get("evidence_item_ids", [])
    if not isinstance(evidence_ids, list) or len(evidence_ids) > 20 or any(not isinstance(entry, str) or not entry or len(entry) > 100 for entry in evidence_ids) or len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("核对材料应为最多 20 个不同的本报告条目标识")
    config = {"schema_version": 1, "template_id": template["id"], "framework_id": framework["id"], "purpose": purpose["id"],
              "fields": fields, "section_order": list(order), "hidden_sections": list(hidden), "evidence_item_ids": list(evidence_ids)}
    if use_llm and not (template["id"] == "classic" and framework["id"] == "none" and purpose["id"] == "standard"
            and order == default_order and not hidden and not evidence_ids and not any(fields.values())):
        raise ValueError("新模板和表达组合目前使用免费规则与人工材料，请关闭 AI；经典默认 AI 路径仍可使用")
    required = set(template["required_field_ids"] + framework["required_field_ids"] + purpose["required_field_ids"])
    definitions = {entry["id"]: entry for entry in directory["fields"]}
    field_definitions = [dict(definitions[identity], required=identity in required) for identity in field_ids]
    return {"schema_version": 1, "renderer_version": 1, "config": config, "template": template,
            "framework": framework, "purpose": purpose, "field_definitions": field_definitions, "layout": {}}


def preference_config(value):
    snapshot = normalize_composition(value["config"] if is_snapshot(value) else value)
    return {key: deepcopy(snapshot["config"][key]) for key in
            ("schema_version", "template_id", "framework_id", "purpose", "section_order", "hidden_sections")}


def _default_layout(snapshot):
    config = snapshot["config"]
    return (config["template_id"] == "classic" and config["framework_id"] == "none" and config["purpose"] == "standard"
            and config["section_order"] == [section["id"] for section in snapshot["template"]["sections"]]
            and not config["hidden_sections"] and not config["evidence_item_ids"] and not any(config["fields"].values()))


def _item_line(item, show_author):
    from .reporting import _plain
    markers = []
    if item.get("origin") == "manual":
        markers.append("人工补充，无自动消息依据")
    elif item.get("manual_text"):
        markers.append("人工改写，引用仅用于回查原话")
    if item.get("original_category", item["category"]) != item["category"]:
        markers.append("人工调整分类")
    author = _plain(item.get("sender_name") or "人工补充") + "：" if show_author else ""
    marker = "【" + "；".join(markers) + "】" if markers else ""
    refs = " ".join(f"[{ref}]" for ref in item.get("refs", []))
    return "- " + author + marker + _plain(item["text"]) + (" " + refs if refs else "")


def _pristine(records, layout):
    baseline = layout.get("source_item_baseline", {})
    if baseline and len(baseline) != len(records):
        return False
    for item in records:
        if (not item.get("included", True) or item.get("origin", "message") != "message" or item.get("manual_text")
                or item["text"] != item.get("original_text", item["text"])
                or item["category"] != item.get("original_category", item["category"])):
            return False
        saved = baseline.get(item["item_id"])
        if saved and (saved["text"], saved["category"]) != (item["text"], item["category"]):
            return False
    return True


def render_composed_report(report, items=None, reviewed=False):
    from .reporting import _plain
    snapshot = deepcopy(report.get("composition"))
    if not is_snapshot(snapshot) or any(not isinstance(snapshot["layout"].get(key), str) for key in ("header", "appendix", "timeline", "legacy_body")):
        raise ValueError("此报告没有完整的表达布局快照，不能从当前标题或缓存重造")
    config, layout = snapshot["config"], snapshot["layout"]
    records = deepcopy(report.get("items", []) if items is None else items)
    included = [item for item in records if item.get("included", True)]
    by_id = {item["item_id"]: item for item in records}
    selected = config["evidence_item_ids"]
    if any(identity not in by_id for identity in selected):
        raise ValueError("核对材料含有不属于本报告的条目，不能引用其他报告或他人的材料")
    if any(by_id[identity].get("origin", "message") != "message" for identity in selected):
        raise ValueError("人工新增条目不能作为自动消息依据，请在人工材料中说明")
    header = layout["header"]
    default = _default_layout(snapshot)
    if reviewed:
        label = "个人向上汇报" if report["audience"] == "personal" else "项目进展汇报"
        header = re.sub(r"(?m)^- 模板与方式：.*$", f"- 模板与方式：{label} · 按条人工审阅稿", header)
    elif not default:
        labels = snapshot["template"]["label"] + " · " + snapshot["framework"]["label"]
        header = re.sub(r"(?m)^- 模板与方式：.*$", "- 模板与方式：" + labels + " · 本地规则草稿", header)
        header += "\n- 沟通用途：" + snapshot["purpose"]["label"]
    prefix = "> 按条人工审阅稿：分类和取舍由使用者确认；人工补充或改写需另行核实，引用仅用于回查原话。\n\n" if reviewed else ""
    blocks = []
    fields = config["fields"]
    active_fields = [definition for definition in snapshot["field_definitions"] if fields.get(definition["id"]) or definition["required"]]
    definitions = {definition["id"]: definition for definition in snapshot["field_definitions"]}
    framework_fields = set(snapshot["framework"]["field_ids"])
    context_fields = [definition for definition in active_fields if definition["id"] not in framework_fields]
    if config["framework_id"] != "none":
        blocks.extend(["## " + snapshot["framework"]["label"], "", "> 以下为人工陈述；消息引用用于回查，不自动证明分析或建议。", ""])
        for step in snapshot["framework"]["steps"]:
            if step.get("optional") and not any(fields.get(identity) for identity in step["field_ids"]):
                continue
            blocks.extend(["### " + step["label"], ""])
            if step.get("notice"):
                blocks.extend([step["notice"], ""])
            for identity in step["field_ids"]:
                definition = definitions[identity]
                text = fields.get(identity, "")
                if text or definition["required"]:
                    value = "【人工陈述，需核对】" + _plain(text) if text else "待补充（人工材料；未从消息推断）。"
                    blocks.append("- " + definition["label"] + "：" + value)
            blocks.append("")
    if context_fields:
        blocks.extend(["## 沟通背景与补充材料", "", "> 以下为人工陈述，不自动证明判断、收益、因果或承诺。", ""])
        for definition in context_fields:
            text = fields.get(definition["id"], "")
            blocks.extend(["### " + definition["label"], "", "【人工陈述，需核对】" + _plain(text) if text else "待补充（人工材料；未从消息推断）。", ""])
    invalid_support = False
    if selected:
        baseline = layout.get("source_item_baseline", {})
        supports = []
        for identity in selected:
            item = by_id[identity]
            original = baseline.get(identity, {})
            valid = (item.get("included", True) and not item.get("manual_text")
                     and item["text"] == item.get("original_text", item["text"])
                     and item["category"] == item.get("original_category", item["category"])
                     and (not original or (item["text"], item["category"]) == (original.get("text"), original.get("category"))))
            if valid:
                supports.append(item)
            else:
                invalid_support = True
        blocks.extend(["## 所选核对材料", "", "以下保留所选消息分项的原话与作者，供核对人工陈述；不表示因果或结论已被证实。", ""])
        if invalid_support:
            blocks.extend(["> 所选支撑条目已被排除、改写或调整分类，请重新核对上方人工陈述；此处不重新呈现这些条目的旧原话。", ""])
        blocks.extend(_item_line(item, True) for item in supports)
        if not supports:
            blocks.append("没有仍可直接摘录的原始分项，请重新选择核对材料。")
        blocks.append("")
    if default and not reviewed and layout["legacy_body"] and _pristine(records, layout):
        blocks.append(layout["legacy_body"])
    else:
        sections = {section["id"]: section for section in snapshot["template"]["sections"]}
        show_author = report["audience"] == "project" or len({item.get("sender_id") for item in included if item.get("sender_id")}) > 1
        for identity in config["section_order"]:
            section = sections[identity]
            if identity in config["hidden_sections"] and section.get("hideable") and identity != "risk":
                continue
            blocks.extend(["## " + section["label"], ""])
            matching = [item for item in included if item["category"] in section["categories"]]
            if not matching:
                blocks.append("本期未提及。")
            seen = set()
            for item in matching:
                # Generated composition can share an original message more than once;
                # review versions preserve every individually reviewed entry.
                signature = (item.get("sender_id"), item.get("chat_id"), item["text"], tuple(item.get("refs", [])))
                if not reviewed and signature in seen:
                    continue
                seen.add(signature)
                blocks.append(_item_line(item, show_author))
                if item.get("attribution_warning"):
                    blocks.append("  > 归属待核对：" + _plain(item["attribution_warning"]))
            blocks.append("")
    appendix = layout["appendix"]
    if reviewed:
        additions = sum(item.get("origin") == "manual" for item in included)
        rewritten = sum(item.get("origin", "message") == "message" and item.get("manual_text", False) for item in included)
        summary = (f"\n\n- 审阅取舍：纳入 {len(included)} 条，排除 {len(records) - len(included)} 条；纳入条目中人工新增 {additions} 条、原话改写 {rewritten} 条（按审阅条目计数）。\n"
                   f"- 来源范围：消息计数 {report['message_count']} 条及证据仍为原稿引用范围，包含本版已排除条目的来源；不代表新增或改写内容已获自动证实。")
        # The stored appendix is generated independently, not cut from edited prose.
        appendix = appendix.replace("## 统计与覆盖说明", "## 统计与覆盖说明" + summary, 1)
    timeline = "" if reviewed else layout["timeline"]
    body = "\n".join(blocks)
    return prefix + header + "\n" + body + "\n\n" + timeline + "\n\n" + appendix + "\n"
