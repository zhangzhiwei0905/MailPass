const form = document.querySelector("#query-form");
const emailInput = document.querySelector("#email-input");
const button = document.querySelector("#query-button");
const statusBox = document.querySelector("#status");
const result = document.querySelector("#result");
const recent = document.querySelector("#recent");
const recentList = document.querySelector("#recent-list");
const codeValue = document.querySelector("#code-value");
const copyCodeButton = document.querySelector("#copy-code-button");

function setStatus(message, tone = "") {
  statusBox.textContent = message;
  statusBox.dataset.tone = tone;
}

function formatTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function renderRecent(messages) {
  recentList.innerHTML = "";
  if (!messages.length) {
    recent.classList.add("hidden");
    return;
  }
  for (const message of messages) {
    const item = document.createElement("li");
    item.className = "recent-item";
    item.innerHTML = `
      <div class="recent-copy">
        <strong></strong>
        <span></span>
        <small></small>
      </div>
      <time></time>
    `;
    item.querySelector("strong").textContent = message.subject || "（无主题）";
    item.querySelector("span").textContent = message.preview || "";
    item.querySelector("small").textContent = message.from || "未知发件人";
    item.querySelector("time").textContent = formatTime(message.receivedDateTime);
    recentList.appendChild(item);
  }
  recent.classList.remove("hidden");
}

copyCodeButton.addEventListener("click", async () => {
  const code = codeValue.textContent.trim();
  if (!code || code === "--") {
    setStatus("当前没有可复制的验证码。", "warn");
    return;
  }
  await navigator.clipboard.writeText(code);
  setStatus("验证码已复制。", "ok");
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const email = emailInput.value.trim();
  result.classList.add("hidden");
  recent.classList.add("hidden");
  setStatus("正在查询最近邮件...");
  button.disabled = true;

  try {
    const response = await fetch("/api/messages/code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || "查询失败，请稍后再试。");
    }
    renderRecent(payload.recentMessages || []);
    if (!payload.found) {
      setStatus("暂未找到验证码，稍后可以再点一次。", "warn");
      return;
    }
    codeValue.textContent = payload.code;
    document.querySelector("#provider-value").textContent = payload.provider === "outlook" ? "Outlook" : "Edu Mail";
    document.querySelector("#message-subject").textContent = payload.message?.subject || "（无主题）";
    document.querySelector("#message-from").textContent = payload.message?.from || "未知发件人";
    document.querySelector("#message-recipient").textContent = payload.message?.recipient || payload.email || "未知收件人";
    document.querySelector("#message-time").textContent = formatTime(payload.message?.receivedDateTime);
    result.classList.remove("hidden");
    setStatus(payload.cached ? "已从短缓存返回，避免重复请求上游。" : "已找到最新验证码。", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    button.disabled = false;
  }
});
