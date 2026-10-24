"use strict";

// Additional workspace tools stay outside the short report form. All remote text is textContent.
const featureState = {
  selection: "", auditSelection: "", memberVersion: 0, taskVersion: 0, auditVersion: 0, backupVersion: 0,
  members: [], taskMessages: [], taskSelection: new Set(), taskContext: null,
  revisionReport: null, restoreBackup: null, activeJob: null, jobUnknown: false, jobPaused: false, polling: false
};
const featureNode = (tag, className = "", text = "") => {
  const node = document.createElement(tag); if (className) node.className = className; node.textContent = text; return node;
};
const featurePost = (path, body = {}) => api(path, { method: "POST", body: JSON.stringify(body) });
const featureKey = () => selectionKey();
const featureAuditKey = () => JSON.stringify([coverageKey(), state.audience, $("#sender-select").value]);
const featureEmpty = (target, text) => $(target).replaceChildren(featureNode("p", "empty-small", text));
const featureTime = (value) => {
  if (value === null || value === undefined || value === "") return "时间未记录";
  const numeric = Number(value); const date = Number.isFinite(numeric) ? new Date(numeric < 100000000000 ? numeric * 1000 : numeric) : new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未记录" : new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
};
const featureBusy = () => state.busy || state.sourceLoading || jobPending();
const taskStatusLabel = (status) => ({ planned: "计划", in_progress: "进行中", blocked: "待协调", completed: "已完成", resolved: "已解除", unspecified: "待核对", risk: "待协调", progress: "进行中", other: "待核对" })[status] || "待核对";
function jobPending() { return featureState.jobUnknown || featureState.activeJob?.status === "running"; }
function featureError(target, error) { $(target).textContent = errorText(error); }
function featureButton(label, action, className = "text-button") {
  const button = featureNode("button", className, label); button.type = "button"; button.dataset.featureAction = "";
  button.addEventListener("click", action); return button;
}
function featureControls() {
  const busy = featureBusy();
  document.querySelectorAll("[data-feature-action]").forEach((node) => { node.disabled = busy; });
  $("#refresh-members").disabled = busy || !state.selectedChats.size || state.source !== "feishu";
  $("#open-task-dialog").disabled = busy || !state.selectedChats.size;
  $("#refresh-audit").disabled = busy || !state.selectedChats.size || !dateRange();
  $("#edit-report").disabled = busy || !state.currentReport || !!state.currentPreview;
  $("#resume-job").disabled = state.busy || featureState.polling;
  $("#save-task").disabled = busy || featureState.taskSelection.size < 2 || !$("#task-title").value.trim() || !$("#task-sender").value;
  document.querySelectorAll(".member-alias-input, #task-sender, #task-title, #task-search, .task-message-option input, #revision-markdown, #revision-notes").forEach((node) => { node.disabled = busy; });
}

function selectionChanged() {
  const key = featureKey();
  const names = [...state.selectedChats].map((id) => state.chats.find((chat) => chat.chat_id === id)?.name || "所选群");
  document.querySelectorAll("[data-feature-scope]").forEach((node) => { node.textContent = `${sourceLabel(state.source)} · ${names.length} 个群`; });
  if (key !== featureState.selection) {
    featureState.selection = key; ++featureState.memberVersion; ++featureState.taskVersion; ++featureState.auditVersion;
    featureState.members = []; featureState.taskMessages = []; featureState.taskSelection.clear();
    featureEmpty("#member-list", "选择群聊后可查看成员，并为已同步成员设置本机备注。");
    featureEmpty("#task-list", "选择群聊后查看已人工关联的事项。");
    $("#member-status").textContent = ""; $("#audit-summary").textContent = "选择群聊和周期后，点击“刷新对账”。"; $("#audit-body").replaceChildren();
    if (state.selectedChats.size && !state.sourceLoading) Promise.allSettled([loadMembers(), loadTasks()]).then((results) => {
      const failed = results.find((result) => result.status === "rejected"); if (failed && key === featureKey()) featureError("#member-status", failed.reason);
    });
  }
  const auditKey = featureAuditKey();
  if (auditKey !== featureState.auditSelection) {
    featureState.auditSelection = auditKey; ++featureState.auditVersion; $("#audit-body").replaceChildren();
    $("#audit-summary").textContent = "当前范围尚未核对，点击“刷新对账”查看本机记录。";
  }
  const range = dateRange();
  const audienceScope = state.audience === "personal" ? $("#sender-select").value ? "按当前所选本人核对" : "尚未选择本人，当前仅核对群记录" : "项目视角，未限定本人";
  $("#audit-scope").textContent = range ? `${sourceLabel(state.source)} · ${range.start_date} 至 ${range.end_date} · ${audienceScope}。数据来自本机，切换范围后请重新刷新对账。` : "请先选择有效汇报日期。";
  featureControls();
}

async function loadMembers() {
  const version = ++featureState.memberVersion; const key = featureKey();
  if (!state.selectedChats.size) { featureEmpty("#member-list", "请先选择群聊。"); return; }
  const data = await api(sourceQuery("/api/members"));
  if (version !== featureState.memberVersion || key !== featureKey()) return;
  featureState.members = (data.members || []).filter((member) => member.sender_id && member.sender_id !== "unknown");
  if ([...document.querySelectorAll(".member-alias-input")].some((input) => input.value !== (input.dataset.initialValue || ""))) {
    $("#member-status").textContent = "成员数据已更新，当前未保存的备注编辑已保留；保存备注后再核对名称。"; return;
  }
  const target = $("#member-list"); target.replaceChildren();
  if (!featureState.members.length) { featureEmpty("#member-list", "尚无已同步成员，请先同步群消息。"); return; }
  for (const [index, member] of featureState.members.entries()) {
    const row = featureNode("article", "member-row");
    const heading = featureNode("div", "member-row-heading"); heading.append(featureNode("strong", "", displayName(member)), featureNode("span", "muted", `${member.message_count || 0} 条消息`));
    const official = featureNode("p", "field-help", `飞书名称：${member.official_name || "尚未读取"} · 本机备注：${member.alias || "未设置"}${member.name_source ? ` · 当前来源：${window.workspaceTools?.nameSourceLabel(member.name_source) || "姓名设置"}` : ""}`);
    const identity = featureNode("details", "identity-details"); identity.append(featureNode("summary", "", "核对成员标识"), featureNode("code", "", member.sender_id));
    const label = featureNode("label", "field-label", "本机备注"); label.htmlFor = `member-alias-${index}`;
    const input = document.createElement("input"); input.id = label.htmlFor; input.type = "text"; input.maxLength = 30; input.value = member.alias || ""; input.dataset.initialValue = input.value; input.className = "member-alias-input"; input.autocomplete = "off"; input.placeholder = "留空保存可清除";
    const save = featureButton("保存备注", () => runAction(save, "保存中…", async () => {
      const source = state.source; const selection = featureKey(); const sender = $("#sender-select").value;
      await featurePost("/api/members/alias", { source, sender_id: member.sender_id, alias: input.value.trim() });
      input.dataset.initialValue = input.value;
      if (source !== state.source || selection !== featureKey()) return;
      await Promise.all([loadMembers(), loadSenders(), loadMessages()]);
      if (state.senders.some((item) => item.sender_id === sender)) $("#sender-select").value = sender;
      renderProfileFields(); renderPreferencesStatus();
      await window.workspaceTools?.refreshIdentity();
      notify("本机备注已保存；本人身份和历史报告保持原样，新报告使用当前名称。");
    }));
    const editor = featureNode("div", "member-alias-row"); editor.append(input, save);
    row.append(heading, official, identity, label, editor);
    if (member.mode === "nickname") row.append(featureNode("p", "field-help", "当前明确启用自定义昵称；若希望使用飞书姓名，可在个人汇报的姓名显示设置中切换。"));
    target.append(row);
  }
  featureControls();
}

$("#refresh-members").addEventListener("click", () => runAction($("#refresh-members"), "刷新中…", async () => {
  const key = featureKey(); const data = await featurePost("/api/members/refresh", { source: state.source, chat_ids: [...state.selectedChats] });
  if (key !== featureKey()) return;
  const warnings = (data.warnings || []).map((warning) => {
    const chat = state.chats.find((item) => item.chat_id === warning.chat_id)?.name || "所选群";
    return `${chat}：${warning.message || "成员姓名暂未刷新，可继续使用本机备注。"}${warning.code ? `（${warning.code}）` : ""}`;
  });
  $("#member-status").textContent = `本次读取 ${data.members_refreshed || 0} 位成员名称。${warnings.length ? warnings.join(" ") : ""} 姓名读取结果不影响消息同步。`;
  await Promise.all([loadMembers(), loadSenders(), loadMessages()]);
  await window.workspaceTools?.refreshIdentity();
}));

async function loadTasks() {
  const version = ++featureState.taskVersion; const key = featureKey();
  if (!state.selectedChats.size) { featureEmpty("#task-list", "请先选择群聊。"); return; }
  const data = await api(sourceQuery("/api/tasks"));
  if (version !== featureState.taskVersion || key !== featureKey()) return;
  const target = $("#task-list"); target.replaceChildren();
  if (!(data.groups || []).length) { featureEmpty("#task-list", "尚无人工关联事项；可选择同一发言人的多条更新建立时间线。"); return; }
  for (const group of data.groups) {
    const details = featureNode("details", `task-group${group.active === false ? " task-inactive" : ""}`);
    details.append(featureNode("summary", "", `${group.title} · ${group.sender_name || "未命名成员"} · ${(group.events || []).length} 条更新${group.active === false ? " · 已撤销关联" : ""}`));
    const timeline = featureNode("ol", "task-timeline");
    for (const event of group.events || []) {
      const item = featureNode("li"); item.append(featureNode("div", "timeline-meta", `${featureTime(event.created_at)} · ${event.chat_name || "群聊"} · ${taskStatusLabel(event.status || event.category)}`), featureNode("p", "timeline-text", event.text || "")); timeline.append(item);
    }
    details.append(timeline);
    if (group.active !== false) {
      const deactivate = featureButton("撤销此事项关联", () => runAction(deactivate, "处理中…", async () => {
        await featurePost(`/api/tasks/${encodeURIComponent(group.id)}/deactivate`); await loadTasks(); notify("事项关联已撤销，原消息和历史报告保留。");
      })); details.append(deactivate);
    }
    target.append(details);
  }
  featureControls();
}

$("#open-task-dialog").addEventListener("click", () => runAction($("#open-task-dialog"), "加载中…", async () => {
  const key = featureKey(); const data = await api(`${sourceQuery("/api/messages")}&limit=500`);
  if (key !== featureKey()) return;
  featureState.taskContext = { source: state.source, chat_ids: [...state.selectedChats], key };
  featureState.taskMessages = (data.messages || []).filter((message) => message.sender_id && message.sender_id !== "unknown"); featureState.taskSelection.clear();
  const senders = new Map(); for (const message of featureState.taskMessages) if (!senders.has(message.sender_id)) senders.set(message.sender_id, message);
  $("#task-sender").replaceChildren(new Option("请选择同一位发言人", ""));
  for (const [id, member] of senders) $("#task-sender").add(new Option(displayName(member), id));
  $("#task-title").value = ""; $("#task-title").maxLength = 100; $("#task-search").value = "";
  $("#task-dialog-status").textContent = `载入当前所选群最近 ${featureState.taskMessages.length} 条可关联消息（最多 500 条）；不会自动选择或创建关联。`;
  renderTaskMessages(); $("#task-dialog").showModal();
}));

function renderTaskMessages() {
  const target = $("#task-message-list"); target.replaceChildren(); const senderId = $("#task-sender").value;
  const search = $("#task-search").value.trim().toLocaleLowerCase();
  const messages = featureState.taskMessages.filter((message) => message.sender_id === senderId && (!search || (message.text || "").toLocaleLowerCase().includes(search)));
  if (!senderId) target.append(featureNode("p", "empty-small", "先选择发言人，再勾选至少 2 条同一事项的相关更新。"));
  else if (!messages.length) target.append(featureNode("p", "empty-small", "没有匹配消息，可调整搜索内容。"));
  for (const message of messages) {
    const label = featureNode("label", "task-message-option"); const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.value = message.message_id; checkbox.checked = featureState.taskSelection.has(message.message_id);
    checkbox.addEventListener("change", () => { checkbox.checked ? featureState.taskSelection.add(message.message_id) : featureState.taskSelection.delete(message.message_id); $("#task-selected-count").textContent = `${featureState.taskSelection.size} 条已选`; featureControls(); });
    const content = featureNode("span"); content.append(featureNode("small", "", `${featureTime(message.created_at)} · ${message.chat_name || "群聊"}`), featureNode("span", "task-message-text", message.text || "")); label.append(checkbox, content); target.append(label);
  }
  $("#task-selected-count").textContent = `${featureState.taskSelection.size} 条已选`; featureControls();
}
$("#task-sender").addEventListener("change", () => { featureState.taskSelection.clear(); renderTaskMessages(); });
$("#task-search").addEventListener("input", renderTaskMessages); $("#task-title").addEventListener("input", featureControls);
$("#task-form").addEventListener("submit", (event) => {
  event.preventDefault(); if ($("#save-task").disabled) return;
  runAction($("#save-task"), "关联中…", async () => {
    if (!featureState.taskContext || featureState.taskContext.key !== featureKey()) throw new Error("所选群聊已变化，请重新打开事项关联。");
    try {
      await featurePost("/api/tasks", { source: featureState.taskContext.source, chat_ids: featureState.taskContext.chat_ids, sender_id: $("#task-sender").value, title: $("#task-title").value.trim(), message_ids: [...featureState.taskSelection] });
      $("#task-dialog").close(); await loadTasks(); notify("事项关联已保存，新生成的周报、月报可引用这份时间线。");
    } catch (error) { featureError("#task-dialog-status", error); throw error; }
  });
});

function auditCoverageText(coverage) {
  if (!coverage || typeof coverage !== "object") return typeof coverage === "string" ? coverage : "范围未记录";
  if (coverage.period_not_started) return "日期尚未到，未查询";
  const text = coverage.complete ? "已查询" : coverage.checked_through_ts ? "部分已查询" : "待同步";
  return `${text}${coverage.period_has_future ? " · 周期未结束" : ""}`;
}
async function loadAudit() {
  const version = ++featureState.auditVersion; const key = featureAuditKey(); const range = dateRange();
  if (!state.selectedChats.size || !range) { $("#audit-summary").textContent = "请先选择群聊和有效日期。"; return; }
  const params = new URLSearchParams({ period: state.period, anchor_date: $("#anchor-date").value });
  const senderId = $("#sender-select").value; const personal = state.audience === "personal" && !!senderId;
  if (personal) params.append("sender_ids", senderId);
  $("#audit-summary").textContent = "正在核对本机消息…";
  const data = await api(`${sourceQuery("/api/audit")}&${params}`);
  if (version !== featureState.auditVersion || key !== featureAuditKey()) return;
  const audit = data.audit || {}; const body = $("#audit-body"); body.replaceChildren();
  for (const day of audit.days || []) {
    const row = body.insertRow(); row.insertCell().textContent = day.date || ""; row.insertCell().textContent = day.chat_name || "所选群";
    row.insertCell().textContent = `${day.message_count || 0} 条`; row.insertCell().textContent = personal ? `${day.selected_sender_count || 0} 条` : "未限定本人"; row.insertCell().textContent = auditCoverageText(day.coverage);
  }
  $("#audit-summary").textContent = `本机共 ${audit.message_count || 0} 条群消息，群内 ${audit.active_days || 0} 个有记录日期${personal ? `；所选本人 ${audit.selected_message_count || 0} 条，${audit.selected_active_days || 0} 个有记录日期` : ""}。${auditCoverageText(audit.coverage)}。${audit.note || "0 条消息不代表没有工作。"}`;
  const senders = featureNode("div", "audit-senders");
  for (const sender of audit.senders || []) senders.append(featureNode("span", "source-badge", `${displayName(sender)} · ${sender.message_count || 0} 条`));
  $("#audit-summary").append(senders);
}
$("#refresh-audit").addEventListener("click", () => runAction($("#refresh-audit"), "核对中…", async () => { try { await loadAudit(); } catch (error) { featureError("#audit-summary", error); throw error; } }));

async function loadBackups() {
  const version = ++featureState.backupVersion; const data = await api("/api/backups"); if (version !== featureState.backupVersion) return;
  const target = $("#backup-list"); target.replaceChildren();
  if (!(data.backups || []).length) { featureEmpty("#backup-list", "尚无本机备份。点击“创建本机备份”保留当前数据。"); return; }
  for (const backup of data.backups) {
    const row = featureNode("article", "backup-row"); const detail = featureNode("div");
    detail.append(featureNode("strong", "", featureTime(backup.created_at)), featureNode("p", "field-help", `${Math.max(1, Math.round((backup.size || 0) / 1024))} KB · ${backup.counts?.messages || 0} 条消息 · ${backup.counts?.reports || 0} 份报告`));
    const restore = featureButton("选择并恢复", () => {
      if (featureBusy()) return; featureState.restoreBackup = backup;
      $("#restore-description").textContent = `将恢复 ${featureTime(backup.created_at)} 创建的备份，含 ${backup.counts?.messages || 0} 条消息、${backup.counts?.reports || 0} 份报告。`;
      $("#restore-status").textContent = "请确认这是你希望恢复的备份；点击下方确认按钮后执行。"; $("#restore-dialog").showModal();
    }); row.append(detail, restore); target.append(row);
  }
  featureControls();
}
$("#refresh-backups").addEventListener("click", () => runAction($("#refresh-backups"), "刷新中…", async () => { try { await loadBackups(); } catch (error) { featureError("#backup-status", error); throw error; } }));
$("#create-backup").addEventListener("click", () => runAction($("#create-backup"), "备份中…", async () => {
  try { const data = await featurePost("/api/backups"); await loadBackups(); $("#backup-status").textContent = `本机备份已创建：${featureTime(data.backup?.created_at)}。`; }
  catch (error) { featureError("#backup-status", error); throw error; }
}));
$("#confirm-restore").addEventListener("click", () => runAction($("#confirm-restore"), "恢复中…", async () => {
  if (!featureState.restoreBackup) return;
  try {
    const data = await featurePost("/api/backups/restore", { backup_id: featureState.restoreBackup.id });
    if (!data.restored) throw new Error("服务尚未确认恢复完成，请核对后重试。");
    $("#restore-status").textContent = "本机数据已恢复，即将重新载入。"; window.location.reload();
  } catch (error) { featureError("#restore-status", error); throw error; }
}));

function showEvidence(reference, snapshotReport = null) {
  const report = snapshotReport || state.currentReport; if (!report) return;
  const target = $("#evidence-content"); target.replaceChildren(); $("#evidence-heading").textContent = `消息依据 [${reference}]`;
  const evidence = Array.isArray(report.evidence) ? report.evidence : [];
  const record = evidence.find((item) => item.ref === reference);
  if (!record || report.evidence_status === "legacy_unavailable") {
    target.append(featureNode("p", "feature-warning", report.evidence_status === "legacy_unavailable" ? "这份历史报告没有结构化来源快照。下方展示报告原有的消息来源附录；不会使用当前群聊缓存重建历史依据。" : "本报告未保存此编号的来源快照。请核对原始附录；人工新增编号不会自动获得依据。"));
    const lines = String(report.markdown || "").split("\n"); const start = lines.findIndex((line) => /^##\s+消息来源\s*$/.test(line));
    let finish = start + 1; while (finish < lines.length && !/^##\s+/.test(lines[finish])) finish++;
    target.append(featureNode("pre", "evidence-original", start >= 0 ? lines.slice(start, finish).join("\n") : "原报告没有消息来源附录，请自行核对原稿。"));
    const sources = [...$("#report-content").querySelectorAll("details")].find((details) => details.querySelector("summary")?.textContent === "消息来源"); if (sources) sources.open = true;
  } else {
    target.append(featureNode("p", "evidence-meta", `${record.sender_name || "未命名成员"} · ${record.chat_name || "群聊"} · ${featureTime(record.created_at)}`));
    target.append(featureNode("p", "field-help", "以下为本报告生成时保存的完整原话，后续同步或改名不会修改此快照。"));
    target.append(featureNode("blockquote", "evidence-quote", record.text || "（无文字内容）"));
    const details = featureNode("details", "identity-details"); details.append(featureNode("summary", "", "核对消息与成员标识"), featureNode("code", "", `消息：${record.message_id || "未记录"}\n成员：${record.sender_id || "未记录"}`)); target.append(details);
    if (report.revision?.review_status === "manual_revision") target.append(featureNode("p", "feature-warning", "当前是人工修订稿：来源快照来自原稿，不能自动证明后来补充或改写的文字。"));
  }
  $("#evidence-dialog").showModal();
}

function freeformDraftDirty() {
  return !!featureState.revisionReport && ($("#revision-markdown").value !== (featureState.revisionReport.markdown || "") || !!$("#revision-notes").value.trim());
}
function reopenRevisionDraft() {
  $("#revision-status").textContent = `已恢复《${featureState.revisionReport.title || "工作汇报"}》未保存的自由编辑内容与修订说明。`;
  $("#revision-dialog").showModal(); featureControls();
}
function openRevision(report = state.currentReport, discardExisting = false) {
  if (!report || featureBusy() || state.currentPreview) return;
  const previous = featureState.revisionReport;
  if (!discardExisting && previous && freeformDraftDirty()) {
    if (previous.id === report.id) { reopenRevisionDraft(); return; }
    if (window.workspaceTools?.resolveDraftConflict) {
      window.workspaceTools.resolveDraftConflict({
        kind: "自由编辑稿", previous, target: report,
        resume: () => { showReport(previous); reopenRevisionDraft(); },
        discard: () => { showReport(report); openRevision(report, true); }
      });
    } else { notify("另一报告有未保存的自由编辑稿，请先保存或明确丢弃。", "info"); }
    return;
  }
  featureState.revisionReport = report;
  $("#revision-markdown").value = report.markdown || ""; $("#revision-notes").value = "";
  $("#revision-status").textContent = `以《${report.title || "工作汇报"}》v${report.revision?.version || 1} 为基础另存，原稿不会被覆盖。`;
  $("#revision-dialog").showModal(); featureControls();
}
$("#edit-report").addEventListener("click", () => openRevision());
$("#revision-form").addEventListener("submit", (event) => {
  event.preventDefault(); if (!featureState.revisionReport || $("#save-revision").disabled) return;
  runAction($("#save-revision"), "保存修订中…", async () => {
    try {
      const data = await featurePost(`/api/reports/${encodeURIComponent(featureState.revisionReport.id)}/revisions`, { markdown: $("#revision-markdown").value, notes: $("#revision-notes").value.trim() });
      if (!data.report) throw new Error("服务未返回修订稿，请检查报告记录。");
      featureState.revisionReport = null; $("#revision-dialog").close(); showReport(data.report); await loadHistory(); notify("修订稿已另存。复制、下载对应当前版本，原稿保留在报告记录中。");
    } catch (error) { featureError("#revision-status", error); throw error; }
  });
});

function reportShown(report) {
  $("#edit-report").disabled = featureBusy() || !report || !!state.currentPreview;
  if (!report) return;
  const revision = report.revision || {}; const metadata = $("#report-meta");
  metadata.append(featureNode("span", revision.review_status === "manual_revision" ? "manual-revision-badge" : "", historySubtitle(report)));
  if (revision.root_report_id && revision.root_report_id !== report.id) {
    const original = featureButton("打开原稿", () => runAction(original, "打开中…", async () => {
      const data = await api(`/api/reports/${encodeURIComponent(revision.root_report_id)}`); showReport(data.report);
    })); metadata.append(original);
  }
  featureControls();
}

function historySubtitle(report) {
  const revision = report.revision || {}; const kind = report.review_kind || revision.review_kind;
  return `v${revision.version || 1}${kind === "composition_revision" ? " · 框架修订" : kind === "structured_review" ? " · 按条审阅" : kind === "freeform" ? " · 自由编辑" : revision.review_status === "manual_revision" ? " · 人工修订" : " · 原稿"}`;
}

function paintJob(job, extra = "") {
  $("#job-panel").hidden = false; $("#job-title").textContent = job.status === "succeeded" ? "后台任务已完成" : job.status === "failed" ? "后台任务未完成" : featureState.jobPaused ? "等待继续查看进度" : "后台处理中";
  const detail = [job.message || "正在准备…"];
  if (job.page) detail.push(`第 ${job.page} 页`); if (job.attempt) detail.push(`第 ${job.attempt} 次重试`); if (extra) detail.push(extra);
  $("#job-message").textContent = detail.join(" · ");
  const completed = Number(job.completed) || 0; const total = Number(job.total) || 0;
  $("#job-count").textContent = total ? `${completed} / ${total}` : "";
  if (total > 0) { $("#job-progress").max = total; $("#job-progress").value = Math.min(completed, total); } else $("#job-progress").removeAttribute("value");
  $("#resume-job").hidden = !featureState.jobPaused; featureControls();
}
function pendingJobError(message) { const error = new Error(message); error.jobPending = true; return error; }
function rememberJob(job) {
  featureState.activeJob = job; featureState.jobUnknown = false;
  try { sessionStorage.setItem("work-summary-active-job", job.id); } catch { /* Progress remains available through the local service. */ }
}
function finishJob(job) {
  featureState.activeJob = job; featureState.jobUnknown = false; featureState.jobPaused = false;
  try { sessionStorage.removeItem("work-summary-active-job"); } catch { /* No persistence is required. */ }
  paintJob(job); updateControls();
}
async function pollJob(initial) {
  let job = initial; let retries = 0; featureState.polling = true; featureState.jobPaused = false;
  try {
    while (true) {
      rememberJob(job); paintJob(job, retries ? `进度连接失败，正在重试 ${retries}/3` : "");
      if (job.status === "succeeded") { finishJob(job); return job.result || {}; }
      if (job.status === "failed") { finishJob(job); throw new Error(job.error || job.message || "后台任务未完成。"); }
      await new Promise((resolve) => setTimeout(resolve, 1000));
      try {
        const data = await api(`/api/jobs/${encodeURIComponent(job.id)}`); if (!data.job) throw new Error("任务进度暂不可用。"); job = data.job; retries = 0;
      } catch (error) {
        if (error.status === 404) { finishJob({ ...job, status: "failed", message: "任务记录已不可用，服务可能已重启。请先核对报告记录，再决定是否重新操作。" }); throw error; }
        retries++;
        if (retries <= 3) { paintJob(job, `进度连接失败，正在重试 ${retries}/3`); continue; }
        featureState.jobPaused = true; paintJob(job, "后台可能仍在运行。点击“继续查看进度”，请勿重复提交。");
        throw pendingJobError("暂时无法取得后台进度；任务可能仍在运行，请继续查看进度，勿重复提交。");
      }
    }
  } finally { featureState.polling = false; featureControls(); }
}

async function startJob(action, payload) {
  if (jobPending()) throw pendingJobError("已有任务尚未确认完成，请继续查看进度。");
  featureState.activeJob = null; featureState.jobUnknown = false; featureState.jobPaused = false; $("#job-panel").hidden = false;
  paintJob({ status: "running", message: "正在提交后台任务…" });
  let data;
  try { data = await featurePost("/api/jobs", { action, ...payload }); }
  catch (error) {
    if (error.status === 409 && error.payload?.job) {
      rememberJob(error.payload.job); featureState.jobPaused = true; paintJob(error.payload.job, "本次未重复提交，请继续查看已有任务。"); updateControls();
      throw pendingJobError("已有后台任务，本次未重复提交。请继续查看进度。");
    }
    if (!error.status || error.status >= 500) {
      let current = null; try { current = await api("/api/jobs/current"); } catch { /* Keep an unconfirmed submission locked until the user checks it. */ }
      if (current?.job) rememberJob(current.job); else featureState.jobUnknown = true;
      featureState.jobPaused = true; paintJob(current?.job || { status: "running", message: "暂时无法确认提交结果。后台可能仍在运行，请继续查看进度并核对报告记录。" }); updateControls();
      throw pendingJobError("提交结果未确认，请继续查看后台进度，勿重复提交。");
    }
    paintJob({ status: "failed", message: errorText(error) }); throw error;
  }
  if (!data.job?.id) {
    featureState.jobUnknown = true; featureState.jobPaused = true; paintJob({ status: "running", message: "服务未返回任务标识，提交结果尚未确认，请继续查看进度。" }); updateControls();
    throw pendingJobError("任务标识未确认，请继续查看进度，勿重复提交。");
  }
  rememberJob(data.job); updateControls(); return pollJob(data.job);
}

async function applyRecoveredJob(job, result) {
  if (result.preview) {
    window.reportComposition.acceptPreview(result.preview); await Promise.allSettled([loadMessages(), loadSenders(), loadCoverage(), loadMembers(), loadTasks()]);
    setRunStatus("后台候选预览已恢复，尚未保存报告；核对后点击“保存此预览”。", "success"); notify("已恢复候选预览，未自动保存报告。"); return;
  }
  if (result.report) showReport(result.report);
  await Promise.allSettled([loadMessages(), loadSenders(), loadCoverage(), loadHistory(), loadMembers(), loadTasks()]);
  const message = result.report ? `后台任务已完成，报告引用 ${result.report.message_count || 0} 条消息。` : `后台同步已完成，本次处理 ${result.synced_count || 0} 条消息。`;
  setRunStatus(message, "success"); notify(message); selectionChanged();
}
$("#resume-job").addEventListener("click", () => runAction($("#resume-job"), "查看中…", async () => {
  let job = featureState.activeJob;
  if (!job?.id) {
    const data = await api("/api/jobs/current");
    if (!data.job) { featureState.jobUnknown = false; featureState.jobPaused = false; featureState.activeJob = null; $("#job-panel").hidden = true; updateControls(); notify("当前没有正在运行的任务。请核对报告记录后再操作。", "info"); return; }
    job = data.job;
  }
  try { const result = await pollJob(job); await applyRecoveredJob(job, result); }
  catch (error) { if (!error.jobPending) setRunStatus(`后台任务未完成：${errorText(error)} 当前预览不代表本次成功。`, "error"); throw error; }
}));

async function discoverJob() {
  try {
    let data = await api("/api/jobs/current");
    if (!data.job) {
      let saved = ""; try { saved = sessionStorage.getItem("work-summary-active-job") || ""; } catch { /* Ignore unavailable storage. */ }
      if (saved) { try { data = await api(`/api/jobs/${encodeURIComponent(saved)}`); } catch { try { sessionStorage.removeItem("work-summary-active-job"); } catch { /* Ignore unavailable storage. */ } } }
    }
    if (data.job) { rememberJob(data.job); featureState.jobPaused = true; paintJob(data.job, "发现此前任务，可继续查看结果；不会自动重新执行。"); updateControls(); }
  } catch { /* An unavailable progress service must not fabricate a completed report. */ }
}

document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => { if (!state.busy) document.getElementById(button.dataset.closeDialog).close(); }));
document.querySelectorAll("#evidence-dialog, #revision-dialog, #task-dialog, #restore-dialog").forEach((dialog) => dialog.addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); }));
document.querySelectorAll('a[href="#members-tasks"]').forEach((link) => link.addEventListener("click", () => Promise.allSettled([loadMembers(), loadTasks()]).then((results) => { const failed = results.find((result) => result.status === "rejected"); if (failed) featureError("#member-status", failed.reason); })));
document.querySelectorAll('a[href="#backups"]').forEach((link) => link.addEventListener("click", () => loadBackups().catch((error) => featureError("#backup-status", error))));

window.workspaceFeatures = { selectionChanged, refresh: () => Promise.allSettled([loadMembers(), loadTasks()]), controls: featureControls, showEvidence, reportShown, historySubtitle, startJob, jobPending };
selectionChanged(); discoverJob();
