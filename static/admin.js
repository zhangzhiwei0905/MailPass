const loginForm = document.querySelector("#login-form");
const adminWorkspace = document.querySelector("#admin-workspace");
const configForm = document.querySelector("#config-form");
const passwordInput = document.querySelector("#admin-password");
const baseUrlInput = document.querySelector("#base-url");
const apiTokenInput = document.querySelector("#api-token");
const statusBox = document.querySelector("#admin-status");
const outlookImportText = document.querySelector("#outlook-import-text");
const outlookImportFile = document.querySelector("#outlook-import-file");
const outlookImportButton = document.querySelector("#outlook-import-button");
const outlookImportResult = document.querySelector("#outlook-import-result");
const outlookAccountsBody = document.querySelector("#outlook-accounts-body");
const outlookListCount = document.querySelector("#outlook-list-count");
const outlookSearch = document.querySelector("#outlook-search");
const outlookUsedFilter = document.querySelector("#outlook-used-filter");
const outlookSelectAll = document.querySelector("#outlook-select-all");
const outlookExportButton = document.querySelector("#outlook-export-button");
const outlookDeleteSelectedButton = document.querySelector("#outlook-delete-selected-button");
const outlookCodeTestSelectedButton = document.querySelector("#outlook-code-test-selected-button");
const chatgptTestSelectedButton = document.querySelector("#chatgpt-test-selected-button");
const chatgptTestAllButton = document.querySelector("#chatgpt-test-all-button");
const testResultDialog = document.querySelector("#test-result-dialog");
const dialogTitle = document.querySelector("#dialog-title");
const dialogBody = document.querySelector("#dialog-body");
let searchTimer = 0;
let currentPage = 1;
let pageSize = 10;
let totalPages = 1;
let usedFilter = "all";
let activeChatGPTJobId = "";
let activeChatGPTPollTimer = 0;

function setStatus(message, tone = "") {
  statusBox.textContent = message;
  statusBox.dataset.tone = tone;
}

async function loadConfig() {
  const response = await fetch("/api/admin/config");
  if (!response.ok) return;
  const payload = await response.json();
  baseUrlInput.value = payload.baseUrl || "";
  apiTokenInput.placeholder = payload.apiTokenConfigured
    ? `当前：${payload.apiTokenMasked}`
    : "尚未配置 token";
  loginForm.classList.add("hidden");
  adminWorkspace.classList.remove("hidden");
  setStatus(payload.apiTokenConfigured ? "已加载配置。" : "请配置 Bearer token。");
  await loadOutlookAccounts();
}

function formatTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function selectedEmails() {
  return Array.from(outlookAccountsBody.querySelectorAll(".account-select:checked"))
    .map((input) => input.value);
}

function showChatGPTDialog(title, payload) {
  dialogTitle.textContent = title;
  const previousList = dialogBody.querySelector(".chatgpt-job-list");
  const previousScrollTop = previousList ? previousList.scrollTop : 0;
  const wasNearBottom = previousList
    ? previousList.scrollTop + previousList.clientHeight >= previousList.scrollHeight - 8
    : false;
  const accounts = payload.accounts || payload.results || [];
  const percent = payload.total ? Math.round(((payload.completed || 0) + (payload.cancelled || 0)) * 100 / payload.total) : 0;
  const rows = accounts
    .map((item) => {
      const status = item.status || (item.ok ? "ok" : "pending");
      const timeline = renderChatGPTTimeline(item, status);
      return `
        <li class="chatgpt-job-row" data-status="${escapeHtml(status)}">
          <strong>${escapeHtml(item.email || "")}</strong>
          <span class="chatgpt-status-pill" data-status="${escapeHtml(status)}">${escapeHtml(chatgptStatusLabel(status))}</span>
          <small>${escapeHtml(item.stageLabel || item.reason || chatgptStatusLabel(status))}</small>
          ${timeline}
        </li>
      `;
    })
    .join("");
  const cancelDisabled = payload.status !== "running" ? "disabled" : "";
  dialogBody.innerHTML = `
    <div class="job-summary" data-status="${escapeHtml(payload.status || "running")}">
      <div>
        <p>${escapeHtml(chatgptJobStatusText(payload))}</p>
        <span>等待 ${payload.pending || 0}，进行中 ${payload.running || 0}，已完成 ${payload.completed || 0}，已中断 ${payload.cancelled || 0}</span>
      </div>
      <button id="chatgpt-cancel-job-button" type="button" ${cancelDisabled}>中断任务</button>
    </div>
    <div class="job-progress" aria-label="检测进度"><span style="width: ${percent}%"></span></div>
    <div class="job-stats">
      <span data-tone="ok">完成 ${payload.success || 0}</span>
      <span data-tone="error">失败 ${payload.failed || 0}</span>
      <span>总数 ${payload.total || 0}</span>
    </div>
    ${rows ? `<ol class="dialog-list chatgpt-job-list">${rows}</ol>` : ""}
  `;
  if (!testResultDialog.open) testResultDialog.showModal();
  const nextList = dialogBody.querySelector(".chatgpt-job-list");
  if (nextList) {
    nextList.scrollTop = wasNearBottom ? nextList.scrollHeight : previousScrollTop;
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function chatgptJobStatusText(payload) {
  if (payload.status === "completed") return `检测完成：完成 ${payload.success || 0}，失败 ${payload.failed || 0}`;
  if (payload.status === "cancelled") return `检测已中断：已中断 ${payload.cancelled || 0} 个账号`;
  if (payload.status === "failed") return "检测任务异常结束";
  return `检测中：${payload.completed || 0} / ${payload.total || 0}`;
}

function renderChatGPTTimeline(item, status) {
  const events = (item.events || []).filter((event) => !String(event.stage || "").startsWith("debug_"));
  if (!events.length) return "";
  const lastIndex = events.length - 1;
  const isTerminal = !["pending", "running"].includes(status);
  return `
    <ol class="chatgpt-timeline">
      ${events.map((event, index) => {
        const stepState = index < lastIndex || (index === lastIndex && isTerminal)
          ? "done"
          : "active";
        const marker = stepState === "done" ? "✓" : String(index + 1);
        return `
          <li data-step-state="${stepState}">
            <span class="timeline-marker">${marker}</span>
            <div>
              <strong>${escapeHtml(event.message || event.stage || "")}</strong>
              ${event.at ? `<time>${escapeHtml(formatTime(event.at))}</time>` : ""}
            </div>
          </li>
        `;
      }).join("")}
      ${item.reason && status !== "ok" ? `
        <li data-step-state="error">
          <span class="timeline-marker">!</span>
          <div>
            <strong>${escapeHtml(chatgptStatusLabel(status))}</strong>
            <small>${escapeHtml(item.reason)}</small>
          </div>
        </li>
      ` : ""}
    </ol>
  `;
}

function showMessageDialog(title, message) {
  dialogTitle.textContent = title;
  dialogBody.innerHTML = `<p>${message}</p>`;
  testResultDialog.showModal();
}

function showCodeTestDialog(title, payload) {
  dialogTitle.textContent = title;
  const previousList = dialogBody.querySelector(".code-test-list");
  const previousScrollTop = previousList ? previousList.scrollTop : 0;
  const results = payload.results || [];
  const completed = results.filter((item) => ["ok", "error"].includes(item.status)).length;
  const total = payload.total || results.length;
  const rows = results.map((item) => {
    const status = item.status || (item.ok ? "ok" : "pending");
    const statusText = codeTestStatusLabel(status);
    const detail = codeTestResultSummary(item);
    const fullDetail = codeTestFullDetail(item);
    const message = item.message;
    return `
      <li class="code-test-row" data-status="${status}">
        <strong>${escapeHtml(item.email || "")}</strong>
        <span data-tone="${escapeHtml(status)}" title="${escapeHtml(fullDetail)}">${escapeHtml(statusText)}</span>
        <small title="${escapeHtml(fullDetail)}">${escapeHtml(detail)}</small>
        ${message ? `<small title="${escapeHtml(fullDetail)}">最近匹配：${escapeHtml(message.subject || "")} · ${escapeHtml(formatTime(message.receivedAt || message.receivedDateTime || ""))}</small>` : ""}
      </li>
    `;
  }).join("");
  dialogBody.innerHTML = `
    <div class="job-summary" data-status="${payload.failed ? "failed" : "completed"}">
      <div>
        <p>收码测试：${completed} / ${total}，正常 ${payload.success || 0}，异常 ${payload.failed || 0}</p>
        <span>按顺序检测账号，账号之间保留调用间隔。</span>
      </div>
    </div>
    ${rows ? `<ol class="dialog-list code-test-list">${rows}</ol>` : ""}
  `;
  if (!testResultDialog.open) testResultDialog.showModal();
  const nextList = dialogBody.querySelector(".code-test-list");
  if (nextList) nextList.scrollTop = previousScrollTop;
}

function codeTestStatusLabel(status) {
  const labels = {
    pending: "等待中",
    running: "测试中",
    ok: "正常",
    error: "异常",
  };
  return labels[status] || status || "等待中";
}

function codeTestResultSummary(item) {
  const status = item.status || (item.ok ? "ok" : "pending");
  if (status === "pending") return "等待开始";
  if (status === "running") return "正在读取邮箱";
  if (status === "ok") {
    return item.found ? "可收码，已找到验证码" : `可读取邮箱，最近邮件 ${Number(item.recentCount || 0)} 封`;
  }
  return "认证信息异常或读取失败";
}

function codeTestFullDetail(item) {
  const lines = [
    item.email || "",
    codeTestResultSummary(item),
  ];
  if (item.error) lines.push(`错误：${item.error}`);
  if (item.message?.subject) lines.push(`最近匹配：${item.message.subject}`);
  if (Number.isFinite(Number(item.recentCount))) lines.push(`最近邮件：${Number(item.recentCount || 0)} 封`);
  return lines.filter(Boolean).join("\n");
}

function sleep(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function exportEmails(emails, mode = "download") {
  if (!emails.length) {
    setStatus("请先选择要导出的邮箱。", "warn");
    return;
  }
  const response = await fetch("/api/admin/outlook/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ emails }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "导出失败。");
  const text = payload.text || (payload.lines || []).join("\n");
  if (mode === "copy") {
    await navigator.clipboard.writeText(text);
    setStatus("已复制完整邮箱信息。", "ok");
    showMessageDialog("复制成功", "已复制到剪切板。");
    return;
  }
  const blob = new Blob([text + "\n"], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "outlook-accounts.txt";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  setStatus(`已导出 ${payload.count || emails.length} 条邮箱信息。`, "ok");
}

async function deleteEmails(emails) {
  if (!emails.length) {
    setStatus("请先选择要删除的邮箱。", "warn");
    return;
  }
  if (!window.confirm(`确认删除选中的 ${emails.length} 个邮箱吗？`)) return;
  const response = await fetch("/api/admin/outlook/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ emails }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "删除失败。");
  setStatus(`已删除 ${payload.deleted || emails.length} 个邮箱。`, "ok");
  await loadOutlookAccounts();
}

async function testSelectedEmailCodes(emails) {
  if (!emails.length) {
    setStatus("请先选择要测试收码的邮箱。", "warn");
    return;
  }
  const results = emails.map((email) => ({
    email,
    status: "pending",
    ok: false,
    recentCount: 0,
  }));
  const payload = { results, total: emails.length, success: 0, failed: 0 };
  showCodeTestDialog("选中邮箱收码测试", payload);
  setStatus(`收码测试中：共 ${emails.length} 个邮箱，按顺序执行。`);
  for (let index = 0; index < emails.length; index += 1) {
    const email = emails[index];
    results[index] = { ...results[index], status: "running" };
    showCodeTestDialog("选中邮箱收码测试", payload);
    setStatus(`收码测试中：${index + 1} / ${emails.length}，当前 ${email}`);
    try {
      const response = await fetch("/api/admin/outlook/code-tests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ emails: [email], intervalSeconds: 0 }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "收码测试失败。");
      const result = (body.results || [])[0] || { email, ok: false, error: "没有返回测试结果。" };
      results[index] = {
        email,
        ...result,
        status: result.ok ? "ok" : "error",
      };
    } catch (error) {
      results[index] = {
        email,
        ok: false,
        status: "error",
        error: error.message || "网络请求失败。",
        recentCount: 0,
      };
    }
    payload.success = results.filter((item) => item.status === "ok").length;
    payload.failed = results.filter((item) => item.status === "error").length;
    showCodeTestDialog("选中邮箱收码测试", payload);
    if (index < emails.length - 1) await sleep(1000);
  }
  const success = payload.success || 0;
  const failed = payload.failed || 0;
  setStatus(`收码测试完成：正常 ${success}，异常 ${failed}。`, failed ? "warn" : "ok");
  await loadOutlookAccounts();
}

function chatgptStatusLabel(status) {
  const labels = {
    pending: "等待中",
    running: "检测中",
    start: "开始",
    ok: "可登录",
    blocked: "账号已被封禁或停用",
    auth_error: "认证失败",
    challenge: "需验证",
    mail_error: "收码失败",
    browser_error: "浏览器失败",
    timeout: "超时",
    cancelled: "已中断",
    email_wait: "等待邮箱响应",
    password_wait: "等待密码响应",
    code_wait: "等待验证码响应",
    plan: "检测订阅",
  };
  return labels[status] || status || "未检测";
}

function chatgptPlanLabel(status) {
  const labels = {
    plus: "Plus",
    pro: "Pro",
    free: "Free",
    unknown: "订阅未知",
  };
  return labels[status] || status || "";
}

async function startChatGPTLoginTest(payload, title) {
  const response = await fetch("/api/admin/chatgpt/login-tests", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const created = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(created.detail || "ChatGPT 登录检测启动失败。");
  activeChatGPTJobId = created.jobId || "";
  setStatus(`ChatGPT 登录检测已启动，共 ${created.total || 0} 个账号。`);
  showChatGPTDialog(title, { status: "running", total: created.total, pending: created.total, running: 0, completed: 0, success: 0, failed: 0, cancelled: 0, accounts: [] });
  await pollChatGPTLoginTest(created.jobId, title);
}

async function pollChatGPTLoginTest(jobId, title) {
  let payload = null;
  for (;;) {
    window.clearTimeout(activeChatGPTPollTimer);
    const response = await fetch(`/api/admin/chatgpt/login-tests/${encodeURIComponent(jobId)}`);
    payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "ChatGPT 登录检测查询失败。");
    showChatGPTDialog(title, payload);
    setStatus(`ChatGPT 检测中：${payload.completed || 0} / ${payload.total || 0}，成功 ${payload.success || 0}，失败 ${payload.failed || 0}。`, payload.failed ? "warn" : "");
    if (payload.status !== "running") break;
    await new Promise((resolve) => {
      activeChatGPTPollTimer = window.setTimeout(resolve, 1600);
    });
  }
  activeChatGPTJobId = "";
  setStatus(chatgptJobStatusText(payload), payload.failed || payload.status === "cancelled" ? "warn" : "ok");
  await loadOutlookAccounts();
}

function renderOutlookAccounts(accounts, meta = {}) {
  outlookAccountsBody.innerHTML = "";
  const total = Number.isFinite(meta.total) ? meta.total : accounts.length;
  const filtered = Number.isFinite(meta.filtered) ? meta.filtered : accounts.length;
  totalPages = Number.isFinite(meta.totalPages) ? meta.totalPages : 1;
  currentPage = Number.isFinite(meta.page) ? meta.page : 1;
  pageSize = Number.isFinite(meta.pageSize) ? meta.pageSize : pageSize;
  const hasFilter = Boolean(meta.query || (meta.usedFilter && meta.usedFilter !== "all"));
  outlookListCount.textContent = `${hasFilter ? `${filtered} / ${total}` : total} 个账号，第 ${currentPage} / ${totalPages} 页`;
  outlookSelectAll.checked = false;
  if (!accounts.length) {
    const row = document.createElement("tr");
    row.innerHTML = `<td colspan="10" class="empty-cell">${hasFilter ? "没有匹配的邮箱。" : "还没有导入 Outlook / Hotmail 账号。"}</td>`;
    outlookAccountsBody.appendChild(row);
    renderPagination();
    return;
  }
  for (const account of accounts) {
    const row = document.createElement("tr");
    const status = account.lastTestStatus || "ok";
    row.innerHTML = `
      <td><input class="account-select" type="checkbox"></td>
      <td class="email-cell">
        <div class="email-inline">
          <span class="email-text"></span>
          <button class="icon-button mini-copy" type="button" data-action="copy-email" aria-label="复制邮箱" title="复制邮箱">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <rect x="9" y="9" width="11" height="11" rx="2"></rect>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
            </svg>
          </button>
        </div>
      </td>
      <td>
        <div class="alias-editor">
          <input class="alias-input" type="text" maxlength="80" placeholder="添加别名">
          <button type="button" data-action="save-alias">保存</button>
        </div>
      </td>
      <td><span class="status-pill" title=""></span></td>
      <td><span class="chatgpt-status-pill" title=""></span></td>
      <td>
        <label class="usage-switch">
          <input class="used-toggle" type="checkbox" aria-label="切换使用状态">
          <span aria-hidden="true"></span>
          <em></em>
        </label>
      </td>
      <td></td>
      <td></td>
      <td></td>
      <td class="row-actions">
        <button type="button" data-action="copy">复制</button>
        <button type="button" data-action="delete" class="danger">删除</button>
      </td>
    `;
    row.dataset.email = account.email;
    row.querySelector(".account-select").value = account.email;
    row.querySelector(".email-text").textContent = account.email;
    row.querySelector(".alias-input").value = account.alias || "";
    row.querySelector(".alias-input").dataset.originalValue = account.alias || "";
    row.querySelector(".status-pill").textContent = status;
    row.querySelector(".status-pill").dataset.status = status;
    row.querySelector(".status-pill").title = status === "error" ? (account.lastError || "测试失败") : "ok";
    const chatgptStatus = account.chatgptLastLoginStatus || "";
    const planLabel = chatgptStatus === "ok" ? chatgptPlanLabel(account.chatgptLastPlanStatus) : "";
    row.querySelector(".chatgpt-status-pill").textContent = planLabel
      ? `${chatgptStatusLabel(chatgptStatus)} / ${planLabel}`
      : chatgptStatusLabel(chatgptStatus);
    row.querySelector(".chatgpt-status-pill").dataset.status = chatgptStatus || "pending";
    row.querySelector(".chatgpt-status-pill").title = account.chatgptLastPlanReason || account.chatgptLastLoginReason || chatgptStatusLabel(chatgptStatus);
    row.querySelector(".used-toggle").checked = Boolean(account.used);
    row.querySelector(".usage-switch em").textContent = account.used ? "已使用" : "未使用";
    row.children[6].textContent = account.clientIdMasked || "--";
    row.children[7].textContent = account.refreshTokenMasked || "--";
    row.children[8].textContent = formatTime(account.updatedAt);
    outlookAccountsBody.appendChild(row);
  }
  renderPagination();
}

async function loadOutlookAccounts() {
  const query = outlookSearch?.value?.trim() || "";
  const params = new URLSearchParams({
    page: String(currentPage),
    pageSize: String(pageSize),
  });
  if (query) params.set("q", query);
  if (usedFilter !== "all") params.set("used", usedFilter);
  const url = `/api/admin/outlook/accounts?${params.toString()}`;
  const response = await fetch(url);
  if (!response.ok) return;
  const payload = await response.json();
  renderOutlookAccounts(payload.accounts || [], {
    total: payload.total,
    filtered: payload.filtered,
    query: payload.query,
    usedFilter: payload.usedFilter || usedFilter,
    page: payload.page,
    pageSize: payload.pageSize,
    totalPages: payload.totalPages,
  });
}

function renderPagination() {
  let pager = document.querySelector("#outlook-pager");
  if (!pager) {
    pager = document.createElement("div");
    pager.id = "outlook-pager";
    pager.className = "pager";
    document.querySelector(".account-table-wrap").after(pager);
  }
  pager.innerHTML = `
    <button type="button" data-page="prev" ${currentPage <= 1 ? "disabled" : ""}>上一页</button>
    <span>${currentPage} / ${totalPages}</span>
    <button type="button" data-page="next" ${currentPage >= totalPages ? "disabled" : ""}>下一页</button>
  `;
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setStatus("正在登录...");
  const response = await fetch("/api/admin/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: passwordInput.value }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    setStatus(payload.detail || "登录失败。", "error");
    return;
  }
  passwordInput.value = "";
  await loadConfig();
});

configForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setStatus("正在保存...");
  const response = await fetch("/api/admin/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      baseUrl: baseUrlInput.value,
      apiToken: apiTokenInput.value,
    }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    setStatus(payload.detail || "保存失败。", "error");
    return;
  }
  apiTokenInput.value = "";
  apiTokenInput.placeholder = payload.apiTokenConfigured
    ? `当前：${payload.apiTokenMasked}`
    : "尚未配置 token";
  setStatus("配置已保存，token 不会明文展示。", "ok");
});

outlookImportFile.addEventListener("change", async () => {
  const file = outlookImportFile.files?.[0];
  if (!file) return;
  outlookImportText.value = await file.text();
  outlookImportResult.textContent = `已读取文件：${file.name}`;
});

outlookImportButton.addEventListener("click", async () => {
  outlookImportButton.disabled = true;
  outlookImportResult.textContent = "正在导入...";
  try {
    const response = await fetch("/api/admin/outlook/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: outlookImportText.value }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "导入失败。");
    const errorText = (payload.errors || []).map((item) => `第 ${item.line} 行：${item.reason}`).join("；");
    const summary = `本次 ${payload.total || 0} 条，成功 ${payload.imported || 0} 条（新增 ${payload.created || 0}，覆盖 ${payload.updated || 0}），失败 ${payload.failed || 0} 条`;
    outlookImportResult.textContent = errorText
      ? `${summary}。${errorText}`
      : `${summary}。`;
    outlookImportText.value = "";
    await loadOutlookAccounts();
  } catch (error) {
    outlookImportResult.textContent = error.message;
  } finally {
    outlookImportButton.disabled = false;
  }
});

outlookAccountsBody.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  const row = event.target.closest("tr");
  if (!button || !row?.dataset.email) return;
  const email = row.dataset.email;
  const originalContent = button.innerHTML;
  button.disabled = true;
  try {
    if (button.dataset.action === "copy") {
      await exportEmails([email], "copy");
      return;
    }
    if (button.dataset.action === "copy-email") {
      await navigator.clipboard.writeText(email);
      setStatus("邮箱已复制。", "ok");
      showMessageDialog("复制成功", "邮箱已复制到剪切板。");
      return;
    }
    if (button.dataset.action === "save-alias") {
      await saveAliasInput(row.querySelector(".alias-input"), button);
      return;
    }
    if (button.dataset.action === "delete") {
      const response = await fetch(`/api/admin/outlook/accounts/${encodeURIComponent(email)}`, { method: "DELETE" });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "删除失败。");
      }
      setStatus(`已删除 ${email}`, "ok");
      await loadOutlookAccounts();
      return;
    }
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    button.innerHTML = originalContent;
    button.disabled = false;
  }
});

outlookAccountsBody.addEventListener("change", async (event) => {
  if (event.target.matches(".used-toggle")) {
    const input = event.target;
    const row = input.closest("tr");
    const email = row?.dataset.email;
    if (!email) return;
    const label = row.querySelector(".usage-switch em");
    const nextUsed = input.checked;
    input.disabled = true;
    try {
      const response = await fetch(`/api/admin/outlook/accounts/${encodeURIComponent(email)}/used`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ used: nextUsed }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "更新使用状态失败。");
      label.textContent = nextUsed ? "已使用" : "未使用";
      setStatus(`${email} 已标记为${nextUsed ? "已使用" : "未使用"}。`, "ok");
      if (usedFilter !== "all") await loadOutlookAccounts();
    } catch (error) {
      input.checked = !nextUsed;
      label.textContent = input.checked ? "已使用" : "未使用";
      setStatus(error.message, "error");
    } finally {
      input.disabled = false;
    }
    return;
  }
  if (!event.target.matches(".account-select")) return;
  const boxes = Array.from(outlookAccountsBody.querySelectorAll(".account-select"));
  outlookSelectAll.checked = boxes.length > 0 && boxes.every((box) => box.checked);
});

outlookAccountsBody.addEventListener("keydown", async (event) => {
  if (!event.target.matches(".alias-input") || event.key !== "Enter") return;
  event.preventDefault();
  await saveAliasInput(event.target, event.target.closest("tr")?.querySelector('[data-action="save-alias"]'));
});

async function saveAliasInput(input, button = null) {
  const row = input.closest("tr");
  const email = row?.dataset.email;
  const alias = input.value.trim();
  if (!email || alias === input.dataset.originalValue) return;
  input.disabled = true;
  if (button) button.disabled = true;
  try {
    const response = await fetch(`/api/admin/outlook/accounts/${encodeURIComponent(email)}/alias`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alias }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "保存别名失败。");
    input.value = payload.account?.alias || alias;
    input.dataset.originalValue = input.value;
    setStatus(`${email} 的别名已保存。`, "ok");
  } catch (error) {
    input.value = input.dataset.originalValue || "";
    setStatus(error.message, "error");
  } finally {
    input.disabled = false;
    if (button) button.disabled = false;
  }
}

outlookSelectAll.addEventListener("change", () => {
  for (const box of outlookAccountsBody.querySelectorAll(".account-select")) {
    box.checked = outlookSelectAll.checked;
  }
});

outlookExportButton.addEventListener("click", async () => {
  outlookExportButton.disabled = true;
  try {
    await exportEmails(selectedEmails(), "download");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    outlookExportButton.disabled = false;
  }
});

outlookDeleteSelectedButton.addEventListener("click", async () => {
  outlookDeleteSelectedButton.disabled = true;
  try {
    await deleteEmails(selectedEmails());
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    outlookDeleteSelectedButton.disabled = false;
  }
});

outlookCodeTestSelectedButton.addEventListener("click", async () => {
  outlookCodeTestSelectedButton.disabled = true;
  try {
    await testSelectedEmailCodes(selectedEmails());
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    outlookCodeTestSelectedButton.disabled = false;
  }
});

chatgptTestSelectedButton.addEventListener("click", async () => {
  chatgptTestSelectedButton.disabled = true;
  try {
    const emails = selectedEmails();
    if (!emails.length) {
      setStatus("请先选择要检测的邮箱。", "warn");
      return;
    }
    await startChatGPTLoginTest({ emails }, "ChatGPT 选中账号检测");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    chatgptTestSelectedButton.disabled = false;
  }
});

chatgptTestAllButton.addEventListener("click", async () => {
  chatgptTestAllButton.disabled = true;
  try {
    await startChatGPTLoginTest({ all: true }, "ChatGPT 全部账号检测");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    chatgptTestAllButton.disabled = false;
  }
});

dialogBody.addEventListener("click", async (event) => {
  const button = event.target.closest("#chatgpt-cancel-job-button");
  if (!button || !activeChatGPTJobId) return;
  button.disabled = true;
  button.textContent = "中断中...";
  try {
    const response = await fetch(`/api/admin/chatgpt/login-tests/${encodeURIComponent(activeChatGPTJobId)}`, { method: "DELETE" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "中断任务失败。");
    showChatGPTDialog(dialogTitle.textContent || "ChatGPT 登录检测", payload);
    setStatus(chatgptJobStatusText(payload), "warn");
  } catch (error) {
    setStatus(error.message, "error");
    button.disabled = false;
    button.textContent = "中断任务";
  }
});

outlookSearch.addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  currentPage = 1;
  searchTimer = window.setTimeout(() => {
    loadOutlookAccounts();
  }, 180);
});

outlookUsedFilter.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-used-filter]");
  if (!button) return;
  usedFilter = button.dataset.usedFilter || "all";
  currentPage = 1;
  for (const item of outlookUsedFilter.querySelectorAll("button")) {
    item.classList.toggle("active", item === button);
  }
  loadOutlookAccounts();
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("#outlook-pager button");
  if (!button) return;
  currentPage += button.dataset.page === "next" ? 1 : -1;
  loadOutlookAccounts();
});

loadConfig();
