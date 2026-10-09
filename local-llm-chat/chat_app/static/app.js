"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const MAX_RENDERED_CHARS = 80000;
const MAX_MARKDOWN_DEPTH = 10;
const STREAM_RENDER_INTERVAL_MS = 100;
const state = { settings: {}, conversations: [], currentId: null, attachments: [], webEnabled: false, busy: false, uploading: 0, abort: null, dragDepth: 0, loadSequence: 0 };
const ui = Object.fromEntries(["history-list", "history-count", "conversation-title", "messages", "welcome", "chat-scroll", "message-input", "composer", "attachments", "send-button", "stop-button", "conversation-status", "settings-dialog", "settings-form", "settings-error", "url-dialog", "url-error"].map(id => [id.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), document.getElementById(id)]));

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function iconButton(name, label, action) {
  const button = element("button", "icon-button");
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.append(icon(name));
  if (action) button.addEventListener("click", action);
  return button;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Local-Chat", "1");
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let response;
  try { response = await fetch(path, { ...options, headers }); }
  catch (error) { if (error.name === "AbortError") throw error; throw new Error("アプリに接続できません。起動しているか確認してください。"); }
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch (_) { /* Use status text for non-JSON errors. */ }
    if (typeof detail !== "string") detail = detail ? JSON.stringify(detail) : `通信に失敗しました（${response.status}）`;
    throw new Error(detail);
  }
  return response;
}

async function getJSON(path, options) { return (await api(path, options)).json(); }
function toast(message, error = false) {
  const node = element("div", `toast${error ? " error" : ""}`, message);
  $("#toasts").append(node);
  setTimeout(() => node.remove(), error ? 8000 : 5000);
}
function displayError(node, message) { node.textContent = message; node.hidden = !message; }
function configured() { return Boolean(state.settings.base_url && state.settings.model); }
function closeSidebar() { $("#sidebar").classList.remove("open"); $("#sidebar-scrim").hidden = true; }
function formatSize(size) { return size >= 1024 * 1024 ? `${(size / (1024 * 1024)).toFixed(1)} MB` : size >= 1024 ? `${Math.round(size / 1024)} KB` : `${size || 0} B`; }
function safeURL(value) { try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : null; } catch (_) { return null; } }
function nearBottom() { return ui.chatScroll.scrollHeight - ui.chatScroll.scrollTop - ui.chatScroll.clientHeight < 140; }
function scrollBottom() { ui.chatScroll.scrollTop = ui.chatScroll.scrollHeight; }
function resizeInput() { ui.messageInput.style.height = "auto"; ui.messageInput.style.height = `${Math.min(Math.max(ui.messageInput.scrollHeight, 39), 200)}px`; }
function updateSend() { ui.sendButton.disabled = state.busy || state.uploading > 0 || (!ui.messageInput.value.trim() && state.attachments.length === 0); }

function updateConnection() {
  $("#model-label").textContent = state.settings.model || "接続を設定";
  $("#sidebar-model").textContent = state.settings.model || "OpenAI互換API";
  $("#model-dot").classList.toggle("unconfigured", !configured());
  $("#setup-card").hidden = configured();
  updateSend();
}

function renderHistory() {
  ui.historyList.replaceChildren();
  ui.historyCount.textContent = state.conversations.length ? String(state.conversations.length) : "";
  if (!state.conversations.length) { ui.historyList.append(element("p", "history-empty", "ここに会話が並びます。\n最初のひとことから、どうぞ。")); return; }
  for (const conversation of state.conversations) {
    const row = element("div", `history-item${conversation.id === state.currentId ? " active" : ""}`);
    const button = element("button", "history-select");
    button.title = conversation.title || "新しいチャット";
    if (conversation.id === state.currentId) button.setAttribute("aria-current", "page");
    button.append(icon("chat"), element("span", "", conversation.title || "新しいチャット"));
    button.addEventListener("click", () => selectConversation(conversation.id));
    const remove = iconButton("trash", `${conversation.title || "チャット"}を削除`, () => deleteConversation(conversation));
    remove.classList.add("history-delete");
    row.append(button, remove);
    ui.historyList.append(row);
  }
}

async function refreshHistory() {
  state.conversations = await getJSON("/api/conversations");
  renderHistory();
  const current = state.conversations.find(conversation => conversation.id === state.currentId);
  if (current) ui.conversationTitle.textContent = current.title || "新しいチャット";
}

function showConversation(conversation) {
  state.currentId = conversation.id;
  ui.conversationTitle.textContent = conversation.title || "新しいチャット";
  ui.messages.replaceChildren();
  for (const message of conversation.messages || []) appendMessage(message);
  ui.welcome.hidden = Boolean(conversation.messages?.length);
  $("#export-chat").hidden = !conversation.messages?.length;
  renderHistory();
  scrollBottom();
}

async function selectConversation(id) {
  if (state.busy) { toast("応答を停止してから、別のチャットを開いてください。"); return; }
  if (state.uploading) { toast("ファイルの読み込みが終わるまでお待ちください。"); return; }
  closeSidebar();
  const sequence = ++state.loadSequence;
  try {
    const conversation = await getJSON(`/api/conversations/${encodeURIComponent(id)}`);
    if (sequence !== state.loadSequence) return;
    state.attachments = [];
    renderAttachments();
    ui.messageInput.value = "";
    resizeInput();
    showConversation(conversation);
    ui.conversationStatus.textContent = "";
  } catch (error) { toast(error.message, true); }
}

function newConversation() {
  if (state.busy) { toast("応答を停止してから、新しいチャットを始めてください。"); return; }
  if (state.uploading) { toast("ファイルの読み込みが終わるまでお待ちください。"); return; }
  state.loadSequence++;
  state.currentId = null;
  state.attachments = [];
  ui.messages.replaceChildren();
  ui.welcome.hidden = false;
  ui.conversationTitle.textContent = "新しいチャット";
  ui.conversationStatus.textContent = "";
  $("#export-chat").hidden = true;
  ui.messageInput.value = "";
  renderAttachments();
  renderHistory();
  resizeInput();
  closeSidebar();
  ui.messageInput.focus();
}

async function deleteConversation(conversation) {
  if (state.busy && conversation.id === state.currentId) { toast("応答を停止してから、このチャットを削除してください。"); return; }
  if (!window.confirm(`「${conversation.title || "新しいチャット"}」を削除しますか？\nこの操作は取り消せません。`)) return;
  try {
    await api(`/api/conversations/${encodeURIComponent(conversation.id)}`, { method: "DELETE" });
    if (state.currentId === conversation.id) newConversation();
    await refreshHistory();
    toast("チャットを削除しました。");
  } catch (error) { toast(error.message, true); }
}

// Build Markdown with DOM nodes. Model output never becomes executable HTML.
function inlineMarkdown(parent, text, depth = 0) {
  if (depth > 5) { parent.append(document.createTextNode(text)); return; }
  const pattern = /(`[^`\n]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*\n]+\*|~~[^~\n]+~~|!?\[[^\]\n]+\]\([^\s)]+\))/g;
  let match, previous = 0;
  while ((match = pattern.exec(text))) {
    parent.append(document.createTextNode(text.slice(previous, match.index)));
    const token = match[0];
    if (token.startsWith("`")) parent.append(element("code", "", token.slice(1, -1)));
    else if (token.startsWith("**") || token.startsWith("__")) { const strong = element("strong"); inlineMarkdown(strong, token.slice(2, -2), depth + 1); parent.append(strong); }
    else if (token.startsWith("~~")) { const del = element("del"); inlineMarkdown(del, token.slice(2, -2), depth + 1); parent.append(del); }
    else if (token.startsWith("*")) { const em = element("em"); inlineMarkdown(em, token.slice(1, -1), depth + 1); parent.append(em); }
    else {
      const linkMatch = token.match(/^!?\[([^\]]+)\]\(([^)]+)\)$/);
      const url = linkMatch && safeURL(linkMatch[2]);
      if (url) { const link = element("a", "", linkMatch[1]); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; parent.append(link); }
      else parent.append(document.createTextNode(token));
    }
    previous = pattern.lastIndex;
  }
  parent.append(document.createTextNode(text.slice(previous)));
}

function renderMarkdown(text, depth = 0) {
  const fragment = document.createDocumentFragment();
  const source = String(text || "");
  const truncated = depth === 0 && source.length > MAX_RENDERED_CHARS;
  const visibleText = truncated ? source.slice(0, MAX_RENDERED_CHARS) : source;
  if (depth >= MAX_MARKDOWN_DEPTH) {
    fragment.append(element("p", "", visibleText));
    return fragment;
  }
  const lines = visibleText.replace(/\r\n?/g, "\n").split("\n");
  const isBoundary = line => /^\s*(```|~~~|#{1,6}\s|>|[-*+]\s|\d+[.)]\s|[-*_]{3,}\s*$)/.test(line);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const fence = line.match(/^\s*(`{3,}|~{3,})(.*)$/);
    if (fence) {
      const codeLines = [];
      const marker = fence[1][0];
      i++;
      while (i < lines.length && !new RegExp(`^\\s*${marker}{${fence[1].length},}\\s*$`).test(lines[i])) codeLines.push(lines[i++]);
      if (i < lines.length) i++;
      const code = codeLines.join("\n");
      const box = element("div", "code-block");
      const heading = element("div", "code-header");
      const copy = element("button", "", "コピー"); copy.type = "button"; copy.addEventListener("click", () => copyText(code, copy));
      heading.append(element("span", "", fence[2].trim() || "code"), copy);
      const pre = element("pre"); pre.append(element("code", "", code));
      box.append(heading, pre); fragment.append(box); continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*$/);
    if (heading) { const node = element(`h${heading[1].length}`); inlineMarkdown(node, heading[2]); fragment.append(node); i++; continue; }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { fragment.append(element("hr")); i++; continue; }
    if (/^\s*>/.test(line)) {
      const quoteLines = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quoteLines.push(lines[i++].replace(/^\s*>\s?/, ""));
      const quote = element("blockquote"); quote.append(renderMarkdown(quoteLines.join("\n"), depth + 1)); fragment.append(quote); continue;
    }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(lines[i + 1])) {
      const cells = row => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(cell => cell.trim());
      const wrap = element("div", "table-wrap"), table = element("table"), head = element("thead"), headRow = element("tr"), body = element("tbody");
      for (const cell of cells(line)) { const th = element("th"); inlineMarkdown(th, cell); headRow.append(th); }
      head.append(headRow); i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        const row = element("tr");
        for (const cell of cells(lines[i++])) { const td = element("td"); inlineMarkdown(td, cell); row.append(td); }
        body.append(row);
      }
      table.append(head, body); wrap.append(table); fragment.append(wrap); continue;
    }
    const listMatch = line.match(/^\s*([-*+]|\d+[.)])\s+(.+)$/);
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1]);
      const list = element(ordered ? "ol" : "ul");
      if (ordered) list.start = parseInt(listMatch[1], 10);
      while (i < lines.length) {
        const item = lines[i].match(/^\s*([-*+]|\d+[.)])\s+(.+)$/);
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        const li = element("li"); inlineMarkdown(li, item[2]); list.append(li); i++;
      }
      fragment.append(list); continue;
    }
    const paragraph = element("p");
    const paragraphLines = [line]; i++;
    while (i < lines.length && lines[i].trim() && !isBoundary(lines[i])) {
      if (lines[i].includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-+:?\s*\|/.test(lines[i + 1])) break;
      paragraphLines.push(lines[i++]);
    }
    paragraphLines.forEach((part, index) => { if (index) paragraph.append(element("br")); inlineMarkdown(paragraph, part); });
    fragment.append(paragraph);
  }
  if (truncated) fragment.append(element("p", "message-status", "画面には先頭80,000文字まで表示しています。全文はメッセージのコピー、または会話のMarkdown保存で確認できます。"));
  return fragment;
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    if (button && !button.querySelector("svg")) { const previous = button.textContent; button.textContent = "コピー済み"; setTimeout(() => { button.textContent = previous; }, 1600); }
    else toast("コピーしました。");
  } catch (_) { toast("コピーできませんでした。テキストを選択してコピーしてください。", true); }
}

function appendMessage(message) {
  const user = message.role === "user";
  const article = element("article", `message ${user ? "user" : "assistant"}`);
  const heading = element("div", "message-heading");
  heading.append(element("span", "message-avatar", user ? "U" : "L"), element("strong", "", user ? "あなた" : "Local Chat"));
  if (message.created_at) {
    const date = new Date(message.created_at);
    if (!isNaN(date.getTime())) heading.append(element("span", "message-time", date.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" })));
  }
  const body = element("div", "message-body");
  if (user) body.textContent = message.content || "添付資料を送信しました。";
  else body.append(renderMarkdown(message.content));
  article.append(heading, body);
  if (message.attachments?.length) {
    const attachments = element("div", "message-attachments");
    for (const attachment of message.attachments) { const chip = element("span", "attachment-mini"); chip.append(icon(attachment.kind === "web" ? "globe" : "file"), element("span", "", attachment.name)); if (attachment.warning) chip.title = attachment.warning; attachments.append(chip); }
    article.append(attachments);
  }
  const sources = element("div", "message-sources");
  article.append(sources);
  const seenSources = new Set();
  function addSource(source) {
    const url = safeURL(source.url);
    if (!url || seenSources.has(url)) return;
    seenSources.add(url);
    const a = element("a"); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.append(icon("globe"), element("span", "", source.title || new URL(url).hostname)); sources.append(a);
  }
  for (const source of message.sources || []) addSource(source);
  const toolbar = element("div", "message-tools");
  const status = element("span", "message-status");
  let currentText = message.content || "";
  let renderedPrefix = currentText.slice(0, MAX_RENDERED_CHARS);
  let renderedTruncated = currentText.length > MAX_RENDERED_CHARS;
  toolbar.append(iconButton("copy", "メッセージをコピー", () => copyText(currentText)), status);
  if (message.status === "stopped" || message.status === "cancelled" || message.status === "interrupted") status.textContent = "応答を停止しました";
  if (message.status === "error") { status.textContent = "応答中にエラーが発生しました"; status.classList.add("error"); }
  article.append(toolbar);
  ui.messages.append(article);
  return {
    body, article, status, addSource,
    update(text) {
      currentText = text;
      const prefix = text.slice(0, MAX_RENDERED_CHARS);
      const truncated = text.length > MAX_RENDERED_CHARS;
      if (prefix === renderedPrefix && truncated === renderedTruncated) return;
      renderedPrefix = prefix;
      renderedTruncated = truncated;
      body.replaceChildren(renderMarkdown(text));
    },
    waiting() { renderedPrefix = null; const dots = element("div", "stream-placeholder"); dots.append(element("span"), element("span"), element("span")); body.replaceChildren(dots); },
  };
}

function renderAttachments() {
  ui.attachments.replaceChildren();
  for (const attachment of state.attachments) {
    const chip = element("div", `attachment-chip${attachment.loading ? " loading" : ""}${attachment.warning ? " warn" : ""}`);
    chip.append(icon(attachment.kind === "web" ? "globe" : "file"));
    const label = element("div"); label.append(element("strong", "", attachment.name), element("small", "", attachment.loading ? "読み込み中…" : `${attachment.kind === "web" ? "Webページ" : formatSize(attachment.size)}${attachment.warning ? " · 注意あり" : ""}`));
    chip.append(label);
    chip.title = attachment.warning || attachment.name;
    if (!attachment.loading) chip.append(iconButton("close", `${attachment.name}の添付を解除`, () => { state.attachments = state.attachments.filter(item => item !== attachment); renderAttachments(); }));
    ui.attachments.append(chip);
  }
  updateSend();
}

async function uploadFiles(files) {
  if (state.busy) { toast("応答が終わってからファイルを添付してください。"); return; }
  const candidates = Array.from(files);
  const available = Math.max(0, 10 - state.attachments.length);
  if (candidates.length > available) toast("添付は1メッセージにつき10件までです。", true);
  const list = candidates.slice(0, available).filter(file => {
    if (file.size > 20 * 1024 * 1024) { toast(`${file.name}: 1ファイルは20 MBまでです。`, true); return false; }
    return true;
  });
  if (!list.length) return;
  state.uploading += list.length;
  const jobs = list.map(file => ({ file, pending: { name: file.name || "貼り付けた画像.png", size: file.size, loading: true } }));
  state.attachments.push(...jobs.map(job => job.pending)); renderAttachments();
  // Sequential uploads keep large document extraction from overloading the local app.
  for (const { file, pending } of jobs) {
    try {
      const data = new FormData(); data.append("file", file, pending.name);
      const result = await getJSON("/api/attachments", { method: "POST", body: data });
      const index = state.attachments.indexOf(pending);
      if (index !== -1) state.attachments.splice(index, 1, result);
      if (result.warning) toast(result.warning);
      if ((result.kind === "image" || file.type.startsWith("image/")) && !state.settings.vision_enabled) toast("画像を送るには、接続・設定で画像対応モデルと画像送信を有効にしてください。");
    } catch (error) { state.attachments = state.attachments.filter(item => item !== pending); toast(`${pending.name}: ${error.message}`, true); }
    finally { state.uploading--; renderAttachments(); }
  }
}

function setBusy(busy) {
  state.busy = busy;
  ui.sendButton.hidden = busy;
  ui.stopButton.hidden = !busy;
  ui.conversationStatus.classList.toggle("active", busy);
  $("#attach-button").disabled = busy;
  $("#url-button").disabled = busy;
  $("#web-toggle").disabled = busy;
  ui.messageInput.disabled = busy;
  updateSend();
}

async function streamEvents(response, onEvent) {
  if (!response.body) throw new Error("このブラウザーではストリーミング応答を読み込めません。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "", eventName = "message", dataLines = [];
  const dispatch = () => {
    if (dataLines.length) {
      const payload = dataLines.join("\n");
      if (payload !== "[DONE]") { let data; try { data = JSON.parse(payload); } catch (_) { throw new Error("サーバーから不正な応答を受信しました。"); } onEvent(eventName, data); }
    }
    eventName = "message"; dataLines = [];
  };
  const consumeLine = raw => {
    const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    if (!line) dispatch();
    else if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) !== -1) { consumeLine(buffer.slice(0, newline)); buffer = buffer.slice(newline + 1); }
      if (done) break;
    }
    if (buffer) consumeLine(buffer);
    dispatch();
  } catch (error) { await reader.cancel().catch(() => {}); throw error; }
  finally { reader.releaseLock(); }
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.busy || state.uploading) return;
  const message = ui.messageInput.value.trim();
  if (!message && !state.attachments.length) return;
  if (!configured()) { openSettings(); toast("APIのURLとモデルを設定してください。"); return; }
  if (!state.settings.vision_enabled && state.attachments.some(attachment => attachment.kind === "image")) { openSettings(); toast("画像を添付するには、画像対応モデルと画像送信を有効にしてください。"); return; }
  state.loadSequence++;
  const attachments = [...state.attachments];
  const controller = new AbortController(); state.abort = controller;
  setBusy(true);
  ui.conversationStatus.textContent = "モデルに接続しています…";
  let assistant, userMessage, fullText = "", renderTimer = null, done = false, streamError = "", submitted = false;
  const paint = () => { renderTimer = null; if (!assistant) return; const stick = nearBottom(); assistant.update(fullText); if (stick) scrollBottom(); };
  try {
    if (!state.currentId) {
      const conversation = await getJSON("/api/conversations", { method: "POST", body: JSON.stringify({}) , signal: controller.signal });
      state.currentId = conversation.id;
      ui.conversationTitle.textContent = conversation.title || "新しいチャット";
    }
    ui.welcome.hidden = true;
    userMessage = appendMessage({ role: "user", content: message, attachments, created_at: new Date().toISOString() });
    assistant = appendMessage({ role: "assistant", content: "", created_at: new Date().toISOString() });
    assistant.waiting();
    ui.messageInput.value = ""; resizeInput();
    state.attachments = []; renderAttachments(); scrollBottom();
    const response = await api("/api/chat", { method: "POST", signal: controller.signal, body: JSON.stringify({ conversation_id: state.currentId, message, attachment_ids: attachments.map(attachment => attachment.id), web_enabled: state.webEnabled }) });
    submitted = true;
    await streamEvents(response, (name, data) => {
      if (name === "meta" && data.conversation_id) state.currentId = data.conversation_id;
      if (name === "delta") { fullText += data.content || ""; ui.conversationStatus.textContent = "応答を作成しています…"; if (renderTimer === null) renderTimer = setTimeout(paint, STREAM_RENDER_INTERVAL_MS); }
      else if (name === "status") ui.conversationStatus.textContent = data.message || "処理しています…";
      else if (name === "source") assistant.addSource(data);
      else if (name === "warning") toast(data.message || "処理中に注意事項があります。");
      else if (name === "error") streamError = data.message || "モデルの応答中にエラーが発生しました。";
      else if (name === "done") { done = true; if (["stopped", "cancelled", "interrupted"].includes(data.status)) assistant.status.textContent = "応答を停止しました"; }
    });
    if (streamError) throw new Error(streamError);
    if (!done) throw new Error("応答の途中で接続が切れました。接続先を確認して、もう一度送信してください。");
    if (!fullText) assistant.update("応答が空でした。モデルの設定を確認してください。");
    ui.conversationStatus.textContent = "";
    $("#export-chat").hidden = false;
  } catch (error) {
    if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null; }
    if (assistant) {
      assistant.update(fullText || (error.name === "AbortError" ? "応答を停止しました。" : "応答を取得できませんでした。"));
      assistant.status.textContent = error.name === "AbortError" ? "停止しました" : error.message;
      assistant.status.classList.toggle("error", error.name !== "AbortError");
    }
    if (error.name === "AbortError") ui.conversationStatus.textContent = "応答を停止しました。";
    else { ui.conversationStatus.textContent = ""; toast(error.message, true); }
    if (!submitted && assistant) {
      userMessage.article.remove(); assistant.article.remove();
      ui.welcome.hidden = ui.messages.childElementCount > 0;
      ui.messageInput.value = message; state.attachments = attachments; renderAttachments(); resizeInput();
    }
  } finally {
    if (renderTimer !== null) { clearTimeout(renderTimer); paint(); }
    state.abort = null;
    setBusy(false);
    ui.messageInput.focus();
    try { await refreshHistory(); } catch (error) { toast(error.message, true); }
  }
}

function openSettings() {
  const form = ui.settingsForm;
  for (const key of ["base_url", "model", "system_prompt", "proxy_mode", "proxy_url", "ca_bundle", "temperature", "max_tokens", "context_chars"]) {
    const field = form.elements.namedItem(key);
    if (field) field.value = state.settings[key] ?? ({ proxy_mode: "direct", temperature: 0.7, max_tokens: 2048, context_chars: 60000 }[key] ?? "");
  }
  form.elements.vision_enabled.checked = Boolean(state.settings.vision_enabled);
  for (const key of ["api_key", "proxy_password"]) {
    form.elements.namedItem(key).value = "";
    form.elements.namedItem(`${key}_clear`).checked = false;
    form.elements.namedItem(key).placeholder = state.settings[`${key}_set`] ? "今回の起動中に設定済み（変更する場合のみ入力）" : "必要な場合のみ";
    $(`#${key.replaceAll("_", "-")}-clear-row`).hidden = !state.settings[`${key}_set`];
  }
  updateProxyFields();
  displayError(ui.settingsError, "");
  $("#models-status").textContent = "一覧の取得時に、接続設定を保存します。";
  closeSidebar();
  if (!ui.settingsDialog.open) ui.settingsDialog.showModal();
}

function updateProxyFields() { $("#manual-proxy-fields").hidden = $("#proxy-mode").value !== "manual"; }
function settingsPayload() {
  const form = ui.settingsForm;
  const payload = {};
  for (const key of ["base_url", "model", "system_prompt", "proxy_mode", "proxy_url", "ca_bundle"]) payload[key] = form.elements.namedItem(key).value.trim();
  for (const key of ["temperature", "max_tokens", "context_chars"]) payload[key] = Number(form.elements.namedItem(key).value);
  payload.vision_enabled = form.elements.vision_enabled.checked;
  for (const key of ["api_key", "proxy_password"]) {
    const value = form.elements.namedItem(key).value;
    if (value) payload[key] = value;
    if (form.elements.namedItem(`${key}_clear`).checked) payload[`${key}_clear`] = true;
  }
  return payload;
}

async function saveSettings(close = true) {
  if (!ui.settingsForm.reportValidity()) return false;
  displayError(ui.settingsError, "");
  $("#save-settings").disabled = true;
  try {
    const result = await getJSON("/api/settings", { method: "PUT", body: JSON.stringify(settingsPayload()) });
    state.settings = result.base_url !== undefined ? result : await getJSON("/api/settings");
    updateConnection();
    for (const key of ["api_key", "proxy_password"]) { ui.settingsForm.elements.namedItem(key).value = ""; ui.settingsForm.elements.namedItem(`${key}_clear`).checked = false; }
    if (close) { ui.settingsDialog.close(); toast("設定を保存しました。"); ui.messageInput.focus(); }
    return true;
  } catch (error) { displayError(ui.settingsError, error.message); return false; }
  finally { $("#save-settings").disabled = false; }
}

async function loadModels() {
  const button = $("#load-models"); button.disabled = true; button.textContent = "取得中…";
  try {
    if (!await saveSettings(false)) return;
    const response = await getJSON("/api/models");
    const list = $("#model-options"); list.replaceChildren();
    for (const model of response.models || []) { const option = element("option"); option.value = model; list.append(option); }
    if (!$("#model").value && response.models?.length) $("#model").value = response.models[0];
    $("#models-status").textContent = response.models?.length ? `${response.models.length} 件のモデルを取得しました。モデルを選んで設定を保存してください。` : "接続しましたが、モデルがありません。LLM側でモデルを読み込んでください。";
    $("#model").focus();
  } catch (error) { displayError(ui.settingsError, error.message); $("#models-status").textContent = "モデルを取得できませんでした。URLとLLMの起動状態を確認してください。"; }
  finally { button.disabled = false; button.textContent = "モデル一覧を取得"; }
}

async function attachURL(event) {
  event.preventDefault();
  if (state.attachments.length >= 10) { displayError(ui.urlError, "添付は1メッセージにつき10件までです。"); return; }
  const url = safeURL($("#web-url").value.trim());
  if (!url) { displayError(ui.urlError, "http:// または https:// から始まるURLを入力してください。"); return; }
  const button = $("#fetch-url"); button.disabled = true; button.textContent = "ページを取得中…"; displayError(ui.urlError, "");
  state.uploading++; updateSend();
  try {
    const attachment = await getJSON("/api/web", { method: "POST", body: JSON.stringify({ url }) });
    state.attachments.push(attachment);
    renderAttachments();
    ui.urlDialog.close();
    if (attachment.warning) toast(attachment.warning);
    else toast("Webページを添付しました。");
  } catch (error) { displayError(ui.urlError, error.message); }
  finally { state.uploading--; button.disabled = false; button.textContent = "取得して添付"; updateSend(); }
}

async function exportConversation() {
  if (!state.currentId) return;
  try {
    const response = await api(`/api/conversations/${encodeURIComponent(state.currentId)}/export`);
    const objectURL = URL.createObjectURL(await response.blob());
    const link = element("a"); link.href = objectURL;
    link.download = `${ui.conversationTitle.textContent.replace(/[<>:"/\\|?*\x00-\x1F]/g, "_").slice(0, 80) || "chat"}.md`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(objectURL), 1000);
  } catch (error) { toast(error.message, true); }
}

ui.composer.addEventListener("submit", sendMessage);
ui.messageInput.addEventListener("input", () => { resizeInput(); updateSend(); });
ui.messageInput.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229 && !event.ctrlKey && !event.altKey && !event.metaKey) { event.preventDefault(); if (!ui.sendButton.disabled) ui.composer.requestSubmit(); } });
ui.messageInput.addEventListener("paste", event => { const files = Array.from(event.clipboardData?.files || []); if (files.length) { event.preventDefault(); uploadFiles(files); } });
$("#new-chat").addEventListener("click", newConversation);
$("#stop-button").addEventListener("click", () => state.abort?.abort());
$("#export-chat").addEventListener("click", exportConversation);
$("#attach-button").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", event => { uploadFiles(event.target.files); event.target.value = ""; });
$("#url-button").addEventListener("click", () => { $("#web-url").value = ""; displayError(ui.urlError, ""); ui.urlDialog.showModal(); });
$("#url-form").addEventListener("submit", attachURL);
$("#web-toggle").addEventListener("click", () => { state.webEnabled = !state.webEnabled; $("#web-toggle").setAttribute("aria-pressed", String(state.webEnabled)); $("#network-note").textContent = state.webEnabled ? "Webはオン · このPCが外部サイトにアクセスします" : "Webはオフ · 添付資料は設定したLLMに送信されます"; });
$("#mobile-menu").addEventListener("click", () => { $("#sidebar").classList.add("open"); $("#sidebar-scrim").hidden = false; });
$("#sidebar-scrim").addEventListener("click", closeSidebar);
document.addEventListener("keydown", event => { if (event.key === "Escape") closeSidebar(); });
document.querySelectorAll("[data-open-settings]").forEach(button => button.addEventListener("click", openSettings));
document.querySelectorAll("[data-close-dialog]").forEach(button => button.addEventListener("click", () => document.getElementById(button.dataset.closeDialog).close()));
document.querySelectorAll("dialog").forEach(dialog => dialog.addEventListener("click", event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } }));
document.querySelectorAll("[data-suggestion]").forEach(button => button.addEventListener("click", () => { ui.messageInput.value = button.dataset.suggestion; resizeInput(); updateSend(); ui.messageInput.focus(); }));
ui.settingsForm.addEventListener("submit", event => { event.preventDefault(); saveSettings(true); });
$("#load-models").addEventListener("click", loadModels);
$("#proxy-mode").addEventListener("change", updateProxyFields);

document.addEventListener("dragenter", event => { if (Array.from(event.dataTransfer?.types || []).includes("Files")) { event.preventDefault(); state.dragDepth++; if (!document.querySelector("dialog[open]")) $("#drop-overlay").hidden = false; } });
document.addEventListener("dragover", event => { if (Array.from(event.dataTransfer?.types || []).includes("Files")) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; } });
document.addEventListener("dragleave", () => { state.dragDepth = Math.max(0, state.dragDepth - 1); if (!state.dragDepth) $("#drop-overlay").hidden = true; });
document.addEventListener("drop", event => { if (Array.from(event.dataTransfer?.types || []).includes("Files")) { event.preventDefault(); state.dragDepth = 0; $("#drop-overlay").hidden = true; if (!document.querySelector("dialog[open]")) uploadFiles(event.dataTransfer.files); } });
window.addEventListener("beforeunload", event => { if (state.busy || state.uploading) { event.preventDefault(); event.returnValue = ""; } });

async function initialize() {
  renderHistory();
  const results = await Promise.allSettled([getJSON("/api/settings"), getJSON("/api/conversations")]);
  if (results[0].status === "fulfilled") { state.settings = results[0].value; updateConnection(); }
  else toast(results[0].reason.message, true);
  if (results[1].status === "fulfilled") { state.conversations = results[1].value; renderHistory(); }
  else toast(results[1].reason.message, true);
  resizeInput();
}
initialize();
