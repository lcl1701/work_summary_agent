"use strict";

const $ = (selector) => document.querySelector(selector);
const state = { status: {}, source: "demo", period: "daily", audience: "project", chats: [], selectedChats: new Set(), senders: [], profile: {}, profileLoading: false, sendersLoading: false, sourceLoading: false, savedPreferences: null, preferenceWarnings: [], coverageLoading: false, coverage: null, reports: [], currentReport: null, currentPreview: null, busy: false, loadVersion: 0 };
const periodLabels = { daily: "日报", weekly: "周报", monthly: "月报" };
const sourceLabel = (source) => source === "feishu" ? "飞书群聊" : "演示数据";
const audienceLabel = (audience) => audience === "personal" ? "个人汇报" : "项目汇报";
const localDate = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;

function dateRange() {
  const value = $("#anchor-date").value;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const [year, month, day] = value.split("-").map(Number);
  const start = new Date(year, month - 1, day, 12);
  const end = new Date(start);
  if (state.period === "weekly") {
    start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
    end.setTime(start.getTime()); end.setDate(end.getDate() + 6);
  } else if (state.period === "monthly") {
    start.setDate(1); end.setMonth(end.getMonth() + 1, 0);
  }
  return { start_date: localDate(start), end_date: localDate(end) };
}

function updateRange() {
  const range = dateRange();
  $("#date-range").textContent = range ? `统计范围：${range.start_date} 至 ${range.end_date} · 上海时间` : "请选择有效日期";
  updateControls();
}

function notify(message, kind = "success") {
  const node = $("#notice"); node.textContent = message; node.className = `notice ${kind}`; node.hidden = false;
}

function errorText(error) { return error instanceof Error ? error.message : "操作未完成，请稍后重试。"; }

async function api(path, options = {}) {
  let response;
  try { response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options }); }
  catch { throw new Error("无法连接本地服务。请确认服务已启动，然后重试。"); }
  let data;
  try { data = await response.json(); } catch { throw new Error(`服务返回了无法读取的结果（${response.status}）。`); }
  if (!response.ok) {
    const detail = data.detail || data.error || data.message;
    const message = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((item) => item.msg || "输入格式有误").join("；") : "服务暂时未完成请求，请检查本地配置。";
    const error = new Error(message); error.status = response.status; error.payload = data; throw error;
  }
  if ((options.method && options.method.toUpperCase() !== "GET") || data.job?.status === "succeeded") window.workspaceTools?.operationCompleted(path, data);
  return data;
}

function updateControls() {
  const unavailable = state.source === "feishu" && !state.status.feishu_configured;
  const hasChats = state.selectedChats.size > 0;
  const locked = state.busy || state.sourceLoading || !!window.workspaceFeatures?.jobPending();
  const cannotGenerate = locked || state.coverageLoading || unavailable || !hasChats || !dateRange() || (state.audience === "personal" && (state.sendersLoading || !$("#sender-select").value));
  $("#sync-button").disabled = locked || unavailable || !hasChats || !dateRange();
  $("#generate-button").disabled = cannotGenerate; $("#draft-button").disabled = cannotGenerate;
  $("#seed-button").disabled = locked;
  $("#use-llm").disabled = locked || !state.status.llm_configured;
  $("#save-preferences").disabled = locked || state.sendersLoading;
  document.querySelectorAll("[data-source], [data-period], [data-audience], #anchor-date, #sender-select, .chat-option input").forEach((node) => { node.disabled = locked; });
  $("#sender-select").disabled = locked || state.sendersLoading;
  $("#sender-nickname").disabled = locked || state.profileLoading || state.sendersLoading || !$("#sender-select").value;
  $("#save-profile").disabled = locked || state.profileLoading || state.sendersLoading || !$("#sender-select").value || Array.from($("#sender-nickname").value.trim()).length > 30;
  $("#chat-count").textContent = `${state.selectedChats.size} 个已选`;
  $("#report-form").setAttribute("aria-busy", String(locked));
  renderQuickReport();
  document.querySelectorAll(".report-link, #refresh-history").forEach((node) => { node.disabled = locked; });
  window.workspaceFeatures?.controls();
  window.workspaceTools?.controls();
  window.reportComposition?.controls();
}

async function runAction(button, pendingText, task) {
  if (state.busy) return;
  const label = [...button.childNodes].map((node) => node.cloneNode(true)); state.busy = true; button.textContent = pendingText; updateControls();
  try { await task(); } catch (error) { notify(errorText(error), "error"); }
  finally { state.busy = false; button.replaceChildren(...label); updateControls(); }
}

function sourceQuery(path, source = state.source, chatIds = [...state.selectedChats]) {
  const params = new URLSearchParams({ source });
  for (const id of chatIds) params.append("chat_ids", id);
  return `${path}?${params}`;
}

const selectionKey = () => JSON.stringify([state.source, [...state.selectedChats].sort()]);
const coverageKey = () => JSON.stringify([selectionKey(), state.period, $("#anchor-date").value]);

function currentPreferences() {
  return { source: state.source, chat_ids: [...state.selectedChats], period: state.period, audience: state.audience, sender_id: $("#sender-select").value || "" };
}

function preferencesKey(preferences) {
  return JSON.stringify([[...(preferences.chat_ids || [])].sort(), preferences.period, preferences.audience, preferences.sender_id || ""]);
}

function renderPreferencesStatus() {
  const matching = state.savedPreferences && preferencesKey(currentPreferences()) === preferencesKey(state.savedPreferences);
  const message = state.sourceLoading ? "正在恢复此来源的常用设置…" : state.savedPreferences ? matching ? "常用设置已就绪；下次打开将恢复这些选择。" : "当前选择有修改，保存后下次自动恢复。" : "尚未保存常用设置。日期从今天开始，AI 开关每次进入默认关闭。";
  $("#preferences-status").textContent = [message, ...state.preferenceWarnings].join(" ");
  renderQuickReport();
}

function renderQuickReport() {
  const range = dateRange(); const senderId = $("#sender-select").value;
  const sender = state.senders.find((member) => member.sender_id === senderId);
  const matching = state.savedPreferences && preferencesKey(currentPreferences()) === preferencesKey(state.savedPreferences);
  const chatsAvailable = state.selectedChats.size > 0 && [...state.selectedChats].every((id) => state.chats.some((chat) => chat.chat_id === id));
  const identityAvailable = state.audience !== "personal" || !!sender;
  const sourceAvailable = state.source !== "feishu" || state.status.feishu_configured;
  $("#quick-report").hidden = !matching || !range || !chatsAvailable || !identityAvailable || !sourceAvailable || state.sourceLoading;
  $("#quick-generate-button").disabled = $("#generate-button").disabled;
  if ($("#quick-report").hidden) return;
  $("#quick-report").setAttribute("aria-busy", String(state.busy));
  $("#quick-report-summary").textContent = `${audienceLabel(state.audience)} · ${periodLabels[state.period]} · ${state.selectedChats.size} 个群${state.audience === "personal" ? ` · ${displayName(sender)}` : ""}`;
  const dates = range.start_date === range.end_date ? range.start_date : `${range.start_date} 至 ${range.end_date}`;
  $("#quick-report-range").textContent = `统计范围：${dates} · 上海时间 · ${sourceLabel(state.source)} · ${window.reportComposition?.payload() ? window.reportComposition.label() : $("#use-llm").checked ? "本次 AI 润色" : "免费规则整理"}`;
}

function displayName(member) {
  const id = member.sender_id || "";
  for (const candidate of [member.display_name, member.sender_name]) {
    if (candidate && candidate !== id && !["未命名成员", "未识别成员"].includes(candidate) && !/^(?:ou_|on_|cli_)[A-Za-z0-9]+$/.test(candidate)) return candidate;
  }
  return id && id !== "unknown" ? `未命名成员 · ID 尾号 ${id.slice(-6)}` : "未识别成员";
}

function refreshSelection() {
  Promise.allSettled([loadSenders(), loadMessages(), loadCoverage()]).then((results) => {
    const failed = results.find((result) => result.status === "rejected");
    if (failed) notify(errorText(failed.reason), "error");
  });
  window.workspaceFeatures?.selectionChanged();
}

function renderChats() {
  const target = $("#chat-list"); target.replaceChildren();
  if (!state.chats.length) {
    const empty = document.createElement("p"); empty.className = "empty-small";
    empty.textContent = state.source === "demo" ? "载入演示消息后，在这里选择要汇总的群聊。" : state.status.feishu_configured ? "暂未找到可读取的群聊，请确认应用已发布且机器人已入群。" : "配置完成后，机器人加入的群聊会显示在这里。";
    target.append(empty);
  }
  for (const chat of state.chats) {
    const label = document.createElement("label"); label.className = "chat-option";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.value = chat.chat_id; checkbox.checked = state.selectedChats.has(chat.chat_id);
    checkbox.addEventListener("change", () => { checkbox.checked ? state.selectedChats.add(chat.chat_id) : state.selectedChats.delete(chat.chat_id); updateControls(); renderPreferencesStatus(); refreshSelection(); });
    const icon = document.createElement("span"); icon.className = "chat-icon"; icon.textContent = "#"; icon.setAttribute("aria-hidden", "true");
    const title = document.createElement("span"); title.textContent = chat.name || chat.chat_id; if (chat.description) title.title = chat.description;
    label.append(checkbox, icon, title); target.append(label);
  }
  updateControls();
}

let sendersVersion = 0;
async function loadSenders() {
  const version = ++sendersVersion; const key = selectionKey();
  if (!state.selectedChats.size) {
    state.senders = []; state.sendersLoading = false;
    $("#sender-select").replaceChildren(new Option("请先选择群聊并同步消息", "")); renderProfileFields(); return;
  }
  state.sendersLoading = true; updateControls();
  try {
    const data = await api(sourceQuery("/api/senders"));
    if (version !== sendersVersion || key !== selectionKey()) return;
    const previous = $("#sender-select").value;
    state.senders = (data.senders || []).filter((sender) => sender.sender_id && sender.sender_id !== "unknown");
    const select = $("#sender-select"); select.replaceChildren(new Option(state.senders.length ? "请选择汇报人" : "请先同步所选群聊的消息", ""));
    for (const sender of state.senders) select.add(new Option(`${displayName(sender)} · ${sender.message_count || 0} 条发言`, sender.sender_id));
    if (state.senders.some((sender) => sender.sender_id === previous)) select.value = previous;
    renderProfileFields(); renderPreferencesStatus();
  } catch (error) { if (version === sendersVersion && key === selectionKey()) throw error; }
  finally { if (version === sendersVersion) { state.sendersLoading = false; updateControls(); } }
}

let profileVersion = 0;
async function loadProfile() {
  const version = ++profileVersion; const source = state.source;
  state.profileLoading = true; updateControls();
  try {
    const data = await api(`/api/profile?${new URLSearchParams({ source })}`);
    if (version !== profileVersion || source !== state.source) return;
    state.profile = data.profile || {}; renderProfileFields();
  } catch (error) { if (version === profileVersion && source === state.source) throw error; }
  finally { if (version === profileVersion) { state.profileLoading = false; updateControls(); } }
}

function renderProfileFields() {
  const senderId = $("#sender-select").value;
  $("#sender-identity").hidden = !senderId;
  $("#sender-identity-value").textContent = senderId;
  window.workspaceTools?.identitySelected(senderId);
  updateControls();
}

let messagesVersion = 0;
async function loadMessages() {
  const source = state.source; const version = ++messagesVersion; const key = selectionKey();
  if (!state.selectedChats.size) {
    const empty = document.createElement("div"); empty.className = "empty-table"; empty.textContent = "选择群聊后，在这里查看对应的消息线索。";
    $("#message-list").replaceChildren(empty); $("#messages-count").textContent = "0";
    $("#messages-source").textContent = sourceLabel(source); $("#messages-source").className = `source-badge ${source === "feishu" ? "real" : ""}`; return;
  }
  let data;
  try { data = await api(`${sourceQuery("/api/messages")}&limit=12`); }
  catch (error) { if (version === messagesVersion && key === selectionKey()) throw error; return; }
  if (version !== messagesVersion || key !== selectionKey()) return;
  const messages = data.messages || [];
  const target = $("#message-list"); target.replaceChildren();
  $("#messages-count").textContent = String(messages.length);
  $("#messages-source").textContent = sourceLabel(source);
  $("#messages-source").className = `source-badge ${source === "feishu" ? "real" : ""}`;
  if (!messages.length) { const empty = document.createElement("div"); empty.className = "empty-table"; empty.textContent = "尚无消息，先载入演示数据或同步飞书群聊。"; target.append(empty); }
  for (const message of messages) {
    const row = document.createElement("article"); row.className = "message-item";
    const name = displayName(message);
    const avatar = document.createElement("span"); avatar.className = "avatar"; avatar.setAttribute("aria-hidden", "true"); avatar.textContent = Array.from(name)[0];
    const detail = document.createElement("div"); detail.className = "message-detail";
    const header = document.createElement("div"); header.className = "message-header";
    const sender = document.createElement("strong"); sender.textContent = name; if (message.name_source) sender.title = `姓名来源：${window.workspaceTools?.nameSourceLabel(message.name_source) || "当前姓名设置"}`;
    const chat = document.createElement("span"); chat.textContent = message.chat_name || message.chat_id;
    const time = document.createElement("time"); time.textContent = formatTime(message.created_at); header.append(sender, chat, time);
    const content = document.createElement("p"); content.className = "message-text"; content.textContent = message.text || "（无可汇总的文字内容）";
    detail.append(header, content);
    if (message.sender_id && message.sender_id !== "unknown") {
      const identity = document.createElement("details"); identity.className = "identity-details message-identity";
      const summary = document.createElement("summary"); summary.textContent = "核对成员标识";
      const value = document.createElement("code"); value.textContent = message.sender_id;
      identity.append(summary, value); detail.append(identity);
    }
    row.append(avatar, detail); target.append(row);
  }
}

function coverageStats(coverage) {
  return `${coverage.chat_count || 0} 个群，群内已同步：${coverage.observed_message_count || 0} 条消息 · ${coverage.active_day_count || 0} 个有记录日期`;
}

function coverageSummary(coverage) {
  if (!coverage.complete) return coverage.last_synced_at ? "所选范围仅部分已查询" : "所选范围尚未同步";
  return coverage.checked_through_ts ? `已查询至 ${formatTime(coverage.checked_through_ts)}` : "所选范围已查询";
}

function renderCoverage(coverage, message = "") {
  const panel = $("#coverage-panel"); const stats = $("#coverage-stats");
  panel.classList.toggle("coverage-partial", !!coverage && !coverage.complete);
  panel.setAttribute("aria-busy", String(state.coverageLoading));
  $("#coverage-badge").textContent = state.coverageLoading ? "查询中" : !coverage ? "待查询" : coverage.complete ? "已查询" : "待补充";
  $("#coverage-summary").textContent = message || (coverage ? coverageSummary(coverage) : "选择群聊和汇报日期，查看已同步范围。");
  stats.hidden = !coverage; stats.textContent = coverage ? coverageStats(coverage) : "";
  const details = [];
  if (coverage) {
    if (coverage.period_has_future) details.push("周期尚未结束，后续发言需再次同步。");
    if (!coverage.complete) details.push("点击“同步并生成”查询并整理，也可仅同步消息或先生成草稿。");
    else if (!coverage.observed_message_count) details.push("已查询范围内暂无可汇总消息，可调整日期或群聊后同步。");
    if (coverage.missing_chat_ids?.length) details.push(`待补充查询：${coverage.missing_chat_ids.map((id, index) => state.chats.find((chat) => chat.chat_id === id)?.name || `群 ${index + 1}`).join("、")}。`);
    if (coverage.last_synced_at) details.push(`最近同步：${formatTime(coverage.last_synced_at)}。`);
    if (coverage.observed_first_at && coverage.observed_last_at) details.push(`观察到的消息：${formatTime(coverage.observed_first_at)} 至 ${formatTime(coverage.observed_last_at)}。`);
  }
  $("#coverage-detail").textContent = details.join(" ");
}

let coverageVersion = 0;
async function loadCoverage() {
  const version = ++coverageVersion; const key = coverageKey();
  state.coverage = null;
  if (!state.selectedChats.size || !dateRange()) { state.coverageLoading = false; renderCoverage(null); updateControls(); return; }
  state.coverageLoading = true; renderCoverage(null, "正在核对已同步范围…"); updateControls();
  try {
    const data = await api(`${sourceQuery("/api/coverage")}&${new URLSearchParams({ period: state.period, anchor_date: $("#anchor-date").value })}`);
    if (version !== coverageVersion || key !== coverageKey()) return;
    state.coverage = data.coverage || null;
  } catch (error) {
    if (version !== coverageVersion || key !== coverageKey()) return;
    state.coverageLoading = false; renderCoverage(null, `暂时无法核对范围：${errorText(error)} 你仍可同步或生成草稿。`); return;
  } finally {
    if (version === coverageVersion) { state.coverageLoading = false; updateControls(); }
  }
  renderCoverage(state.coverage);
}

function formatTime(value) {
  if (!value) return "";
  const numeric = Number(value);
  const date = Number.isFinite(numeric) ? new Date(numeric < 100000000000 ? numeric * 1000 : numeric) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

async function loadHistory() {
  const data = await api("/api/reports"); state.reports = data.reports || [];
  $("#history-count").textContent = String(state.reports.length);
  const body = $("#history-body"); body.replaceChildren();
  if (!state.reports.length) { const row = body.insertRow(); const cell = row.insertCell(); cell.colSpan = 5; cell.className = "empty-table"; cell.textContent = "暂无汇报记录，开始整理第一份吧。"; return; }
  for (const report of state.reports) {
    const row = body.insertRow(); const titleCell = row.insertCell();
    const title = document.createElement("button"); title.className = "report-link"; title.type = "button"; title.textContent = report.title || `${periodLabels[report.period] || "工作汇报"} · ${report.start_date}`;
    title.addEventListener("click", () => runAction(title, "正在打开…", async () => { const data = await api(`/api/reports/${encodeURIComponent(report.id)}`); showReport(data.report); $(".preview-card").scrollIntoView({ behavior: "smooth", block: "start" }); }));
    const subtitle = document.createElement("div"); subtitle.className = "report-subtitle"; subtitle.textContent = `${audienceLabel(report.audience)} · ${report.mode === "llm" ? "AI 整理" : "规则整理"} · ${window.workspaceFeatures?.historySubtitle(report) || "v1 · 原稿"}`;
    titleCell.append(title, subtitle);
    row.insertCell().textContent = report.start_date === report.end_date ? report.start_date : `${report.start_date} — ${report.end_date}`;
    const badge = document.createElement("span"); badge.className = `source-badge ${report.source === "feishu" ? "real" : ""}`; badge.textContent = sourceLabel(report.source); row.insertCell().append(badge);
    row.insertCell().textContent = `${report.message_count || 0} 条`;
    const download = document.createElement("a"); download.className = "text-button"; download.textContent = "下载 ↓"; download.href = `/api/reports/${encodeURIComponent(report.id)}/download`; download.download = ""; download.setAttribute("aria-label", `下载${report.title || "汇报"}`); row.insertCell().append(download);
  }
}

// Build report markup with DOM nodes only. Group messages and model output are untrusted text.
function plainText(text) {
  return text.replace(/\\([\\`*_{}\[\]#|])/g, "$1").replace(/&(amp|lt|gt);/g, (_, name) => ({ amp: "&", lt: "<", gt: ">" })[name]);
}

function inlineText(node, text) {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[M\d{3,}\])/g;
  let cursor = 0; let match;
  while ((match = pattern.exec(text)) !== null) {
    node.append(document.createTextNode(plainText(text.slice(cursor, match.index))));
    if (/^\[M\d{3,}\]$/.test(match[0])) {
      const reference = match[0].slice(1, -1); const button = document.createElement("button"); button.type = "button"; button.className = "evidence-reference"; button.textContent = match[0]; button.setAttribute("aria-label", `查看消息依据 ${reference}`); button.addEventListener("click", () => window.workspaceFeatures?.showEvidence(reference)); node.append(button);
    } else {
      const strong = match[0].startsWith("**"); const element = document.createElement(strong ? "strong" : "code");
      if (strong) inlineText(element, match[0].slice(2, -2));
      else element.textContent = plainText(match[0].slice(1, -1));
      node.append(element);
    }
    cursor = pattern.lastIndex;
  }
  node.append(document.createTextNode(plainText(text.slice(cursor))));
}

function renderMarkdown(markdown, target) {
  target.replaceChildren(); let list = null; let code = null; let container = target;
  for (const line of String(markdown || "").replace(/\r\n/g, "\n").split("\n")) {
    if (/^\s*```/.test(line)) { if (code) code = null; else { code = document.createElement("pre"); container.append(code); } list = null; continue; }
    if (code) { code.append(document.createTextNode(`${line}\n`)); continue; }
    if (!line.trim()) { list = null; continue; }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading && heading[1].length <= 2) {
      container = target; list = null;
      if (["补充记录", "统计与覆盖说明", "消息来源", "成员标识核对"].includes(plainText(heading[2]).trim())) {
        const appendix = document.createElement("details"); appendix.className = "identity-details report-identity";
        const summary = document.createElement("summary"); summary.textContent = plainText(heading[2]).trim();
        const content = document.createElement("div"); appendix.append(summary, content); target.append(appendix); container = content; continue;
      }
    }
    const item = /^\s*(?:[-*+] |\d+[.)] )(.+)$/.exec(line);
    const quote = /^\s*>\s?(.*)$/.exec(line);
    if (item) {
      const tag = /^\s*\d/.test(line) ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) { list = document.createElement(tag); container.append(list); }
      const element = document.createElement("li"); inlineText(element, item[1]); list.append(element); continue;
    }
    list = null;
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { container.append(document.createElement("hr")); continue; }
    const element = document.createElement(heading ? `h${Math.min(heading[1].length, 3)}` : quote ? "blockquote" : "p");
    inlineText(element, heading ? heading[2] : quote ? quote[1] : line); container.append(element);
  }
}

function showReport(report, preview = null) {
  state.currentReport = report;
  state.currentPreview = preview;
  $("#preview-empty").hidden = true; $("#report-document").hidden = false;
  const metadata = $("#report-meta"); metadata.replaceChildren();
  const labels = [sourceLabel(report.source), audienceLabel(report.audience), `${report.message_count || 0} 条消息`, report.mode === "llm" ? "AI 整理" : "规则整理"];
  for (const [index, label] of labels.entries()) { const chip = document.createElement("span"); chip.textContent = label; if (index === 0 && report.source !== "feishu") chip.className = "demo-badge"; metadata.append(chip); }
  const coverage = $("#report-coverage"); coverage.replaceChildren();
  const coverageText = document.createElement("p");
  const hasCoverageAppendix = /^##\s+统计与覆盖说明\s*$/m.test(report.markdown || "");
  coverageText.textContent = report.coverage && typeof report.coverage.complete === "boolean" ? hasCoverageAppendix ? `生成时范围：${coverageSummary(report.coverage)}。${report.coverage.period_has_future ? "周期尚未结束。" : ""}详见文末“统计与覆盖说明”。` : `生成时范围：${coverageSummary(report.coverage)}。${coverageStats(report.coverage)}。${report.coverage.period_has_future ? "周期尚未结束。" : ""}已查询不代表工作记录完整。` : "这份历史报告未保存数据覆盖快照；当前左侧筛选不能代表其生成时的范围。";
  coverage.append(coverageText);
  renderMarkdown(report.markdown, $("#report-content"));
  const download = $("#download-button");
  if (!preview) { download.href = `/api/reports/${encodeURIComponent(report.id)}/download`; download.download = ""; download.classList.remove("disabled"); download.removeAttribute("aria-disabled"); download.removeAttribute("tabindex"); $("#copy-button").disabled = false; }
  else { download.removeAttribute("href"); download.classList.add("disabled"); download.setAttribute("aria-disabled", "true"); download.tabIndex = -1; $("#copy-button").disabled = true; }
  $("#preview-footnote").textContent = report.source === "demo" ? "当前为虚构演示消息生成的汇报，仅用于体验流程。" : report.audience === "personal" ? "个人汇报仅依据所选成员的发言，可能遗漏未在群内提及的工作，请核对补充。" : "根据选定群聊中的消息整理，群内陈述需核对后再用于正式汇报。";
  window.workspaceFeatures?.reportShown(report);
  window.workspaceTools?.reportShown(report);
  window.reportComposition?.reportShown(report, preview);
}

async function reloadSource(resetSelection = false) {
  const version = ++state.loadVersion; const source = state.source; const previousSelection = [...state.selectedChats];
  const compositionLoad = window.reportComposition?.loadSource(source);
  state.sourceLoading = true; state.savedPreferences = null; state.preferenceWarnings = [];
  state.chats = []; state.selectedChats = new Set(); renderChats();
  $("#use-llm").checked = false;
  $("#anchor-date").value = state.status.today || localDate(new Date());
  ++sendersVersion; ++messagesVersion; ++profileVersion; ++coverageVersion;
  state.senders = []; state.profile = {}; state.coverage = null;
  state.sendersLoading = false; state.profileLoading = false; state.coverageLoading = false;
  $("#sender-select").replaceChildren(new Option("正在加载成员…", "")); renderProfileFields();
  $("#message-list").replaceChildren(); $("#messages-count").textContent = "0";
  $("#messages-source").textContent = sourceLabel(source);
  renderCoverage(null); renderPreferencesStatus(); window.workspaceFeatures?.selectionChanged();
  $("#seed-button").hidden = source !== "demo";
  $("#feishu-setup-hint").hidden = source !== "feishu" || !!state.status.feishu_configured;
  $("#source-note").textContent = source === "demo" ? "演示消息为虚构内容，可直接体验完整流程。" : "仅读取机器人已加入的群聊；选定范围后手动同步。";
  try {
    const [chatResult, preferenceResult] = await Promise.allSettled([
      source === "feishu" && !state.status.feishu_configured ? Promise.resolve({ chats: [] }) : api(`/api/chats?${new URLSearchParams({ source })}`),
      api(`/api/preferences?${new URLSearchParams({ source })}`)
    ]);
    if (version !== state.loadVersion || source !== state.source) return;
    if (chatResult.status === "rejected") throw chatResult.reason;
    state.chats = chatResult.value.chats || [];
    const preferenceData = preferenceResult.status === "fulfilled" ? preferenceResult.value : {};
    const saved = preferenceData.has_saved ? preferenceData.preferences || {} : null;
    state.savedPreferences = saved;
    if (preferenceResult.status === "rejected") state.preferenceWarnings.push("常用设置暂时无法读取，可重新选择后使用。");
    state.period = saved && periodLabels[saved.period] ? saved.period : "daily";
    state.audience = saved && ["personal", "project"].includes(saved.audience) ? saved.audience : "project";
    if (saved) {
      const savedChats = saved.chat_ids || [];
      state.selectedChats = new Set(savedChats.filter((id) => state.chats.some((chat) => chat.chat_id === id)));
      const inaccessible = savedChats.length - state.selectedChats.size;
      if (inaccessible) state.preferenceWarnings.push(`有 ${inaccessible} 个常用群当前不可访问，请重新选择群聊。`);
    } else if (resetSelection) state.selectedChats = new Set(source === "demo" ? state.chats.map((chat) => chat.chat_id) : []);
    else state.selectedChats = new Set(previousSelection.filter((id) => state.chats.some((chat) => chat.chat_id === id)));
    chooseSegment("period", state.period); chooseSegment("audience", state.audience);
    $("#personal-fields").hidden = state.audience !== "personal"; updateRange(); renderChats();
    const results = await Promise.allSettled([loadProfile(), loadSenders(), loadMessages(), loadCoverage(), compositionLoad]);
    if (version !== state.loadVersion || source !== state.source) return;
    const preferredSender = saved ? saved.sender_id : state.profile.nickname ? state.profile.sender_id : "";
    if (preferredSender) {
      if (state.senders.some((sender) => sender.sender_id === preferredSender)) $("#sender-select").value = preferredSender;
      else state.preferenceWarnings.push("保存的本人身份未出现在当前所选群的消息中，请同步后重新选择本人。");
    }
    renderProfileFields();
    const failed = results.find((result) => result.status === "rejected"); if (failed) throw failed.reason;
  } finally {
    if (version === state.loadVersion) { state.sourceLoading = false; renderPreferencesStatus(); updateControls(); window.workspaceFeatures?.selectionChanged(); }
  }
}

function chooseSegment(attribute, value) {
  document.querySelectorAll(`[data-${attribute}]`).forEach((button) => { const selected = button.dataset[attribute] === value; button.classList.toggle("selected", selected); button.setAttribute("aria-pressed", String(selected)); });
}

function openSetup() { $("#setup-dialog").showModal(); }
$("#open-setup").addEventListener("click", openSetup); $("#inline-setup").addEventListener("click", openSetup);
$("#close-setup").addEventListener("click", () => $("#setup-dialog").close()); $("#setup-done").addEventListener("click", () => $("#setup-dialog").close());
$("#setup-dialog").addEventListener("click", (event) => { if (event.target === event.currentTarget) { const rect = event.currentTarget.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.currentTarget.close(); } });

document.querySelectorAll("[data-source]").forEach((button) => button.addEventListener("click", async () => {
  if (state.busy || state.source === button.dataset.source) return;
  if (window.reportComposition && !(await window.reportComposition.beforeSourceChange(button.dataset.source))) return;
  state.source = button.dataset.source; state.selectedChats = new Set(); $("#sender-select").value = ""; chooseSegment("source", state.source); updateControls();
  runAction(button, "加载中…", () => reloadSource(true));
}));
document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => { state.period = button.dataset.period; chooseSegment("period", state.period); updateRange(); renderPreferencesStatus(); loadCoverage(); window.workspaceFeatures?.selectionChanged(); }));
document.querySelectorAll("[data-audience]").forEach((button) => button.addEventListener("click", () => { state.audience = button.dataset.audience; chooseSegment("audience", state.audience); $("#personal-fields").hidden = state.audience !== "personal"; updateControls(); renderPreferencesStatus(); window.workspaceFeatures?.selectionChanged(); }));
$("#anchor-date").addEventListener("change", () => { updateRange(); loadCoverage(); window.workspaceFeatures?.selectionChanged(); });
$("#sender-select").addEventListener("change", () => { renderProfileFields(); renderPreferencesStatus(); window.workspaceFeatures?.selectionChanged(); });
$("#sender-nickname").addEventListener("input", updateControls);
$("#save-preferences").addEventListener("click", () => {
  if ($("#save-preferences").disabled) return;
  runAction($("#save-preferences"), "正在保存…", async () => {
    const payload = currentPreferences();
    const data = await api("/api/preferences", { method: "POST", body: JSON.stringify(payload) });
    if (payload.source !== state.source) return;
    state.savedPreferences = data.preferences || payload; state.preferenceWarnings = []; renderPreferencesStatus();
    notify("常用设置已保存在本机。下次打开恢复群聊、本人、周期和视角；日期使用当天，AI 开关默认关闭。");
  });
});
$("#save-profile").addEventListener("click", () => {
  if ($("#save-profile").disabled) return;
  window.workspaceTools?.saveIdentity($("#save-profile"));
});

$("#seed-button").addEventListener("click", () => runAction($("#seed-button"), "正在载入…", async () => {
  const data = await api("/api/demo/seed", { method: "POST", body: "{}" });
  await reloadSource(true); notify(`演示数据已就绪，共 ${data.message_count || 0} 条虚构消息。选择周期后即可生成汇报。`);
}));
$("#sync-button").addEventListener("click", () => runAction($("#sync-button"), "正在同步…", async () => {
  try {
    const data = await window.workspaceFeatures.startJob("sync", { ...dateRange(), chat_ids: [...state.selectedChats], source: state.source });
    await Promise.all([loadMessages(), loadSenders(), loadCoverage()]); await window.workspaceFeatures.refresh(); notify(`同步完成，本次处理 ${data.synced_count || 0} 条消息，当前共保存 ${data.total_count || 0} 条。现在可以生成汇报。`);
  } catch (error) { setRunStatus(error.jobPending ? "后台完成状态尚未确认，请点击“继续查看进度”，不要重复同步。" : `本次同步未完成：${errorText(error)}`, "error"); throw error; }
}));

function reportPayload() {
  const composition = window.reportComposition?.payload();
  return { period: state.period, anchor_date: $("#anchor-date").value, source: state.source, chat_ids: [...state.selectedChats], audience: state.audience, sender_ids: state.audience === "personal" ? [$("#sender-select").value] : [], use_llm: !composition && !!state.status.llm_configured && $("#use-llm").checked, ...(composition ? { composition } : {}) };
}

function setRunStatus(message, kind = "info") {
  const node = $("#run-status"); node.hidden = !message; node.textContent = message; node.className = `run-status ${kind}`;
}

async function generateReport(syncFirst) {
  const label = syncFirst ? "同步并生成" : "草稿生成"; const payload = reportPayload();
  setRunStatus(syncFirst ? "正在同步所选范围并整理汇报，请稍候…" : "正在使用本机已存消息整理草稿…");
  let data;
  try {
    data = syncFirst ? await window.workspaceFeatures.startJob("run", { ...payload, ...(payload.composition ? { preview_only: true } : {}) }) : await api(payload.composition ? "/api/composition/preview" : "/api/reports", { method: "POST", body: JSON.stringify(payload) });
    if (data.preview) {
      window.reportComposition.acceptPreview(data.preview); await Promise.allSettled([loadMessages(), loadSenders(), loadCoverage()]); await window.workspaceFeatures.refresh();
      setRunStatus("候选预览已生成，尚未保存报告；请核对材料、来源和结构，再点击“保存此预览”。", "success"); notify("候选预览已准备，尚未进入汇报记录。核对后可明确保存。"); return;
    }
    if (!data.report) throw new Error("服务未返回新报告，请重试。");
  } catch (error) {
    if (error.jobPending) {
      setRunStatus("本次后台完成状态尚未确认。请点击“继续查看进度”，不要重复提交。", "error");
      if (state.currentReport) $("#preview-footnote").textContent = "后台任务状态尚未确认；当前预览仍是之前保存的报告。";
      throw error;
    }
    const previewNote = state.currentReport ? "当前预览仍是之前保存的报告，本次未生成新报告。" : "本次未生成新报告。";
    setRunStatus(`本次${label}未完成。${previewNote}`, "error");
    if (state.currentReport) $("#preview-footnote").textContent = `本次${label}未完成；当前预览为之前保存的报告。`;
    if (syncFirst) await Promise.allSettled([loadMessages(), loadSenders(), loadCoverage()]);
    throw new Error(`${label}未完成：${errorText(error)} ${previewNote}`);
  } finally { $("#use-llm").checked = false; }
  showReport(data.report);
  const results = await Promise.allSettled([loadMessages(), loadSenders(), loadCoverage(), loadHistory()]);
  await window.workspaceFeatures?.refresh();
  const refreshFailed = results.some((result) => result.status === "rejected");
  const message = syncFirst ? `已同步并生成${periodLabels[payload.period]}，本次处理 ${data.synced_count || 0} 条消息，报告引用 ${data.report.message_count || 0} 条。` : `${periodLabels[payload.period]}草稿已生成，引用 ${data.report.message_count || 0} 条本机消息；未重新同步。`;
  setRunStatus(`${message}${refreshFailed ? "部分列表刷新失败，可稍后刷新页面。" : ""}`, "success");
  notify(`${message}请核对后使用。${refreshFailed ? "部分列表暂未刷新。" : ""}`);
  if (window.matchMedia("(max-width: 760px)").matches) $(".preview-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function requestReportGeneration(button, syncFirst) {
  if (button.disabled || state.busy) return;
  if (window.reportComposition && !(await window.reportComposition.beforeGenerate())) return;
  if (state.busy || button.disabled) return;
  runAction(button, syncFirst ? "同步并整理中…" : "正在整理…", () => generateReport(syncFirst));
}
$("#report-form").addEventListener("submit", (event) => {
  event.preventDefault(); requestReportGeneration($("#generate-button"), true);
});
$("#draft-button").addEventListener("click", () => requestReportGeneration($("#draft-button"), false));
$("#quick-generate-button").addEventListener("click", () => {
  if (!$("#quick-report").hidden) requestReportGeneration($("#quick-generate-button"), true);
});
$("#use-llm").addEventListener("change", updateControls);
$("#refresh-history").addEventListener("click", () => runAction($("#refresh-history"), "刷新中…", loadHistory));
$("#copy-button").addEventListener("click", async () => { if (!state.currentReport || state.currentPreview) return; try { await navigator.clipboard.writeText(state.currentReport.markdown); notify("汇报已复制，可粘贴到文档中继续编辑。"); } catch { notify("浏览器未允许复制，请使用下载按钮保存 Markdown 文件。", "info"); } });

async function initialize() {
  const now = new Date(); $("#anchor-date").value = localDate(now);
  $("#today-month").textContent = `${now.getMonth() + 1} 月`; $("#today-day").textContent = String(now.getDate()).padStart(2, "0"); $("#today-weekday").textContent = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"][now.getDay()]; updateRange();
  try {
    state.status = await api("/api/status");
    if (/^\d{4}-\d{2}-\d{2}$/.test(state.status.today || "")) {
      $("#anchor-date").value = state.status.today;
      const [year, month, day] = state.status.today.split("-").map(Number); const today = new Date(year, month - 1, day, 12);
      $("#today-month").textContent = `${month} 月`; $("#today-day").textContent = String(day).padStart(2, "0"); $("#today-weekday").textContent = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"][today.getDay()]; updateRange();
    }
    state.source = ["demo", "feishu"].includes(state.status.preferred_source) ? state.status.preferred_source : state.status.mode === "feishu" ? "feishu" : "demo"; chooseSegment("source", state.source);
    $("#use-llm").checked = false;
    $("#llm-hint").textContent = state.status.llm_configured ? "仅本次勾选才将所选消息发送给已配置的模型服务" : "未配置模型，使用免费规则整理；配置后可按次选择 AI 润色";
    $("#setup-status").replaceChildren();
    for (const label of [`飞书：${state.status.feishu_configured ? "已配置" : "未配置"}`, `AI 模型：${state.status.llm_configured ? "已配置" : "未配置"}`, "时区：Asia/Shanghai"]) { const chip = document.createElement("span"); chip.textContent = label; $("#setup-status").append(chip); }
    const results = await Promise.allSettled([reloadSource(true), loadHistory()]);
    const failed = results.find((result) => result.status === "rejected"); if (failed) throw failed.reason;
  } catch (error) { notify(errorText(error), "error"); } finally { updateControls(); }
}
initialize();
