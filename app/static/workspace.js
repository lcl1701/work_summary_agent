"use strict";

// Identity display, change notices, operation receipts and structured review.
// These tools never refresh the page or replace an editor without a user's action.
const FRONTEND_VERSION = "0.3.0";
const workspaceState = {
  identityKey: "", identityVersion: 0, identityLoading: false, identity: null, identityBaseline: "",
  version: null, versionChecking: false, pageBuild: "", pageInstance: "", lastInstance: "", settingsRevision: null,
  externalSettings: false, ownChangePending: false, localChangeSequence: 0, operationsVersion: 0, operationWarning: "", seenOperations: new Set(),
  channel: null, tabId: `tab-${Date.now()}-${Math.random().toString(36).slice(2)}`,
  reviewReport: null, reviewRows: [], additions: [], reviewBaseline: "", reviewItemBaseline: "", allowUnload: false
};
const reviewCategories = { completed: "本期成果", progress: "工作进展", risk: "风险与待协调", plan: "下一步计划", other: "补充记录" };
const operationActions = {
  save_identity: "保存报告姓名设置", save_profile: "保存本人昵称", save_remark: "保存成员备注", save_preferences: "保存常用设置",
  refresh_names: "刷新飞书姓名", sync_messages: "同步消息", sync_and_generate: "同步并生成汇报", generate_report: "生成汇报草稿",
  revise_report: "另存自由编辑稿", review_report: "另存按条审阅稿", link_task: "关联事项更新", unlink_task: "撤销事项关联",
  create_backup: "创建本机备份", restore_backup: "恢复本机备份", seed_demo: "载入演示消息"
  , save_composition_preferences: "保存汇报用途设置", save_report_preview: "保存汇报预览"
};
const operationStatuses = { running: "进行中", succeeded: "已完成", failed: "未完成", interrupted: "已中断，完成状态未确认" };
const nameSourceLabel = (source) => ({ official: "飞书姓名", nickname: "自定义昵称", alias: "本机备注", original: "原始消息姓名", unnamed: "稳定成员标识" })[source] || "当前显示姓名";
const workspaceLocked = () => state.busy || state.sourceLoading || !!window.workspaceFeatures?.jobPending();
const identityMode = () => document.querySelector('input[name="identity-mode"]:checked')?.value || "official";
const identityEditorKey = () => JSON.stringify([identityMode(), $("#sender-nickname").value]);
function realMeta(name) {
  const value = document.querySelector(`meta[name="${name}"]`)?.content || "";
  return value && !/^__.*__$/.test(value) ? value : "";
}
function workspaceControls() {
  const locked = workspaceLocked(); const senderId = $("#sender-select").value;
  const identityLocked = locked || workspaceState.identityLoading || state.profileLoading || state.sendersLoading || !senderId;
  document.querySelectorAll('input[name="identity-mode"]').forEach((node) => { node.disabled = identityLocked; });
  $("#sender-nickname").disabled = identityLocked || identityMode() !== "nickname";
  const nicknameLength = Array.from($("#sender-nickname").value.trim()).length;
  $("#save-profile").disabled = identityLocked || (identityMode() === "nickname" && (nicknameLength < 1 || nicknameLength > 30));
  $("#review-report").disabled = locked || !!state.currentPreview || !state.currentReport?.reviewability?.allowed;
  $("#refresh-operations").disabled = locked;
  const reviewLocked = locked || !workspaceState.reviewReport;
  document.querySelectorAll(".review-row input, .review-row select, .review-row textarea, #review-notes, #add-review-item, .remove-review-addition").forEach((node) => { node.disabled = reviewLocked; });
  const included = workspaceState.reviewRows.filter((row) => row.included.checked).length + workspaceState.additions.length;
  $("#save-review").disabled = reviewLocked || included < 1 || !reviewHasItemChanges();
  $("#review-selection-count").textContent = workspaceState.reviewReport ? `纳入 ${included} 项 · 原事项 ${workspaceState.reviewRows.length} 项` : "";
  $("#reload-workspace").disabled = locked || (!!workspaceState.version && workspaceState.version.backend_build !== workspaceState.version.running_build);
}

function paintIdentity(identity) {
  workspaceState.identity = identity || null;
  const mode = identity?.mode === "nickname" ? "nickname" : "official";
  document.querySelectorAll('input[name="identity-mode"]').forEach((radio) => { radio.checked = radio.value === mode; });
  $("#sender-nickname").value = identity?.nickname || "";
  $("#identity-official").textContent = `飞书姓名：${identity?.official_name || "尚未读取，可在“成员与事项”尝试刷新姓名"}`;
  $("#identity-source").textContent = identity ? `当前显示：${identity.display_name || "未命名成员"} · 来源：${nameSourceLabel(identity.name_source)}` : "选择汇报人后查看姓名来源。";
  $("#nickname-status").textContent = identity ? identity.notice || "选择显示方式并保存后，仅新报告使用此设置。" : "姓名设置不会验证身份，请先核对本人标识。";
  workspaceState.identityBaseline = identityEditorKey(); workspaceControls();
}
async function identitySelected(senderId, force = false) {
  const key = JSON.stringify([state.source, senderId || ""]);
  if (!force && key === workspaceState.identityKey) { workspaceControls(); return; }
  workspaceState.identityKey = key; const version = ++workspaceState.identityVersion;
  if (!senderId) { workspaceState.identityLoading = false; paintIdentity(null); return; }
  workspaceState.identityLoading = true; paintIdentity(null); $("#nickname-status").textContent = "正在读取该成员的姓名显示设置…"; workspaceControls();
  try {
    const data = await api(`/api/identity?${new URLSearchParams({ source: state.source, sender_id: senderId })}`);
    if (version !== workspaceState.identityVersion || key !== JSON.stringify([state.source, $("#sender-select").value])) return;
    paintIdentity(data.identity || null);
  } catch (error) {
    if (version !== workspaceState.identityVersion || key !== workspaceState.identityKey) return;
    $("#nickname-status").textContent = `姓名设置暂未读取：${errorText(error)}`;
  } finally { if (version === workspaceState.identityVersion) { workspaceState.identityLoading = false; workspaceControls(); } }
}
async function saveIdentity(button) {
  if (button.disabled) return;
  return runAction(button, "保存中…", async () => {
    const source = state.source; const senderId = $("#sender-select").value;
    const data = await featurePost("/api/identity", { source, sender_id: senderId, mode: identityMode(), nickname: $("#sender-nickname").value.trim() });
    if (source !== state.source || senderId !== $("#sender-select").value) return;
    paintIdentity(data.identity); await Promise.all([loadProfile(), loadSenders(), loadMessages()]);
    if (state.senders.some((sender) => sender.sender_id === senderId)) $("#sender-select").value = senderId;
    renderProfileFields(); renderPreferencesStatus(); await window.workspaceFeatures?.refresh();
    notify("报告姓名设置已保存；本人标识、常用身份与历史报告保持原样，新报告使用已选显示方式。");
  });
}
async function refreshIdentity() {
  if (workspaceState.identityBaseline && workspaceState.identityBaseline !== identityEditorKey()) {
    $("#nickname-status").textContent = "姓名来源可能已有更新，当前未保存的姓名编辑已保留；保存后再核对显示效果。"; return;
  }
  return identitySelected($("#sender-select").value, true);
}
document.querySelectorAll('input[name="identity-mode"]').forEach((radio) => radio.addEventListener("change", () => {
  $("#nickname-status").textContent = "姓名显示设置尚未保存；切回飞书姓名会保留原昵称供以后使用。"; workspaceControls();
}));
$("#sender-nickname").addEventListener("input", () => { $("#nickname-status").textContent = "昵称输入尚未保存，仅在选择“自定义昵称”并保存后启用。"; workspaceControls(); });

function dirtyReasons() {
  const reasons = [];
  if (workspaceState.identityBaseline && workspaceState.identityBaseline !== identityEditorKey()) reasons.push("姓名或昵称设置");
  if ([...document.querySelectorAll(".member-alias-input")].some((node) => node.value !== (node.dataset.initialValue || ""))) reasons.push("成员备注");
  if (featureState.revisionReport && ($("#revision-markdown").value !== (featureState.revisionReport.markdown || "") || $("#revision-notes").value.trim())) reasons.push("自由 Markdown 编辑稿");
  if (workspaceState.reviewReport && reviewHasChanges()) reasons.push("按条审阅稿");
  if ($("#task-dialog").open && ($("#task-title").value.trim() || featureState.taskSelection.size)) reasons.push("事项关联选择");
  reasons.push(...(window.reportComposition?.dirtyReasons() || []));
  return reasons;
}
function requestPageUpdate() {
  if (workspaceLocked()) { notify("当前操作尚未结束，请先查看任务进度。", "info"); return; }
  const dirty = dirtyReasons();
  if (dirty.length) { $("#update-dirty-description").textContent = `本页有未保存内容：${dirty.join("、")}。`; $("#update-dialog").showModal(); return; }
  workspaceState.allowUnload = true; window.location.reload();
}
$("#reload-workspace").addEventListener("click", requestPageUpdate);
$("#confirm-update").addEventListener("click", () => { workspaceState.allowUnload = true; window.location.reload(); });
window.addEventListener("beforeunload", (event) => { if (!workspaceState.allowUnload && dirtyReasons().length) { event.preventDefault(); event.returnValue = ""; } });

function resolveDraftConflict({ kind, previous, target, resume, discard }) {
  let dialog = workspaceState.draftConflictDialog;
  if (!dialog) {
    dialog = document.createElement("dialog"); dialog.id = "draft-conflict-dialog"; dialog.setAttribute("aria-labelledby", "draft-conflict-heading");
    const header = featureNode("div", "dialog-header"); const title = featureNode("h2", "", "已有未保存草稿"); title.id = "draft-conflict-heading";
    const close = featureNode("button", "icon-button", "×"); close.type = "button"; close.setAttribute("aria-label", "保留草稿并取消切换"); close.addEventListener("click", () => dialog.close()); header.append(title, close);
    const body = featureNode("div", "dialog-body"); const message = featureNode("p"); message.dataset.draftConflictMessage = "";
    body.append(message, featureNode("p", "feature-warning", "当前草稿不会被自动覆盖。请返回旧草稿保存，或明确丢弃旧草稿后再编辑另一报告。"));
    const footer = featureNode("div", "dialog-footer"); const back = featureNode("button", "button button-secondary", "返回旧草稿"); back.type = "button"; back.dataset.draftConflictResume = "";
    const replace = featureNode("button", "button button-primary", "丢弃旧草稿并编辑当前报告"); replace.type = "button"; replace.dataset.draftConflictDiscard = "";
    footer.append(back, replace); dialog.append(header, body, footer); document.body.append(dialog); workspaceState.draftConflictDialog = dialog;
  }
  dialog.querySelector("[data-draft-conflict-message]").textContent = `《${previous.title || "之前的报告"}》还有未保存的${kind}。当前准备编辑《${target.title || "另一份报告"}》。`;
  dialog.querySelector("[data-draft-conflict-resume]").onclick = () => { if (!workspaceLocked()) { dialog.close(); resume(); } };
  dialog.querySelector("[data-draft-conflict-discard]").onclick = () => { if (!workspaceLocked()) { dialog.close(); discard(); } };
  dialog.showModal();
}

function paintVersion() {
  const version = workspaceState.version;
  $("#page-version").textContent = `页面 v${FRONTEND_VERSION}${workspaceState.pageBuild ? ` · ${workspaceState.pageBuild.slice(0, 8)}` : ""}`;
  if (!version) { $("#service-version").textContent = "服务版本暂未核对"; return; }
  $("#service-version").textContent = `服务 v${version.version || "未知"} · ${version.environment === "feishu" ? "飞书环境" : "演示环境"}`;
  document.querySelectorAll(".sidebar-bottom .version").forEach((node) => { node.textContent = `v${FRONTEND_VERSION}`; });
  const restartNeeded = version.backend_build && version.running_build && version.backend_build !== version.running_build;
  const pageChanged = workspaceState.pageBuild && version.build !== workspaceState.pageBuild;
  const instanceChanged = workspaceState.pageInstance && version.instance_id !== workspaceState.pageInstance;
  const messages = [];
  if (restartNeeded) messages.push("工程已更新，当前服务需要重启；重启后再主动更新此页面。");
  else if (pageChanged) messages.push("当前页面使用旧版文件，请主动更新此页面后使用新功能。");
  else if (instanceChanged) messages.push("本地服务已重新启动，建议主动更新页面以核对当前状态。");
  if (workspaceState.externalSettings) messages.push("其他页面的工作台数据已更新；当前编辑会保留，可保存后主动更新。");
  $("#version-banner").hidden = !messages.length; $("#version-banner-message").textContent = messages.join(" ");
  $("#version-details").textContent = `当前页面 v${FRONTEND_VERSION}${workspaceState.pageBuild ? `（${workspaceState.pageBuild.slice(0, 12)}）` : ""}；服务 v${version.version || "未知"}，${version.environment === "feishu" ? "真实飞书" : "演示"}环境。${restartNeeded ? "服务需要重启。" : ""}`;
  workspaceControls();
}
async function checkVersion() {
  if (workspaceState.versionChecking || document.hidden) return;
  workspaceState.versionChecking = true; const requestChangeSequence = workspaceState.localChangeSequence;
  try {
    const version = await api("/api/version"); workspaceState.version = version;
    if (workspaceState.settingsRevision === null || workspaceState.ownChangePending) {
      workspaceState.settingsRevision = String(version.settings_revision ?? "");
      if (requestChangeSequence === workspaceState.localChangeSequence) workspaceState.ownChangePending = false;
    } else if (String(version.settings_revision ?? "") !== workspaceState.settingsRevision) workspaceState.externalSettings = true;
    workspaceState.lastInstance = version.instance_id || workspaceState.lastInstance; paintVersion();
  } catch { if (!workspaceState.version) paintVersion(); }
  finally {
    workspaceState.versionChecking = false;
    if (workspaceState.ownChangePending && requestChangeSequence !== workspaceState.localChangeSequence) setTimeout(checkVersion, 0);
  }
}
function broadcastWorkspaceChange() {
  const payload = { type: "workspace-updated", from: workspaceState.tabId, at: Date.now() };
  try { workspaceState.channel?.postMessage(payload); localStorage.setItem("work-summary-workspace-event", JSON.stringify(payload)); } catch { /* Version polling remains the fallback. */ }
}
function acceptExternalChange(payload) {
  if (!payload || payload.type !== "workspace-updated" || payload.from === workspaceState.tabId) return;
  workspaceState.externalSettings = true; paintVersion(); checkVersion();
}
function operationCompleted(path, data) {
  const result = data?.job?.status === "succeeded" ? data.job.result : data;
  if (result?.operation_warning) {
    workspaceState.operationWarning = typeof result.operation_warning === "string" ? result.operation_warning : "操作已完成，但回执暂未完整保存；请勿因此重复执行。";
    $("#operations-status").textContent = workspaceState.operationWarning;
  }
  const operationId = result?.operation_id || (data?.job?.status === "succeeded" && data.job.id ? `job:${data.job.id}` : "");
  const explicitMutation = data?.job?.status === "succeeded" || /^\/api\/(identity|profile|preferences|composition\/(preferences|save)|members\/(alias|refresh)|tasks(?:\/[^/]+\/deactivate)?|backups(?:\/restore)?|reports(?:\/[^/]+\/(revisions|review))?|demo\/seed|run|sync)$/.test(path);
  if ((!operationId && !explicitMutation) || (operationId && workspaceState.seenOperations.has(operationId))) return;
  if (operationId) workspaceState.seenOperations.add(operationId);
  workspaceState.localChangeSequence++; workspaceState.ownChangePending = true; broadcastWorkspaceChange(); checkVersion();
  if (window.location.hash === "#operations") loadOperations().catch((error) => { $("#operations-status").textContent = errorText(error); });
}

async function showChangelog() {
  await checkVersion(); const target = $("#changelog-list"); target.replaceChildren();
  const changes = workspaceState.version?.changes || [];
  if (!changes.length) target.append(featureNode("p", "empty-small", "服务暂未提供变更说明。"));
  for (const change of changes) {
    const article = featureNode("article", "changelog-entry"); article.append(featureNode("h3", "", `v${change.version || ""} · ${change.date || ""}`));
    article.append(featureNode("p", "", Array.isArray(change.summary) ? change.summary.join("；") : change.summary || ""));
    if (change.dependencies) article.append(featureNode("p", "field-help", `工程依赖：${Array.isArray(change.dependencies) ? change.dependencies.join("、") : typeof change.dependencies === "object" ? Object.entries(change.dependencies).map(([key, value]) => `${key} ${value}`).join("、") : change.dependencies}`));
    target.append(article);
  }
  $("#changelog-dialog").showModal();
}
$("#open-changelog").addEventListener("click", showChangelog); $("#show-changelog").addEventListener("click", showChangelog);

function operationCountText(counts) {
  const labels = { synced_count: "本次同步", total_count: "本机累计消息", message_count: "引用消息", members_refreshed: "读取姓名", review_item_count: "纳入事项", manual_item_count: "人工新增或改写", messages: "消息", reports: "报告" };
  return Object.entries(labels).filter(([key]) => typeof counts?.[key] === "number").map(([key, label]) => `${label} ${counts[key]}`).join(" · ");
}
async function loadOperations() {
  const version = ++workspaceState.operationsVersion; const data = await api("/api/operations?limit=50"); if (version !== workspaceState.operationsVersion) return;
  const target = $("#operations-list"); target.replaceChildren();
  const operations = data.operations || [];
  $("#operations-status").textContent = [workspaceState.operationWarning, `显示最近 ${operations.length} 条操作回执${typeof data.total === "number" ? `，共 ${data.total} 条` : ""}。`].filter(Boolean).join(" ");
  if (!operations.length) { target.append(featureNode("p", "empty-small", "尚无已记录操作。后续同步、生成、姓名设置与数据维护会留下回执。")); return; }
  for (const operation of operations) {
    const row = featureNode("article", `operation-row operation-${["running", "succeeded", "failed", "interrupted"].includes(operation.status) ? operation.status : "unknown"}`);
    const heading = featureNode("div", "operation-heading"); heading.append(featureNode("strong", "", operationActions[operation.action] || "工作空间操作"), featureNode("span", "operation-status", operationStatuses[operation.status] || "状态待核对"));
    const scope = [featureTime(operation.time), operation.source ? sourceLabel(operation.source) : "本机数据", operation.chat_ids?.length ? `${operation.chat_ids.length} 个群` : ""];
    if (operation.display_mode === "official" || operation.display_mode === "nickname") scope.push(operation.display_mode === "official" ? "飞书姓名" : "自定义昵称");
    if (operation.audience === "personal" || operation.audience === "project") scope.push(operation.audience === "personal" ? "个人汇报" : "项目汇报");
    if (operation.start_date) scope.push(operation.start_date === operation.end_date ? operation.start_date : `${operation.start_date} 至 ${operation.end_date || operation.start_date}`);
    row.append(heading, featureNode("p", "field-help", scope.filter(Boolean).join(" · ")));
    const counts = operationCountText(operation.counts); if (counts) row.append(featureNode("p", "operation-counts", counts));
    if (operation.status === "failed") row.append(featureNode("p", "field-help", "本次操作未完成，请先核对当前数据和任务进度，再决定是否重试。"));
    if (operation.status === "interrupted") row.append(featureNode("p", "field-help", "服务曾重启，此操作未确认完成，不代表成功；请核对报告记录或备份结果。"));
    if (operation.report_id) {
      const button = featureButton("打开本次报告", () => runAction(button, "打开中…", async () => {
        const response = await api(`/api/reports/${encodeURIComponent(operation.report_id)}`); showReport(response.report); $(".preview-card").scrollIntoView({ behavior: "smooth", block: "start" });
      })); row.append(button);
    }
    target.append(row);
  }
  window.workspaceFeatures?.controls(); workspaceControls();
}
$("#refresh-operations").addEventListener("click", () => runAction($("#refresh-operations"), "刷新中…", async () => { try { await loadOperations(); } catch (error) { $("#operations-status").textContent = errorText(error); throw error; } }));
document.querySelectorAll('a[href="#operations"]').forEach((link) => link.addEventListener("click", () => loadOperations().catch((error) => { $("#operations-status").textContent = errorText(error); })));

function reviewCategory(item) {
  if (Object.hasOwn(reviewCategories, item.review_category)) return item.review_category;
  if (Object.hasOwn(reviewCategories, item.category)) return item.category;
  return ({ "本期成果": "completed", "工作进展": "progress", "风险与待协调": "risk", "下一步计划": "plan", "补充记录": "other" })[item.category] || "other";
}
function categorySelect(value, label) {
  const select = document.createElement("select"); select.setAttribute("aria-label", label);
  for (const [key, title] of Object.entries(reviewCategories)) select.add(new Option(title, key)); select.value = value; return select;
}
function reviewSnapshot(includeNotes = true) {
  const snapshot = {
    changes: workspaceState.reviewRows.map((row) => ({ item_id: row.item.item_id, included: row.included.checked, category: row.category.value, text: row.text.value })),
    additions: workspaceState.additions.map((row) => ({ text: row.text.value, category: row.category.value }))
  };
  if (includeNotes) snapshot.notes = $("#review-notes").value;
  return JSON.stringify(snapshot);
}
function reviewHasChanges() { return !!workspaceState.reviewReport && workspaceState.reviewBaseline !== reviewSnapshot(); }
function reviewHasItemChanges() { return !!workspaceState.reviewReport && workspaceState.reviewItemBaseline !== reviewSnapshot(false); }
function updateReviewRow(row) {
  row.node.classList.toggle("review-excluded", !row.included.checked);
  row.manual.textContent = row.item.origin === "manual" ? "人工补充条目，无新增消息引用" : row.item.manual_text || row.text.value !== row.initialText ? "人工改写，原引用不会自动证明改写内容" : "来自消息快照";
  workspaceControls();
}
function reopenStructuredDraft() {
  $("#review-status").textContent = `已恢复《${workspaceState.reviewReport.title || "工作汇报"}》未保存的纳入选择、分类、改写、人工补充与审阅说明。`;
  $("#review-dialog").showModal(); workspaceControls();
}
function openStructuredReview(report = state.currentReport, discardExisting = false) {
  if (workspaceLocked() || state.currentPreview || !report?.reviewability?.allowed) return;
  const previous = workspaceState.reviewReport;
  if (!discardExisting && previous && reviewHasChanges()) {
    if (previous.id === report.id) { reopenStructuredDraft(); return; }
    resolveDraftConflict({
      kind: "按条审阅稿", previous, target: report,
      resume: () => { showReport(previous); reopenStructuredDraft(); },
      discard: () => { showReport(report); openStructuredReview(report, true); }
    });
    return;
  }
  workspaceState.reviewReport = report; workspaceState.reviewRows = []; workspaceState.additions = []; workspaceState.reviewBaseline = "";
  $("#review-item-list").replaceChildren(); $("#review-additions").replaceChildren(); $("#review-notes").value = "";
  for (const [index, item] of (report.items || []).entries()) {
    const row = { item, node: featureNode("article", "review-row"), initialText: String(item.text || ""), initialIncluded: item.included !== false, initialCategory: reviewCategory(item) };
    const heading = featureNode("div", "review-item-heading"); const includedLabel = featureNode("label", "review-include-label");
    row.included = document.createElement("input"); row.included.type = "checkbox"; row.included.checked = item.included !== false; row.included.setAttribute("aria-label", `纳入第 ${index + 1} 项`); includedLabel.append(row.included, document.createTextNode(`纳入第 ${index + 1} 项`));
    row.category = categorySelect(reviewCategory(item), `第 ${index + 1} 项分类`); heading.append(includedLabel, row.category);
    const source = featureNode("div", "review-item-source", `作者：${item.sender_name || (item.origin === "manual" ? "人工补充" : "未记录")}`);
    for (const reference of item.refs || (item.ref ? [item.ref] : [])) {
      if (!/^M\d{3,}$/.test(reference)) continue;
      const button = featureNode("button", "evidence-reference", `[${reference}]`); button.type = "button"; button.setAttribute("aria-label", `查看第 ${index + 1} 项消息依据 ${reference}`); button.addEventListener("click", () => window.workspaceFeatures.showEvidence(reference, report)); source.append(button);
    }
    row.text = document.createElement("textarea"); row.text.rows = 3; row.text.maxLength = 10000; row.text.value = row.initialText; row.text.setAttribute("aria-label", `第 ${index + 1} 项内容`);
    row.manual = featureNode("p", "field-help"); row.node.append(heading, source, row.text, row.manual);
    if (item.original_text && item.original_text !== row.initialText) { const original = featureNode("details", "identity-details"); original.append(featureNode("summary", "", "查看生成时原条目"), featureNode("p", "review-original", item.original_text)); row.node.append(original); }
    for (const node of [row.included, row.category, row.text]) node.addEventListener(node === row.text ? "input" : "change", () => updateReviewRow(row));
    workspaceState.reviewRows.push(row); $("#review-item-list").append(row.node); updateReviewRow(row);
  }
  workspaceState.reviewBaseline = reviewSnapshot(); workspaceState.reviewItemBaseline = reviewSnapshot(false); $("#review-status").textContent = `保留 ${report.items?.length || 0} 项可审阅事项，包括先前排除的条目；原作者与来源编号不可更改。`;
  $("#review-dialog").showModal(); workspaceControls();
}
function addManualReviewItem() {
  if (workspaceLocked() || !workspaceState.reviewReport || workspaceState.additions.length >= 100) return;
  const row = { node: featureNode("article", "review-row review-manual-addition") }; const heading = featureNode("div", "review-item-heading");
  row.category = categorySelect("other", "人工补充条目分类");
  const remove = featureNode("button", "text-button remove-review-addition", "移除此补充"); remove.type = "button"; remove.addEventListener("click", () => { workspaceState.additions = workspaceState.additions.filter((item) => item !== row); row.node.remove(); workspaceControls(); });
  heading.append(featureNode("strong", "", "人工补充"), row.category, remove);
  row.text = document.createElement("textarea"); row.text.rows = 3; row.text.maxLength = 10000; row.text.placeholder = "填写你确认要补充的工作内容"; row.text.setAttribute("aria-label", "人工补充条目内容");
  row.node.append(heading, row.text, featureNode("p", "field-help", "本条无自动消息引用，保存后标记为人工补充。"));
  row.text.addEventListener("input", workspaceControls); row.category.addEventListener("change", workspaceControls);
  workspaceState.additions.push(row); $("#review-additions").append(row.node); workspaceControls(); row.text.focus();
}
$("#review-report").addEventListener("click", () => openStructuredReview()); $("#add-review-item").addEventListener("click", addManualReviewItem); $("#review-notes").addEventListener("input", workspaceControls);
$("#review-form").addEventListener("submit", (event) => {
  event.preventDefault(); if ($("#save-review").disabled || !workspaceState.reviewReport) return;
  runAction($("#save-review"), "保存审阅中…", async () => {
    const changes = workspaceState.reviewRows.filter((row) => row.included.checked !== row.initialIncluded || row.category.value !== row.initialCategory || row.text.value !== row.initialText).map((row) => {
      const change = { item_id: row.item.item_id, included: row.included.checked, category: row.category.value };
      if (row.text.value !== row.initialText) change.text = row.text.value.trim(); return change;
    });
    if (changes.length > 500) { $("#review-status").textContent = "每次最多调整 500 条原事项，请分批审阅并另存。"; return; }
    const additions = workspaceState.additions.map((row) => ({ text: row.text.value.trim(), category: row.category.value }));
    if (changes.some((item) => Object.hasOwn(item, "text") && !item.text) || additions.some((item) => !item.text)) { $("#review-status").textContent = "事项内容不能为空；不需要的原条目请取消纳入，空人工补充请移除。"; return; }
    const total = workspaceState.reviewRows.reduce((sum, row) => sum + row.text.value.length, 0) + additions.reduce((sum, item) => sum + item.text.length, 0);
    if (total > 200000) { $("#review-status").textContent = "事项文字总量超过 200000 字符，请减少内容后保存。"; return; }
    try {
      const data = await featurePost(`/api/reports/${encodeURIComponent(workspaceState.reviewReport.id)}/review`, { changes, additions, notes: $("#review-notes").value.trim() });
      if (!data.report) throw new Error("服务尚未返回审阅稿，请核对报告记录。");
      workspaceState.reviewReport = null; workspaceState.reviewBaseline = ""; $("#review-dialog").close(); showReport(data.report); await loadHistory(); notify("按条审阅稿已另存，原稿保留；人工改写与补充已单独标记。");
    } catch (error) { $("#review-status").textContent = errorText(error); throw error; }
  });
});
function reportShownTools(report) {
  $("#review-notice").hidden = !report;
  $("#review-notice").textContent = report?.reviewability?.allowed ? "可按条核对纳入范围与分类，或使用自由编辑另存修订稿。" : report?.reviewability?.reason || "此报告不支持按条审阅，可使用自由编辑并另存修订稿。";
  if (report?.review_summary) {
    const summary = report.review_summary;
    $("#review-notice").textContent = `本版纳入 ${summary.included_count || 0} 项、排除 ${summary.excluded_count || 0} 项；人工补充 ${summary.manual_count || 0} 项、改写 ${summary.rewritten_count || 0} 项。引用消息数保留原始来源范围，不等于纳入事项数。`;
  }
  workspaceControls();
}

document.querySelectorAll("[data-close-workspace]").forEach((button) => button.addEventListener("click", () => { if (!state.busy) document.getElementById(button.dataset.closeWorkspace).close(); }));
$("#review-dialog").addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
workspaceState.pageBuild = realMeta("work-summary-build"); workspaceState.pageInstance = realMeta("work-summary-instance");
try { workspaceState.channel = new BroadcastChannel("work-summary-workspace"); workspaceState.channel.onmessage = (event) => acceptExternalChange(event.data); } catch { /* Storage events and version polling provide a fallback. */ }
window.addEventListener("storage", (event) => { if (event.key === "work-summary-workspace-event" && event.newValue) { try { acceptExternalChange(JSON.parse(event.newValue)); } catch { /* Ignore malformed unrelated storage. */ } } });
window.addEventListener("focus", checkVersion); document.addEventListener("visibilitychange", () => { if (!document.hidden) checkVersion(); });
setInterval(checkVersion, 30000);
window.workspaceTools = { identitySelected, refreshIdentity, saveIdentity, controls: workspaceControls, nameSourceLabel, operationCompleted, reportShown: reportShownTools, dirtyReasons, resolveDraftConflict };
identitySelected($("#sender-select").value); paintVersion(); checkVersion();
if (state.currentReport) reportShownTools(state.currentReport);
