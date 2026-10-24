"use strict";

// Composition changes presentation, never the identity or provenance of source items.
const compositionState = { catalog: null, catalogPromise: null, source: "", loadVersion: 0, loading: false, config: null, preference: null, draft: null, draftContext: null, draftBaseline: "", preview: null, previousReport: null, prompt: null };
const compositionCopy = (value) => value ? JSON.parse(JSON.stringify(value)) : null;
const compositionLocked = () => state.busy || state.sourceLoading || !!window.workspaceFeatures?.jobPending();
const compositionEqual = (left, right) => JSON.stringify(left) === JSON.stringify(right);
const compositionCatalog = () => compositionState.catalog || { templates: [], frameworks: [], purposes: [], fields: [], combinations: [], common_field_ids: [] };
function compositionDefinition(kind, id) { return (compositionCatalog()[kind] || []).find((item) => item.id === id); }
function compositionCombinations(purpose) { return (compositionCatalog().combinations || []).filter((item) => (item.purposes || []).includes(purpose)); }
function compositionPreference(config) {
  if (!config) return null;
  const { fields, evidence_item_ids, ...preference } = compositionCopy(config); return preference;
}
function compositionDefault(purpose = "update") {
  const preferred = { standard: ["classic", "none"], update: ["ppp", "bluf"], recommendation: ["ppp", "prep"], decision: ["project", "scqa"], escalation: ["project", "sbar"], reflection: ["reflection", "wswnw"] }[purpose] || [];
  const available = compositionCombinations(purpose);
  const pair = available.find((item) => item.template_id === preferred[0] && item.framework_id === preferred[1]) || available[0];
  if (!pair) return null;
  const template = compositionDefinition("templates", pair.template_id);
  return { schema_version: 1, template_id: pair.template_id, framework_id: pair.framework_id, purpose, fields: {}, section_order: (template?.sections || []).map((section) => section.id), hidden_sections: [], evidence_item_ids: [] };
}
function normalizeComposition(raw) {
  if (!raw) return null;
  const config = compositionCopy(raw); const template = compositionDefinition("templates", config.template_id);
  if (!template || !compositionCombinations(config.purpose).some((pair) => pair.template_id === config.template_id && pair.framework_id === config.framework_id)) return null;
  const ids = (template.sections || []).map((section) => section.id);
  config.schema_version = 1; config.fields = config.fields || {}; config.evidence_item_ids = config.evidence_item_ids || [];
  config.section_order = [...new Set([...(config.section_order || []).filter((id) => ids.includes(id)), ...ids])];
  config.hidden_sections = (config.hidden_sections || []).filter((id) => id === "other" && ids.includes(id)); return config;
}
function activeCompositionFieldIds(config) {
  const catalog = compositionCatalog();
  return [...new Set([...(catalog.common_field_ids || []), ...(compositionDefinition("templates", config.template_id)?.field_ids || []), ...(compositionDefinition("frameworks", config.framework_id)?.field_ids || []), ...(compositionDefinition("purposes", config.purpose)?.field_ids || [])])];
}
function compositionMaterials(config) { return !!config && (Object.values(config.fields || {}).some((value) => String(value).trim()) || !!config.evidence_item_ids?.length); }
function compositionDraftDirty() { return !!compositionState.draft && !compositionEqual(compositionState.draft, compositionState.draftBaseline ? JSON.parse(compositionState.draftBaseline) : null); }
function compositionDirtyReasons() {
  const reasons = [];
  if (compositionMaterials(compositionState.config) || compositionDraftDirty() || compositionMaterials(compositionState.draft)) reasons.push("本次汇报用途与沟通材料");
  if (compositionState.preview) reasons.push("尚未保存的汇报候选预览");
  return reasons;
}
function compositionQuestion(message, action = "确认继续") {
  return new Promise((resolve) => {
    let dialog = compositionState.prompt;
    if (!dialog) {
      dialog = document.createElement("dialog"); dialog.setAttribute("aria-labelledby", "composition-confirm-title");
      const header = featureNode("div", "dialog-header"); const title = featureNode("h2", "", "保留本次汇报材料"); title.id = "composition-confirm-title"; header.append(title);
      const body = featureNode("div", "dialog-body"); body.append(featureNode("p")); const footer = featureNode("div", "dialog-footer");
      const cancel = featureNode("button", "button button-secondary", "取消并保留"); cancel.type = "button"; cancel.dataset.compositionCancel = "";
      const accept = featureNode("button", "button button-primary"); accept.type = "button"; accept.dataset.compositionAccept = ""; footer.append(cancel, accept); dialog.append(header, body, footer); document.body.append(dialog); compositionState.prompt = dialog;
    }
    let settled = false; const finish = (answer) => { if (settled) return; settled = true; dialog.close(); resolve(answer); };
    dialog.querySelector(".dialog-body p").textContent = message; dialog.querySelector("[data-composition-accept]").textContent = action;
    dialog.querySelector("[data-composition-cancel]").onclick = () => finish(false); dialog.querySelector("[data-composition-accept]").onclick = () => finish(true);
    dialog.oncancel = (event) => { event.preventDefault(); finish(false); }; dialog.showModal();
  });
}
async function loadCompositionCatalog() {
  if (!compositionState.catalogPromise) compositionState.catalogPromise = api("/api/composition/catalog").then((data) => { compositionState.catalog = data.catalog || data; renderCompositionPurpose(); return compositionState.catalog; }).catch((error) => { compositionState.catalogPromise = null; throw error; });
  return compositionState.catalogPromise;
}
function renderCompositionPurpose() {
  const select = $("#composition-purpose"); select.replaceChildren(new Option("经典结构（保持现有方式）", ""));
  for (const purpose of compositionCatalog().purposes || []) select.add(new Option(purpose.label, purpose.id)); select.value = compositionState.config?.purpose || "";
}
async function loadCompositionSource(source) {
  const version = ++compositionState.loadVersion; compositionState.loading = true; compositionState.source = source;
  compositionState.config = null; compositionState.preference = null; compositionState.draft = null; compositionState.draftContext = null; renderCompositionPurpose(); compositionControls();
  try {
    const [, data] = await Promise.all([loadCompositionCatalog(), api(`/api/composition/preferences?${new URLSearchParams({ source })}`)]);
    if (version !== compositionState.loadVersion || source !== state.source) return;
    compositionState.preference = normalizeComposition(data.composition); compositionState.config = compositionCopy(compositionState.preference);
    if (compositionState.config) { compositionState.config.fields = {}; compositionState.config.evidence_item_ids = []; }
    $("#composition-status").textContent = data.composition && !compositionState.config ? "已保存用途暂不可用，当前使用经典结构；请重新配置。" : compositionState.config ? "已恢复此来源的用途偏好；本次沟通材料需重新填写。" : "未设置用途偏好，保持原有生成方式。";
  } catch (error) {
    if (version === compositionState.loadVersion) $("#composition-status").textContent = `用途配置暂未读取：${errorText(error)} 当前保持经典结构。`;
  } finally { if (version === compositionState.loadVersion) { compositionState.loading = false; renderCompositionPurpose(); updateControls(); } }
}
function compositionLabel(config) {
  if (!config) return "经典结构";
  return `${compositionDefinition("templates", config.template_id)?.label || config.template_id} · ${compositionDefinition("frameworks", config.framework_id)?.label || config.framework_id}`;
}
function compositionControls() {
  const locked = compositionLocked(); const config = compositionState.config;
  $("#composition-purpose").disabled = locked || compositionState.loading;
  $("#configure-composition").disabled = locked || compositionState.loading || !compositionState.catalog;
  $("#composition-summary").textContent = config ? `${compositionLabel(config)}。先生成候选预览，再确认保存。` : "按原有成果、进展、风险和计划整理。";
  if (!state.busy) { $("#generate-button").textContent = config ? "同步并生成候选预览" : "同步并生成 ↗"; $("#draft-button").textContent = config ? "仅预览本机消息" : "仅生成草稿"; $("#quick-generate-button").textContent = config ? "一键生成常用预览 ↗" : "一键生成常用汇报 ↗"; }
  if (compositionState.loading) ["#generate-button", "#draft-button", "#quick-generate-button"].forEach((id) => { $(id).disabled = true; });
  if (config) { $("#use-llm").checked = false; $("#use-llm").disabled = true; $("#llm-hint").textContent = "新用途按本地规则组织；AI 润色仅保留在经典方式中"; }
  else $("#llm-hint").textContent = state.status.llm_configured ? "仅本次勾选才将所选消息发送给已配置的模型服务" : "未配置模型，使用免费规则整理；配置后可按次选择 AI 润色";
  const preview = compositionState.preview; const visibleCandidate = !!state.currentPreview;
  $("#save-composition-preview").disabled = locked || !preview || !visibleCandidate || preview.id !== state.currentPreview.id || Date.now() >= Date.parse(preview.expires_at);
  $("#composition-save-notes").disabled = locked || !visibleCandidate;
  $("#recompose-report").disabled = locked || visibleCandidate || !state.currentReport?.recomposeability?.allowed;
  $("#composition-report-info").querySelectorAll("button").forEach((button) => { button.disabled = locked; });
  document.querySelectorAll("#composition-dialog select, #composition-dialog textarea, #composition-dialog input, #composition-dialog [data-layout-action]").forEach((node) => { node.disabled = locked || node.dataset.layoutDisabled === "true"; });
  $("#save-composition-preferences").disabled = locked || !compositionState.draft; $("#apply-composition").disabled = locked || !compositionState.draft;
  if (visibleCandidate) {
    $("#edit-report").disabled = true; $("#review-report").disabled = true; $("#copy-button").disabled = true;
    $("#download-button").classList.add("disabled"); $("#download-button").removeAttribute("href"); $("#download-button").setAttribute("aria-disabled", "true"); $("#download-button").tabIndex = -1;
  }
}
function currentCompositionPayload() { return compositionCopy(compositionState.config); }
async function beforeCompositionSourceChange(source) {
  if (source === state.source) return true;
  if (compositionDirtyReasons().length && !(await compositionQuestion("切换消息来源会清除本轮用途编辑、沟通材料和未保存候选预览；已保存报告及自由/结构编辑草稿保持保留。", "清除本轮材料并切换来源"))) return false;
  if (compositionState.preview) discardCompositionPreview(); return true;
}
async function beforeCompositionGenerate() {
  if (compositionDraftDirty() && compositionState.draftContext?.mode === "workbench") {
    if (!(await compositionQuestion("配置弹窗里有尚未应用的编辑。本次将使用工作台当前已应用设置，弹窗草稿仍保留。", "使用已应用设置继续"))) { openCompositionDialog(); return false; }
  }
  if (compositionState.preview && !(await compositionQuestion("已有尚未保存的候选预览。继续生成将替换它；如需保留请先保存。", "放弃旧候选并继续生成"))) return false;
  if (compositionState.preview) discardCompositionPreview(); return true;
}
function matchingCompositionReport() {
  const report = state.currentReport; const range = dateRange(); if (!report || !range) return null;
  if (report.source !== state.source || report.audience !== state.audience || report.start_date !== range.start_date || report.end_date !== range.end_date) return null;
  if (!compositionEqual([...(report.chat_ids || [])].sort(), [...state.selectedChats].sort())) return null;
  if (state.audience === "personal" && !report.sender_ids?.includes($("#sender-select").value)) return null; return report;
}
function compositionDraftContextKey(context) { return context.mode === "recompose" ? `report:${context.report.id}` : `source:${context.source}`; }
async function openCompositionDialog(report = null) {
  if (compositionLocked()) return;
  try { await loadCompositionCatalog(); } catch (error) { notify(errorText(error), "error"); return; }
  const context = report ? { mode: "recompose", report, source: report.source } : { mode: "workbench", source: state.source };
  const sameContext = compositionState.draftContext && compositionDraftContextKey(compositionState.draftContext) === compositionDraftContextKey(context);
  if (!sameContext && compositionDraftDirty() && !(await compositionQuestion("另一份用途配置还有未应用编辑，切换将放弃该配置草稿。", "放弃旧配置并打开"))) return;
  if (!sameContext || !compositionState.draft) {
    const saved = report?.composition?.config;
    let sourcePreference = context.source === state.source ? compositionState.config : null;
    if (report && !saved && context.source !== state.source) {
      try { const data = await api(`/api/composition/preferences?${new URLSearchParams({ source: context.source })}`); sourcePreference = normalizeComposition(data.composition); } catch { /* A different source never borrows the current workbench's preferences. */ }
    }
    compositionState.draft = normalizeComposition(saved) || compositionCopy(sourcePreference) || compositionDefault("update") || compositionDefault(compositionCatalog().purposes?.[0]?.id);
    if (report && !saved) { compositionState.draft.fields = {}; compositionState.draft.evidence_item_ids = []; }
    compositionState.draftContext = context; compositionState.draftBaseline = JSON.stringify(compositionState.draft);
  }
  if (!compositionState.draft) { notify("暂未提供可用的用途组合。", "info"); return; }
  $("#composition-heading").textContent = report ? "按用途重组当前报告" : "配置本次汇报用途";
  $("#composition-dialog-scope").textContent = `${sourceLabel(context.source)} · ${report ? `以《${report.title}》的保存快照重组，不读取当前筛选的群消息` : "本次工作台设置，业务材料不会保存为通用偏好"}`;
  $("#apply-composition").textContent = report ? "预览重组结果" : "用于本次汇报";
  renderCompositionDialog(); $("#composition-dialog").showModal();
}
function renderCompositionDialog() {
  const config = compositionState.draft; if (!config) return;
  const purposes = $("#composition-dialog-purpose"); purposes.replaceChildren(); for (const item of compositionCatalog().purposes || []) purposes.add(new Option(item.label, item.id)); purposes.value = config.purpose;
  const available = compositionCombinations(config.purpose); const templateIds = [...new Set(available.map((item) => item.template_id))];
  const templates = $("#composition-template"); templates.replaceChildren(); for (const id of templateIds) templates.add(new Option(compositionDefinition("templates", id)?.label || id, id)); templates.value = config.template_id;
  const frameworks = $("#composition-framework"); frameworks.replaceChildren();
  for (const pair of available.filter((item) => item.template_id === config.template_id)) frameworks.add(new Option(compositionDefinition("frameworks", pair.framework_id)?.label || pair.framework_id, pair.framework_id)); frameworks.value = config.framework_id;
  const framework = compositionDefinition("frameworks", config.framework_id);
  $("#composition-description").textContent = [compositionDefinition("templates", config.template_id)?.description, framework?.description, framework?.steps?.length ? `表达顺序：${framework.steps.map((step) => step.label).join(" → ")}。` : ""].filter(Boolean).join(" ");
  renderCompositionSections(); renderCompositionFields(); renderCompositionEvidence(); updateCompositionMissing(); compositionControls();
}
function renderCompositionSections() {
  const config = compositionState.draft; const target = $("#composition-sections"); target.replaceChildren();
  for (const [index, id] of config.section_order.entries()) {
    const section = compositionDefinition("templates", config.template_id)?.sections.find((item) => item.id === id); const row = featureNode("div", "composition-section-row"); row.append(featureNode("span", "", section?.label || id));
    for (const [offset, label] of [[-1, "上移"], [1, "下移"]]) {
      const button = featureNode("button", "text-button", label); button.type = "button"; button.dataset.layoutAction = ""; button.setAttribute("aria-label", `${label}${section?.label || id}`); button.disabled = index + offset < 0 || index + offset >= config.section_order.length; button.dataset.layoutDisabled = String(button.disabled);
      button.addEventListener("click", () => { const destination = index + offset; if (destination < 0 || destination >= config.section_order.length) return; [config.section_order[index], config.section_order[destination]] = [config.section_order[destination], config.section_order[index]]; renderCompositionSections(); }); row.append(button);
    }
    if (id === "other" && section?.hideable) { const label = featureNode("label"); const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = !config.hidden_sections.includes(id); checkbox.setAttribute("aria-label", "显示补充记录"); checkbox.addEventListener("change", () => { config.hidden_sections = checkbox.checked ? config.hidden_sections.filter((item) => item !== id) : [...new Set([...config.hidden_sections, id])]; }); label.append(checkbox, document.createTextNode("显示")); row.append(label); }
    target.append(row);
  }
}
function renderCompositionFields() {
  const config = compositionState.draft; const target = $("#composition-fields"); target.replaceChildren();
  const required = new Set([...(compositionDefinition("templates", config.template_id)?.required_field_ids || []), ...(compositionDefinition("frameworks", config.framework_id)?.required_field_ids || []), ...(compositionDefinition("purposes", config.purpose)?.required_field_ids || [])]);
  for (const id of activeCompositionFieldIds(config)) {
    const field = compositionDefinition("fields", id); if (!field) continue;
    const block = featureNode("div", "composition-field"); const label = featureNode("label", "field-label", `${field.label}${required.has(id) || field.required ? " · 关键材料" : ""}`); label.htmlFor = `composition-field-${id}`;
    const input = document.createElement("textarea"); input.id = label.htmlFor; input.rows = 2; input.maxLength = field.max_length || 2000; input.placeholder = field.placeholder || "未填时显示待补充"; input.value = config.fields[id] || "";
    input.addEventListener("input", () => { config.fields[id] = input.value; updateCompositionMissing(); }); block.append(label, input, featureNode("p", "field-help", field.description || "由你确认的本次材料，不自动视为消息事实。")); target.append(block);
  }
}
function renderCompositionEvidence() {
  const context = compositionState.draftContext; const report = context?.mode === "recompose" ? context.report : matchingCompositionReport();
  const target = $("#composition-evidence"); target.replaceChildren();
  const items = (report?.items || []).filter((item) => item.item_id && (item.origin || "message") === "message");
  $("#composition-evidence-help").textContent = items.length ? "选择此次表达要重点展开的消息事项；不修改原作者、引用或其他事实归类。" : "当前没有同范围结构化事项；可先生成候选预览，再配置重点事项。";
  const available = new Set(items.map((item) => item.item_id)); compositionState.draft.evidence_item_ids = compositionState.draft.evidence_item_ids.filter((id) => available.has(id));
  for (const item of items) {
    const label = featureNode("label", "composition-evidence-option"); const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = compositionState.draft.evidence_item_ids.includes(item.item_id);
    checkbox.addEventListener("change", () => { if (checkbox.checked && compositionState.draft.evidence_item_ids.length >= 20) { checkbox.checked = false; $("#composition-dialog-status").textContent = "最多选择 20 个重点事项。"; return; } compositionState.draft.evidence_item_ids = checkbox.checked ? [...compositionState.draft.evidence_item_ids, item.item_id] : compositionState.draft.evidence_item_ids.filter((id) => id !== item.item_id); });
    label.append(checkbox, featureNode("span", "", `${item.sender_name || "成员"}：${item.text || ""} ${(item.refs || []).map((reference) => `[${reference}]`).join(" ")}`)); target.append(label);
  }
}
function updateCompositionMissing() {
  const config = compositionState.draft; if (!config) return;
  const required = new Set([...(compositionDefinition("templates", config.template_id)?.required_field_ids || []), ...(compositionDefinition("frameworks", config.framework_id)?.required_field_ids || []), ...(compositionDefinition("purposes", config.purpose)?.required_field_ids || []), ...activeCompositionFieldIds(config).filter((id) => compositionDefinition("fields", id)?.required)]);
  const missing = [...required].filter((id) => !String(config.fields[id] || "").trim());
  $("#composition-dialog-status").textContent = missing.length ? `还有 ${missing.length} 项关键材料待补充，仍可先预览；系统不会编造这些内容。` : "本次材料保留在此页面，保存用途偏好只保存结构与表达选择。";
}
function adjustCompositionChoice(kind) {
  const config = compositionState.draft; if (!config) return;
  if (kind === "purpose") {
    const next = compositionDefault($("#composition-dialog-purpose").value); if (next) compositionState.draft = next;
  } else {
    config.template_id = $("#composition-template").value;
    const available = compositionCombinations(config.purpose).filter((item) => item.template_id === config.template_id);
    config.framework_id = kind === "framework" ? $("#composition-framework").value : available.some((item) => item.framework_id === config.framework_id) ? config.framework_id : available[0]?.framework_id;
    const sectionIds = (compositionDefinition("templates", config.template_id)?.sections || []).map((section) => section.id);
    config.section_order = [...new Set([...config.section_order.filter((id) => sectionIds.includes(id)), ...sectionIds])]; config.hidden_sections = config.hidden_sections.filter((id) => sectionIds.includes(id));
    const fields = activeCompositionFieldIds(config); config.fields = Object.fromEntries(Object.entries(config.fields).filter(([id]) => fields.includes(id)));
  }
  renderCompositionDialog();
}
$("#composition-template").addEventListener("change", () => adjustCompositionChoice("template")); $("#composition-framework").addEventListener("change", () => adjustCompositionChoice("framework"));
$("#composition-dialog-purpose").addEventListener("change", async () => {
  const next = $("#composition-dialog-purpose").value; const previous = compositionState.draft.purpose;
  if (compositionMaterials(compositionState.draft) && !(await compositionQuestion("切换用途会清除这份配置中的本次沟通材料和重点事项；章节与字段将按新用途重新列出。", "清除材料并切换用途"))) { $("#composition-dialog-purpose").value = previous; return; }
  $("#composition-dialog-purpose").value = next; adjustCompositionChoice("purpose");
});
$("#composition-purpose").addEventListener("change", async () => {
  const purpose = $("#composition-purpose").value; $("#composition-purpose").value = compositionState.config?.purpose || "";
  if ((compositionMaterials(compositionState.config) || compositionDraftDirty()) && !(await compositionQuestion("切换汇报用途会清除当前本次沟通材料和用途配置草稿；已保存报告不会改变。", "清除材料并切换用途"))) return;
  compositionState.config = purpose ? compositionDefault(purpose) : null; compositionState.draft = null; compositionState.draftContext = null;
  renderCompositionPurpose(); $("#composition-status").textContent = purpose ? "已切换本次用途。可打开配置补充材料；用途偏好需另行明确保存。" : "已切回经典结构；AI 开关保持关闭。"; $("#use-llm").checked = false; updateControls();
});
$("#configure-composition").addEventListener("click", () => openCompositionDialog());
document.querySelectorAll("[data-close-composition]").forEach((button) => button.addEventListener("click", () => { if (!state.busy) $("#composition-dialog").close(); }));
$("#composition-dialog").addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
$("#save-composition-preferences").addEventListener("click", () => runAction($("#save-composition-preferences"), "保存偏好中…", async () => {
  const context = compositionState.draftContext; const config = normalizeComposition(compositionState.draft); if (!config || !context) return;
  const data = await featurePost("/api/composition/preferences", { source: context.source, composition: compositionPreference(config) });
  if (context.mode === "workbench" && context.source === state.source) { compositionState.preference = normalizeComposition(data.composition); compositionState.config = compositionCopy(config); compositionState.draftBaseline = JSON.stringify(config); renderCompositionPurpose(); }
  $("#composition-dialog-status").textContent = "用途偏好已保存；本次业务材料与重点事项未写入偏好，仍保留在当前弹窗。"; notify("用途偏好已保存，未自动生成或重组报告。");
}));
$("#composition-form").addEventListener("submit", (event) => {
  event.preventDefault(); const config = normalizeComposition(compositionState.draft); const context = compositionState.draftContext; if (!config || !context || compositionLocked()) return;
  if (Object.values(config.fields).reduce((sum, value) => sum + String(value).length, 0) > (compositionCatalog().limits?.max_fields_total || 16000)) { $("#composition-dialog-status").textContent = "本次沟通材料合计超过 16000 字符，请缩短后应用。"; return; }
  if (context.mode === "workbench") {
    if (context.source !== state.source) { $("#composition-dialog-status").textContent = "来源已变化，请关闭后重新配置。"; return; }
    compositionState.config = config; compositionState.draftBaseline = JSON.stringify(config); renderCompositionPurpose(); $("#composition-dialog").close(); updateControls(); notify("已应用本次用途。生成后先预览，确认无误再保存。"); return;
  }
  runAction($("#apply-composition"), "准备重组预览…", async () => {
    if (compositionState.preview && !(await compositionQuestion("已有尚未保存的候选预览，继续将替换它。", "放弃旧候选并预览重组"))) return;
    const data = await featurePost(`/api/reports/${encodeURIComponent(context.report.id)}/composition/preview`, { composition: config });
    $("#composition-dialog").close(); compositionState.draftBaseline = JSON.stringify(config); acceptCompositionPreview(data.preview); notify("框架重组候选已准备，原报告未改变；请核对并保存此预览。");
  });
});
function acceptCompositionPreview(preview) {
  if (!preview?.id || !preview.report) throw new Error("服务未返回可确认的候选预览，未保存报告。");
  if (!state.currentPreview && state.currentReport) compositionState.previousReport = state.currentReport;
  if (compositionState.preview?.id !== preview.id) $("#composition-save-notes").value = "";
  compositionState.preview = preview; showReport(preview.report, preview);
  clearTimeout(compositionState.expiryTimer); compositionState.expiryTimer = setTimeout(() => { if (compositionState.preview?.id === preview.id) { $("#composition-candidate-status").textContent = "候选预览已过期，请重新生成或重组候选后再确认保存。"; compositionControls(); } }, Math.max(0, Math.min(2147483647, Date.parse(preview.expires_at) - Date.now() + 50)));
}
function discardCompositionPreview() {
  const previous = compositionState.previousReport; compositionState.preview = null; compositionState.previousReport = null; state.currentPreview = null;
  if (previous) showReport(previous); else { state.currentReport = null; $("#report-document").hidden = true; $("#preview-empty").hidden = false; $("#composition-candidate").hidden = true; $("#copy-button").disabled = true; $("#download-button").classList.add("disabled"); $("#composition-report-info").hidden = true; $("#recompose-notice").textContent = ""; }
  compositionControls();
}
function compositionReportShown(report, preview = null) {
  $("#composition-candidate").hidden = !preview;
  if (preview) $("#composition-candidate-status").textContent = `${sourceLabel(report.source)} · 仅为当前快照的候选结果，尚未进入汇报记录。有效至 ${featureTime(preview.expires_at)}；保存后才可编辑、复制和下载。`;
  const info = $("#composition-report-info"); info.replaceChildren(); info.hidden = !report;
  if (!report) return;
  const snapshot = report.composition;
  info.append(featureNode("p", "", snapshot ? `用途：${snapshot.purpose?.label || snapshot.config?.purpose || "已保存用途"} · 内容模板：${snapshot.template?.label || snapshot.config?.template_id || ""}${snapshot.template?.version ? ` v${snapshot.template.version}` : ""} · 表达框架：${snapshot.framework?.label || snapshot.config?.framework_id || ""}${snapshot.framework?.version ? ` v${snapshot.framework.version}` : ""}` : "原有模板：此报告未保存新的用途配置快照；当前工作台配置不会改写它。"));
  if (snapshot) {
    const definitions = snapshot.field_definitions || []; const list = Array.isArray(definitions) ? definitions : Object.values(definitions);
    const missing = list.filter((field) => (field.required || snapshot.framework?.required_field_ids?.includes(field.id)) && !String(snapshot.config?.fields?.[field.id] || "").trim());
    if (missing.length) info.append(featureNode("p", "composition-missing", `材料待补充：${missing.map((field) => field.label || field.id).join("、")}。`));
  }
  if (compositionState.preview && !preview) { const button = featureNode("button", "text-button", "返回尚未保存的候选预览"); button.type = "button"; button.addEventListener("click", () => acceptCompositionPreview(compositionState.preview)); info.append(button); }
  $("#recompose-notice").textContent = preview ? "先保存候选，再进行编辑或重组。" : report.recomposeability?.allowed ? "以此报告快照重组，核对后另存版本。" : report.recomposeability?.reason || "此报告暂不支持按用途重组。";
  if (preview) $("#preview-footnote").textContent = "候选预览尚未保存；消息引用来自本次候选快照，核对后再确认保存。";
  compositionControls();
}
$("#save-composition-preview").addEventListener("click", () => runAction($("#save-composition-preview"), "保存预览中…", async () => {
  const preview = compositionState.preview; if (!preview || state.currentPreview?.id !== preview.id) return;
  try {
    const data = await featurePost("/api/composition/save", { preview_id: preview.id, notes: $("#composition-save-notes").value.trim() }); if (!data.report) throw new Error("服务尚未返回已保存报告，请核对汇报记录。");
    compositionState.preview = null; compositionState.previousReport = null; compositionState.draft = null; compositionState.draftContext = null;
    if (compositionState.config) { compositionState.config.fields = {}; compositionState.config.evidence_item_ids = []; }
    showReport(data.report); await loadHistory(); $("#composition-status").textContent = "本轮候选已保存；下一轮沟通材料已清空，避免复用旧业务内容。";
    notify(data.already_saved ? "此候选已保存，已打开同一份报告；未重复新增。" : "候选已确认保存，现可复制、下载或另存审阅版本。");
  } catch (error) { $("#composition-candidate-status").textContent = `候选尚未确认保存：${errorText(error)} 可继续核对，过期后需重新预览。`; throw error; }
}));
$("#recompose-report").addEventListener("click", () => { if (!$("#recompose-report").disabled) openCompositionDialog(state.currentReport); });
window.reportComposition = { loadSource: loadCompositionSource, controls: compositionControls, payload: currentCompositionPayload, beforeSourceChange: beforeCompositionSourceChange, beforeGenerate: beforeCompositionGenerate, acceptPreview: acceptCompositionPreview, reportShown: compositionReportShown, dirtyReasons: compositionDirtyReasons, label: () => compositionLabel(compositionState.config) };
loadCompositionSource(state.source);
