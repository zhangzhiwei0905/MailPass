from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import logging
import os
import re
import secrets
import uuid
import time
from dataclasses import dataclass, field
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


DEFAULT_EDU_MAIL_BASE_URL = "https://inbox.beastxa.com"
DEFAULT_MAIL_PAGE_SIZE = 20
OUTLOOK_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
CODE_PATTERNS = (
    re.compile(r"(?:verification|verify|login|sign[- ]?in|security|code)[^\d]{0,40}(\d{4,8})", re.I),
    re.compile(r"(?:验证码|代码|校验码)[^0-9]{0,20}(\d{4,8})"),
    re.compile(r"\b(\d{6})\b"),
)
OPENAI_SENDER_PATTERN = re.compile(r"(^|@|\.)openai\.com$|(^|@|\.)chatgpt\.com$", re.I)
OPENAI_MESSAGE_PATTERN = re.compile(r"openai|chatgpt", re.I)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("edu_mail_web")


class Settings(BaseModel):
    bind_host: str = Field(default="0.0.0.0")
    bind_port: int = Field(default=8091)
    edu_mail_base_url: str = Field(default=DEFAULT_EDU_MAIL_BASE_URL)
    edu_mail_api_token: str = Field(default="")
    admin_password: str = Field(default="")
    session_secret: str = Field(default="")
    public_rate_limit_requests: int = Field(default=12)
    public_rate_limit_window_seconds: int = Field(default=60)
    admin_rate_limit_requests: int = Field(default=5)
    admin_rate_limit_window_seconds: int = Field(default=300)
    upstream_timeout_seconds: float = Field(default=15.0)
    upstream_max_concurrency: int = Field(default=20)
    outlook_bulk_test_concurrency: int = Field(default=3)
    chatgpt_login_concurrency: int = Field(default=2)
    chatgpt_login_timeout_seconds: float = Field(default=180.0)
    chatgpt_code_poll_seconds: float = Field(default=60.0)
    chatgpt_code_poll_interval_seconds: float = Field(default=2.0)
    message_cache_ttl_seconds: int = Field(default=8)
    data_dir: str = Field(default="/app/data")
    config_file: str = Field(default="")
    session_cookie_secure: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    service_name: str = Field(default="edu-mail-web")
    service_version: str = Field(default="1.0.0")


class CodeQueryRequest(BaseModel):
    email: str
    limit: Optional[int] = None


class AdminLoginRequest(BaseModel):
    password: str


class AdminConfigRequest(BaseModel):
    baseUrl: Optional[str] = None
    apiToken: Optional[str] = None


class OutlookImportRequest(BaseModel):
    text: str


class OutlookExportRequest(BaseModel):
    emails: List[str]


class OutlookDeleteRequest(BaseModel):
    emails: List[str]


class OutlookCodeTestRequest(BaseModel):
    emails: List[str]
    intervalSeconds: float = Field(default=1.0)


class OutlookUsedRequest(BaseModel):
    used: bool


class OutlookAliasRequest(BaseModel):
    alias: str = ""


class ChatGPTLoginTestRequest(BaseModel):
    emails: List[str] = Field(default_factory=list)
    all: bool = False


@dataclass
class FetchResult:
    messages: List[Dict[str, Any]]
    next_refresh_token: str = ""
    provider: str = ""


@dataclass
class ChatGPTLoginResult:
    status: str
    reason: str = ""
    stage: str = ""
    plan_status: str = ""
    plan_reason: str = ""


@dataclass
class ChatGPTLoginJob:
    job_id: str
    status: str
    total: int
    completed: int = 0
    running: int = 0
    pending: int = 0
    success: int = 0
    failed: int = 0
    cancelled: int = 0
    results: List[Dict[str, Any]] = field(default_factory=list)
    accounts: List[Dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


ChatGPTCodeFetcher = Callable[[Dict[str, Any], float], Awaitable[str]]
ChatGPTProgressReporter = Callable[[str, str], None]


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = max(1, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self._hits: Dict[str, List[float]] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        window_start = now - self.window_seconds
        hits = [value for value in self._hits.get(key, []) if value >= window_start]
        if len(hits) >= self.limit:
            retry_after = max(1, int(self.window_seconds - (now - hits[0])))
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请稍后再试。",
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)
        self._hits[key] = hits


class TTLCache:
    def __init__(self, ttl_seconds: int, max_items: int = 256):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_items = max(8, int(max_items))
        self._items: OrderedDict[str, Tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        item = self._items.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        self._items[key] = (time.monotonic() + self.ttl_seconds, value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)


class RuntimeConfig:
    def __init__(self, settings: Settings):
        self.config_file = Path(settings.config_file or Path(settings.data_dir) / "config.json")
        self.default_base_url = settings.edu_mail_base_url
        self.default_api_token = settings.edu_mail_api_token
        self.base_url = normalize_base_url(settings.edu_mail_base_url)
        self.api_token = normalize_api_token(settings.edu_mail_api_token)
        self.outlook_accounts: List[Dict[str, Any]] = []
        self.refresh_from_disk()

    def apply_stored_config(self, stored: Dict[str, Any]) -> None:
        edu_config = stored.get("edu") if isinstance(stored.get("edu"), dict) else {}
        self.base_url = normalize_base_url(edu_config.get("baseUrl") or stored.get("baseUrl") or self.default_base_url)
        self.api_token = normalize_api_token(edu_config.get("apiToken") or stored.get("apiToken") or self.default_api_token)
        self.outlook_accounts = normalize_outlook_accounts(stored.get("outlookAccounts"))

    def load_stored_config(self) -> Dict[str, Any]:
        try:
            if not self.config_file.exists():
                return {}
            payload = json.loads(self.config_file.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load config file %s: %s", self.config_file, exc)
            return {}

    def refresh_from_disk(self) -> None:
        self.apply_stored_config(self.load_stored_config())

    def persist(self, merge_disk_accounts: bool = True) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        outlook_accounts = normalize_outlook_accounts(self.outlook_accounts)
        if merge_disk_accounts:
            existing_accounts = normalize_outlook_accounts(self.load_stored_config().get("outlookAccounts"))
            by_email = {account["email"]: account for account in existing_accounts}
            by_email.update({account["email"]: account for account in outlook_accounts})
            outlook_accounts = sorted(by_email.values(), key=lambda item: item["email"])
            self.outlook_accounts = outlook_accounts
        payload = {
            "edu": {
                "baseUrl": self.base_url,
                "apiToken": self.api_token,
            },
            "outlookAccounts": outlook_accounts,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        tmp_path = self.config_file.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.config_file)


def load_settings(env: Optional[Dict[str, str]] = None) -> Settings:
    source = env if env is not None else os.environ

    def get(name: str, default: str = "") -> str:
        return str(source.get(name, default))

    return Settings(
        bind_host=get("BIND_HOST", "0.0.0.0"),
        bind_port=int(get("PORT", "8091")),
        edu_mail_base_url=get("EDU_MAIL_BASE_URL", DEFAULT_EDU_MAIL_BASE_URL),
        edu_mail_api_token=get("EDU_MAIL_API_TOKEN", ""),
        admin_password=get("ADMIN_PASSWORD", ""),
        session_secret=get("SESSION_SECRET", ""),
        public_rate_limit_requests=int(get("PUBLIC_RATE_LIMIT_REQUESTS", "12")),
        public_rate_limit_window_seconds=int(get("PUBLIC_RATE_LIMIT_WINDOW_SECONDS", "60")),
        admin_rate_limit_requests=int(get("ADMIN_RATE_LIMIT_REQUESTS", "5")),
        admin_rate_limit_window_seconds=int(get("ADMIN_RATE_LIMIT_WINDOW_SECONDS", "300")),
        upstream_timeout_seconds=float(get("UPSTREAM_TIMEOUT_SECONDS", "15")),
        upstream_max_concurrency=int(get("UPSTREAM_MAX_CONCURRENCY", "20")),
        outlook_bulk_test_concurrency=int(get("OUTLOOK_BULK_TEST_CONCURRENCY", "3")),
        chatgpt_login_concurrency=int(get("CHATGPT_LOGIN_CONCURRENCY", "2")),
        chatgpt_login_timeout_seconds=float(get("CHATGPT_LOGIN_TIMEOUT_SECONDS", "180")),
        chatgpt_code_poll_seconds=float(get("CHATGPT_CODE_POLL_SECONDS", "60")),
        chatgpt_code_poll_interval_seconds=float(get("CHATGPT_CODE_POLL_INTERVAL_SECONDS", "2")),
        message_cache_ttl_seconds=int(get("MESSAGE_CACHE_TTL_SECONDS", "8")),
        data_dir=get("DATA_DIR", "/app/data"),
        config_file=get("CONFIG_FILE", ""),
        session_cookie_secure=get("SESSION_COOKIE_SECURE", "0").lower() in ("1", "true", "yes", "on"),
        log_level=get("LOG_LEVEL", "INFO").upper(),
        service_name=get("SERVICE_NAME", "edu-mail-web"),
        service_version=get("SERVICE_VERSION", "1.0.0"),
    )


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def first_non_empty(values: List[Any]) -> str:
    for value in values:
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_api_token(raw_value: Optional[str] = "") -> str:
    value = str(raw_value or "").strip()
    if not value:
        return ""
    bearer_match = re.search(r"(?:authorization\s*:\s*)?bearer\s+([^\s\"']+)", value, re.I)
    if bearer_match:
        return bearer_match.group(1).strip()
    value = re.sub(r"^authorization\s*:\s*", "", value, flags=re.I).strip()
    value = re.sub(r"^bearer\s+", "", value, flags=re.I).strip()
    return value.strip("\"'")


def mask_token(token: Optional[str] = "") -> str:
    return mask_secret(normalize_api_token(token))


def mask_secret(value: Optional[str] = "") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) < 12:
        return "configured"
    return f"{text[:4]}...{text[-4:]}"


def safe_email_label(email: Optional[str] = "") -> str:
    text = str(email or "").strip().lower()
    if "@" not in text:
        return "unknown"
    local, domain = text.rsplit("@", 1)
    local_label = f"{local[:2]}***" if local else "***"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{local_label}@{domain}#{digest}"


def normalize_base_url(raw_value: Optional[str] = "") -> str:
    value = str(raw_value or "").strip() or DEFAULT_EDU_MAIL_BASE_URL
    if not re.match(r"^[a-zA-Z][a-zA-Z\d+\-.]*://", value):
        value = f"https://{value}"
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Edu Mail API 地址格式无效。")
    path = parsed.path or ""
    public_index = path.find("/public-api/")
    if public_index >= 0:
        path = path[:public_index]
    path = path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def normalize_email(value: Optional[str] = "") -> str:
    email = str(value or "").strip().lower()
    if not EMAIL_PATTERN.match(email):
        raise ValueError("邮箱格式无效。")
    return email


def email_domain(email: str) -> str:
    return normalize_email(email).rsplit("@", 1)[1]


def is_edu_domain(email: str) -> bool:
    return "edu" in email_domain(email)


def is_outlook_domain(email: str) -> bool:
    return email_domain(email) in OUTLOOK_DOMAINS


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "used", "已使用"}


def normalize_outlook_account(raw_account: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw_account, dict):
        return None
    try:
        email = normalize_email(raw_account.get("email"))
    except ValueError:
        return None
    client_id = str(raw_account.get("clientId") or raw_account.get("client_id") or "").strip()
    refresh_token = str(raw_account.get("refreshToken") or raw_account.get("refresh_token") or "").strip()
    if not client_id or not refresh_token:
        return None
    password = str(raw_account.get("password") or "").strip()
    created_at = str(raw_account.get("createdAt") or now_iso()).strip()
    updated_at = str(raw_account.get("updatedAt") or created_at).strip()
    account = {
        "email": email,
        "alias": str(raw_account.get("alias") or "").strip()[:80],
        "password": password,
        "clientId": client_id,
        "refreshToken": refresh_token,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "used": coerce_bool(raw_account.get("used")),
        "status": str(raw_account.get("status") or "imported").strip() or "imported",
        "lastTestAt": str(raw_account.get("lastTestAt") or "").strip(),
        "lastTestStatus": str(raw_account.get("lastTestStatus") or "").strip(),
        "lastError": str(raw_account.get("lastError") or "").strip(),
        "chatgptLastLoginAt": str(raw_account.get("chatgptLastLoginAt") or "").strip(),
        "chatgptLastLoginStatus": str(raw_account.get("chatgptLastLoginStatus") or "").strip(),
        "chatgptLastLoginReason": str(raw_account.get("chatgptLastLoginReason") or "").strip(),
        "chatgptLastLoginStage": str(raw_account.get("chatgptLastLoginStage") or "").strip(),
        "chatgptLastPlanStatus": str(raw_account.get("chatgptLastPlanStatus") or "").strip(),
        "chatgptLastPlanReason": str(raw_account.get("chatgptLastPlanReason") or "").strip(),
    }
    return account


def normalize_outlook_accounts(raw_accounts: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_accounts, list):
        return []
    accounts_by_email: Dict[str, Dict[str, Any]] = {}
    for raw_account in raw_accounts:
        account = normalize_outlook_account(raw_account)
        if account:
            accounts_by_email[account["email"]] = account
    return sorted(accounts_by_email.values(), key=lambda item: item["email"])


def parse_outlook_import_text(text: str) -> Dict[str, Any]:
    accounts: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    total = 0
    for line_number, raw_line in enumerate(str(text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line_number == 1 and re.fullmatch(r"账号----密码----ID----Token", line, re.I):
            continue
        total += 1
        parts = [part.strip() for part in line.split("----")]
        if len(parts) < 4:
            errors.append({"line": line_number, "reason": "格式应为：邮箱号----密码----客户端 ID----刷新令牌"})
            continue
        email, password, client_id, refresh_token = parts[:4]
        try:
            normalized_email = normalize_email(email)
        except ValueError:
            errors.append({"line": line_number, "reason": "邮箱格式无效"})
            continue
        if not client_id or not refresh_token:
            errors.append({"line": line_number, "reason": "客户端 ID 和刷新令牌不能为空"})
            continue
        timestamp = now_iso()
        accounts.append({
            "email": normalized_email,
            "alias": "",
            "password": password,
            "clientId": client_id,
            "refreshToken": refresh_token,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "used": False,
            "status": "imported",
            "lastTestAt": "",
            "lastTestStatus": "",
            "lastError": "",
            "chatgptLastLoginAt": "",
            "chatgptLastLoginStatus": "",
            "chatgptLastLoginReason": "",
            "chatgptLastLoginStage": "",
            "chatgptLastPlanStatus": "",
            "chatgptLastPlanReason": "",
        })
    return {"accounts": accounts, "errors": errors, "total": total}


def strip_html_tags(value: Optional[str] = "") -> str:
    text = str(value or "")
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def parse_message_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if 0 < timestamp < 100000000000:
            timestamp *= 1000
        return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{10,13}", text):
        timestamp = float(text)
        if 0 < timestamp < 100000000000:
            timestamp *= 1000
        return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).isoformat()
    except ValueError:
        return text


def message_rows(payload: Any) -> List[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    candidates = [
        payload.get("messages"),
        payload.get("data"),
        payload.get("items"),
        payload.get("list"),
        payload.get("rows"),
        payload.get("records"),
        payload.get("results"),
        payload.get("data", {}).get("messages") if isinstance(payload.get("data"), dict) else None,
        payload.get("data", {}).get("items") if isinstance(payload.get("data"), dict) else None,
        payload.get("data", {}).get("list") if isinstance(payload.get("data"), dict) else None,
        payload.get("data", {}).get("rows") if isinstance(payload.get("data"), dict) else None,
        payload.get("data", {}).get("records") if isinstance(payload.get("data"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
    return []


def normalize_message(row: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(row, dict):
        return None
    address = first_non_empty([
        row.get("alias"),
        row.get("to"),
        row.get("toEmail"),
        row.get("to_email"),
        row.get("recipient"),
        row.get("address"),
        row.get("email"),
        row.get("mailbox"),
    ]).lower()
    subject = first_non_empty([row.get("subject"), row.get("title")])
    raw_from = row.get("from")
    from_address = first_non_empty([
        raw_from.get("email") if isinstance(raw_from, dict) else raw_from,
        row.get("sender"),
        row.get("fromEmail"),
        row.get("from_email"),
        row.get("mailFrom"),
    ])
    html_content = first_non_empty([row.get("html"), row.get("bodyHtml"), row.get("body_html"), row.get("contentHtml")])
    text_content = first_non_empty([
        row.get("text"),
        row.get("body"),
        row.get("bodyText"),
        row.get("body_text"),
        row.get("content"),
        row.get("plainText"),
        row.get("preview"),
        row.get("snippet"),
    ])
    body_preview = re.sub(r"\s+", " ", text_content or strip_html_tags(html_content)).strip()
    return {
        "id": first_non_empty([row.get("id"), row.get("messageId"), row.get("message_id"), row.get("mailId"), row.get("mail_id")]),
        "address": address,
        "subject": subject,
        "from": {"emailAddress": {"address": from_address}},
        "bodyPreview": body_preview,
        "raw": html_content or text_content,
        "receivedDateTime": parse_message_time(first_non_empty([
            row.get("receivedAt"),
            row.get("received_at"),
            row.get("createdAt"),
            row.get("created_at"),
            row.get("date"),
            row.get("time"),
            row.get("timestamp"),
        ])),
    }


def normalize_messages(payload: Any) -> List[Dict[str, Any]]:
    return [message for row in message_rows(payload) if (message := normalize_message(row))]


def normalize_microsoft_message(row: Any, mailbox: str = "INBOX") -> Optional[Dict[str, Any]]:
    if not isinstance(row, dict):
        return None
    sender = row.get("From") or row.get("from") or {}
    email_address = sender.get("EmailAddress") or sender.get("emailAddress") or {} if isinstance(sender, dict) else {}
    recipients = []
    for key in ("ToRecipients", "toRecipients", "CcRecipients", "ccRecipients", "BccRecipients", "bccRecipients"):
        raw_items = row.get(key) or []
        if not isinstance(raw_items, list):
            raw_items = [raw_items]
        for item in raw_items:
            if isinstance(item, dict):
                item_email = item.get("EmailAddress") or item.get("emailAddress") or {}
                address = str(item_email.get("Address") or item_email.get("address") or "").strip()
                if address:
                    recipients.append(address)
    body = row.get("Body") or row.get("body") or {}
    return {
        "id": first_non_empty([row.get("Id"), row.get("id"), row.get("internetMessageId")]),
        "address": "",
        "mailbox": mailbox,
        "subject": first_non_empty([row.get("Subject"), row.get("subject")]),
        "from": {
            "emailAddress": {
                "address": str(email_address.get("Address") or email_address.get("address") or "").strip(),
                "name": str(email_address.get("Name") or email_address.get("name") or "").strip(),
            },
        },
        "bodyPreview": first_non_empty([row.get("BodyPreview"), row.get("bodyPreview")]),
        "raw": str(body.get("Content") or body.get("content") or "").strip() if isinstance(body, dict) else "",
        "receivedDateTime": parse_message_time(first_non_empty([row.get("ReceivedDateTime"), row.get("receivedDateTime")])),
        "recipients": recipients,
    }


def extract_verification_code(message: Dict[str, Any]) -> str:
    source = " ".join([
        str(message.get("subject") or ""),
        str(message.get("bodyPreview") or ""),
        strip_html_tags(message.get("raw") or ""),
    ])
    for pattern in CODE_PATTERNS:
        match = pattern.search(source)
        if match:
            return match.group(1)
    return ""


def message_timestamp_ms(message: Dict[str, Any]) -> float:
    value = str(message.get("receivedDateTime") or "").strip()
    if not value:
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return 0


def message_sender(message: Dict[str, Any]) -> str:
    raw_from = message.get("from") or {}
    email_address = raw_from.get("emailAddress") if isinstance(raw_from, dict) else {}
    return str(email_address.get("address") if isinstance(email_address, dict) else "").strip().lower()


def message_search_text(message: Dict[str, Any]) -> str:
    return "\n".join([
        str(message.get("subject") or ""),
        str(message.get("bodyPreview") or ""),
        strip_html_tags(message.get("raw") or ""),
        message_sender(message),
    ])


def is_openai_message(message: Dict[str, Any]) -> bool:
    sender = message_sender(message)
    return bool(OPENAI_SENDER_PATTERN.search(sender) or OPENAI_MESSAGE_PATTERN.search(message_search_text(message)))


def extract_chatgpt_code_from_messages(messages: List[Dict[str, Any]], after_timestamp_ms: float) -> Optional[Dict[str, Any]]:
    sorted_messages = sort_messages_newest_first(messages)
    for message in sorted_messages:
        timestamp_ms = message_timestamp_ms(message)
        if timestamp_ms and timestamp_ms < after_timestamp_ms:
            continue
        if not is_openai_message(message):
            continue
        code = extract_verification_code(message)
        if code:
            return {"code": code, "message": message}
    return None


def sort_messages_newest_first(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(messages, key=message_timestamp_ms, reverse=True)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def session_signature(session_id: str, expires_at: int, secret: str) -> str:
    return hashlib.sha256(f"{session_id}:{expires_at}:{secret}".encode("utf-8")).hexdigest()


def build_session_cookie(session_id: str, secret: str, max_age: int = 86400) -> str:
    expires_at = int(time.time()) + max_age
    return f"{session_id}.{expires_at}.{session_signature(session_id, expires_at, secret)}"


def parse_session_cookie(value: str, secret: str) -> str:
    session_id, expires_text, signature = str(value or "").split(".", 2) if str(value or "").count(".") == 2 else ("", "", "")
    if not session_id or not expires_text or not signature:
        return ""
    try:
        expires_at = int(expires_text)
    except ValueError:
        return ""
    if expires_at < int(time.time()):
        return ""
    expected = session_signature(session_id, expires_at, secret)
    return session_id if secrets.compare_digest(signature, expected) else ""


def render_template(name: str) -> str:
    return (BASE_DIR / "static" / name).read_text(encoding="utf-8")


def public_config(runtime: RuntimeConfig) -> Dict[str, Any]:
    runtime.refresh_from_disk()
    return {
        "baseUrl": runtime.base_url,
        "apiTokenConfigured": bool(runtime.api_token),
        "apiTokenMasked": mask_token(runtime.api_token),
    }


async def fetch_messages_from_upstream(
    runtime: RuntimeConfig,
    settings: Settings,
    semaphore: asyncio.Semaphore,
    email: str,
    limit: int,
) -> List[Dict[str, Any]]:
    if not runtime.api_token:
        raise HTTPException(status_code=503, detail="管理员尚未配置 Edu Mail Bearer token。")
    url = f"{runtime.base_url}/public-api/messages"
    headers = {
        "Authorization": f"Bearer {runtime.api_token}",
        "Accept": "application/json",
    }
    async with semaphore:
        try:
            async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
                response = await client.get(url, params={"alias": email, "limit": limit}, headers=headers)
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="Edu Mail 请求超时，请稍后再试。") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Edu Mail 请求失败：{exc}") from exc

    text = response.text
    try:
        payload = response.json() if text else {}
    except json.JSONDecodeError:
        payload = text
    if response.status_code >= 400:
        detail = payload.get("message") if isinstance(payload, dict) else ""
        raise HTTPException(status_code=502, detail=f"Edu Mail 返回错误：{detail or response.status_code}")
    if isinstance(payload, dict):
        code = payload.get("code", payload.get("statusCode"))
        ok_value = payload.get("ok", payload.get("success"))
        if ok_value is False or (code is not None and int(code) >= 400):
            raise HTTPException(status_code=502, detail=f"Edu Mail 业务错误：{payload.get('message') or payload.get('error') or code}")
    return normalize_messages(payload)


async def exchange_microsoft_refresh_token(account: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    body = {
        "client_id": account["clientId"],
        "grant_type": "refresh_token",
        "refresh_token": account["refreshToken"],
        "scope": "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read",
    }
    async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
        response = await client.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Microsoft token 请求失败：{extract_upstream_error(response)}")
    payload = response.json()
    if not payload.get("access_token"):
        raise HTTPException(status_code=502, detail="Microsoft token 响应缺少 access_token。")
    return payload


def extract_upstream_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error_description") or payload.get("error") or payload.get("message")
            if error:
                return str(error)
    except json.JSONDecodeError:
        pass
    return response.text[:240] or f"HTTP {response.status_code}"


async def fetch_microsoft_messages(access_token: str, settings: Settings, mailbox: str, limit: int) -> List[Dict[str, Any]]:
    mailbox_id = "junkemail" if mailbox.lower().startswith("junk") else "inbox"
    top = min(max(limit, 1), 30)
    url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{mailbox_id}/messages"
    params = {
        "$top": str(top),
        "$select": "id,internetMessageId,subject,from,bodyPreview,receivedDateTime,toRecipients,ccRecipients,bccRecipients",
        "$orderby": "receivedDateTime desc",
    }
    async with httpx.AsyncClient(timeout=settings.upstream_timeout_seconds) as client:
        response = await client.get(
            url,
            params=params,
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Microsoft Graph 请求失败：{extract_upstream_error(response)}")
    payload = response.json()
    raw_messages = payload.get("value") if isinstance(payload, dict) else []
    if not isinstance(raw_messages, list):
        return []
    return [message for item in raw_messages if (message := normalize_microsoft_message(item, mailbox))]


async def fetch_outlook_account_messages(account: Dict[str, Any], limit: int, settings: Settings) -> FetchResult:
    token_data = await exchange_microsoft_refresh_token(account, settings)
    messages: List[Dict[str, Any]] = []
    for mailbox in ("INBOX", "Junk"):
        messages.extend(await fetch_microsoft_messages(token_data["access_token"], settings, mailbox, limit))
    return FetchResult(
        messages=messages,
        next_refresh_token=str(token_data.get("refresh_token") or "").strip(),
        provider="outlook",
    )


def find_outlook_account(runtime: RuntimeConfig, email: str) -> Optional[Dict[str, Any]]:
    runtime.refresh_from_disk()
    normalized_email = normalize_email(email)
    return next((account for account in runtime.outlook_accounts if account.get("email") == normalized_email), None)


def normalize_alias(value: Optional[str] = "") -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:80]


def find_outlook_account_by_alias(runtime: RuntimeConfig, alias: str) -> Optional[Dict[str, Any]]:
    runtime.refresh_from_disk()
    normalized_alias = normalize_alias(alias).casefold()
    if not normalized_alias:
        return None
    return next(
        (
            account
            for account in runtime.outlook_accounts
            if normalize_alias(account.get("alias")).casefold() == normalized_alias
        ),
        None,
    )


def upsert_outlook_accounts(runtime: RuntimeConfig, imported_accounts: List[Dict[str, Any]]) -> Dict[str, int]:
    runtime.refresh_from_disk()
    by_email = {account["email"]: account for account in runtime.outlook_accounts}
    imported_count = 0
    created_count = 0
    updated_count = 0
    for account in imported_accounts:
        existing = by_email.get(account["email"])
        if existing and existing.get("createdAt"):
            account["createdAt"] = existing["createdAt"]
            account["alias"] = existing.get("alias", "")
            account["used"] = coerce_bool(existing.get("used"))
            updated_count += 1
        else:
            created_count += 1
        by_email[account["email"]] = account
        imported_count += 1
    runtime.outlook_accounts = sorted(by_email.values(), key=lambda item: item["email"])
    runtime.persist()
    return {
        "imported": imported_count,
        "created": created_count,
        "updated": updated_count,
    }


def public_outlook_account(account: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "email": account.get("email", ""),
        "alias": account.get("alias", ""),
        "status": account.get("status", ""),
        "passwordMasked": mask_secret(account.get("password", "")),
        "clientIdMasked": mask_secret(account.get("clientId", "")),
        "refreshTokenMasked": mask_secret(account.get("refreshToken", "")),
        "createdAt": account.get("createdAt", ""),
        "updatedAt": account.get("updatedAt", ""),
        "used": coerce_bool(account.get("used")),
        "lastTestAt": account.get("lastTestAt", ""),
        "lastTestStatus": account.get("lastTestStatus", ""),
        "lastError": account.get("lastError", ""),
        "chatgptLastLoginAt": account.get("chatgptLastLoginAt", ""),
        "chatgptLastLoginStatus": account.get("chatgptLastLoginStatus", ""),
        "chatgptLastLoginReason": account.get("chatgptLastLoginReason", ""),
        "chatgptLastLoginStage": account.get("chatgptLastLoginStage", ""),
        "chatgptLastPlanStatus": account.get("chatgptLastPlanStatus", ""),
        "chatgptLastPlanReason": account.get("chatgptLastPlanReason", ""),
    }


def outlook_import_line(account: Dict[str, Any]) -> str:
    return "----".join([
        str(account.get("email") or ""),
        str(account.get("password") or ""),
        str(account.get("clientId") or ""),
        str(account.get("refreshToken") or ""),
    ])


def filter_outlook_accounts(
    accounts: List[Dict[str, Any]],
    query: Optional[str] = "",
    used_filter: Optional[str] = "",
) -> List[Dict[str, Any]]:
    needle = str(query or "").strip().lower()
    normalized_used_filter = str(used_filter or "").strip().lower()
    filtered = accounts
    if normalized_used_filter in {"used", "true", "1"}:
        filtered = [account for account in filtered if coerce_bool(account.get("used"))]
    elif normalized_used_filter in {"unused", "false", "0"}:
        filtered = [account for account in filtered if not coerce_bool(account.get("used"))]
    if needle:
        filtered = [
            account
            for account in filtered
            if needle in str(account.get("email") or "").lower()
            or needle in str(account.get("alias") or "").lower()
        ]
    return filtered


def paginate_items(items: List[Dict[str, Any]], page: int, page_size: int) -> Tuple[List[Dict[str, Any]], int, int, int]:
    safe_page_size = min(max(int(page_size or 20), 1), 100)
    total_pages = max(1, (len(items) + safe_page_size - 1) // safe_page_size)
    safe_page = min(max(int(page or 1), 1), total_pages)
    start = (safe_page - 1) * safe_page_size
    return items[start:start + safe_page_size], safe_page, safe_page_size, total_pages


def update_outlook_account(runtime: RuntimeConfig, updated_account: Dict[str, Any]) -> None:
    normalized_email = normalize_email(updated_account.get("email"))
    runtime.refresh_from_disk()
    by_email = {account["email"]: account for account in runtime.outlook_accounts}
    if normalized_email not in by_email:
        return
    by_email[normalized_email].update(updated_account)
    runtime.outlook_accounts = sorted(by_email.values(), key=lambda item: item["email"])
    runtime.persist()


async def test_single_outlook_account(
    runtime: RuntimeConfig,
    settings: Settings,
    account: Dict[str, Any],
    fetcher: Any,
) -> Dict[str, Any]:
    working_account = dict(account)
    try:
        fetch_result = await fetcher(working_account, DEFAULT_MAIL_PAGE_SIZE, settings)
        if fetch_result.next_refresh_token:
            working_account["refreshToken"] = fetch_result.next_refresh_token
        working_account["lastTestAt"] = now_iso()
        working_account["lastTestStatus"] = "ok"
        working_account["lastError"] = ""
        working_account["updatedAt"] = now_iso()
        update_outlook_account(runtime, working_account)
        messages = sort_messages_newest_first(fetch_result.messages)
        return {
            "ok": True,
            "account": public_outlook_account(working_account),
            "recentMessages": [summarize_message(message) for message in messages[:5]],
        }
    except HTTPException as exc:
        working_account["lastTestAt"] = now_iso()
        working_account["lastTestStatus"] = "error"
        working_account["lastError"] = str(exc.detail)
        working_account["updatedAt"] = now_iso()
        update_outlook_account(runtime, working_account)
        raise
    except Exception as exc:
        working_account["lastTestAt"] = now_iso()
        working_account["lastTestStatus"] = "error"
        working_account["lastError"] = str(exc)
        working_account["updatedAt"] = now_iso()
        update_outlook_account(runtime, working_account)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def is_chatgpt_success_status(status: str) -> bool:
    return str(status or "").strip().lower() in {"ok", "blocked"}


def chatgpt_status_message(status: str, reason: str = "") -> str:
    labels = {
        "ok": "登录成功",
        "blocked": "账号封禁或停用",
        "auth_error": "认证失败",
        "challenge": "需要额外验证",
        "mail_error": "收码失败",
        "browser_error": "浏览器或网络失败",
        "timeout": "检测超时",
        "cancelled": "任务已中断",
    }
    label = labels.get(str(status or "").strip().lower(), status or "未知状态")
    reason = str(reason or "").strip()
    return f"{label}：{reason}" if reason else label


async def fetch_chatgpt_session_plan(page: Any) -> Tuple[str, str]:
    try:
        response = await page.request.get("https://chatgpt.com/api/auth/session", timeout=30000)
        payload = await response.json()
    except Exception as exc:
        return "unknown", f"读取 /api/auth/session 失败：{str(exc)[:240]}"
    plan_type, plan_path = find_plan_type(payload)
    if plan_type == "plus":
        return "plus", f"{plan_path}=plus"
    if plan_type:
        return plan_type, f"{plan_path}={plan_type}"
    return "unknown", f"api/auth/session 未返回 planType，HTTP {getattr(response, 'status', '')}。"


def find_plan_type(value: Any, path: str = "$") -> Tuple[str, str]:
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = f"{path}.{key}"
            if str(key).lower() == "plantype":
                plan_type = str(item or "").strip().lower()
                if plan_type:
                    return plan_type, next_path
            found, found_path = find_plan_type(item, next_path)
            if found:
                return found, found_path
    if isinstance(value, list):
        for index, item in enumerate(value):
            found, found_path = find_plan_type(item, f"{path}[{index}]")
            if found:
                return found, found_path
    return "", ""


async def detect_chatgpt_plan(page: Any) -> Tuple[str, str]:
    session_status, session_reason = await fetch_chatgpt_session_plan(page)
    if session_status != "unknown":
        return session_status, session_reason
    with contextlib.suppress(Exception):
        await page.goto("https://chatgpt.com/#settings/Subscription", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
    text = (await visible_page_excerpt(page)).lower()
    if re.search(r"\b(chatgpt\s+plus|plus plan|manage subscription|subscription.*plus)\b", text):
        return "plus", "检测到 ChatGPT Plus 订阅。"
    if re.search(r"\b(chatgpt\s+pro|pro plan|subscription.*pro)\b", text):
        return "pro", "检测到 ChatGPT Pro 订阅。"
    if re.search(r"\b(upgrade plan|upgrade to plus|get plus|free plan|try plus)\b", text):
        return "free", "未检测到 Plus 订阅，页面显示可升级/免费计划。"
    return "unknown", await visible_page_excerpt(page)


def should_bypass_proxy_for_chatgpt() -> bool:
    no_proxy = ",".join([
        os.environ.get("NO_PROXY", ""),
        os.environ.get("no_proxy", ""),
    ]).lower()
    entries = [entry.strip() for entry in no_proxy.split(",") if entry.strip()]
    return any(entry in {"*", "chatgpt.com", ".chatgpt.com", "openai.com", ".openai.com"} for entry in entries)


def system_proxy_url() -> str:
    if should_bypass_proxy_for_chatgpt():
        return ""
    return first_non_empty([
        os.environ.get("HTTPS_PROXY"),
        os.environ.get("https_proxy"),
        os.environ.get("HTTP_PROXY"),
        os.environ.get("http_proxy"),
        os.environ.get("ALL_PROXY"),
        os.environ.get("all_proxy"),
    ])


def persist_chatgpt_login_result(runtime: RuntimeConfig, account: Dict[str, Any], result: ChatGPTLoginResult) -> Dict[str, Any]:
    updated_account = dict(account)
    updated_account["chatgptLastLoginAt"] = now_iso()
    updated_account["chatgptLastLoginStatus"] = str(result.status or "").strip() or "browser_error"
    updated_account["chatgptLastLoginReason"] = str(result.reason or "").strip()[:500]
    updated_account["chatgptLastLoginStage"] = str(result.stage or "").strip()[:120]
    updated_account["chatgptLastPlanStatus"] = str(result.plan_status or "").strip()[:80]
    updated_account["chatgptLastPlanReason"] = str(result.plan_reason or "").strip()[:500]
    updated_account["updatedAt"] = now_iso()
    update_outlook_account(runtime, updated_account)
    runtime.refresh_from_disk()
    return find_outlook_account(runtime, updated_account["email"]) or updated_account


async def default_chatgpt_login_runner(
    account: Dict[str, Any],
    settings: Settings,
    code_fetcher: ChatGPTCodeFetcher,
    progress: Optional[Callable[[str, str], None]] = None,
) -> ChatGPTLoginResult:
    def report(stage: str, message: str) -> None:
        if progress:
            progress(stage, message)

    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        return ChatGPTLoginResult(
            status="browser_error",
            reason="Playwright 未安装或浏览器运行环境不可用。",
            stage="startup",
        )

    timeout_ms = max(1000, int(settings.chatgpt_login_timeout_seconds * 1000))
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    proxy_url = system_proxy_url()
    proxy = {"server": proxy_url} if proxy_url else None
    email = account.get("email", "")
    password = account.get("password", "")
    email_label = safe_email_label(email)
    logger.info(
        "chatgpt_login_start email=%s timeout_ms=%s proxy=%s",
        email_label,
        timeout_ms,
        "enabled" if proxy else "disabled",
    )

    async with async_playwright() as playwright:
        launch_options: Dict[str, Any] = {"headless": True, "args": launch_args, "proxy": proxy}
        chromium_path = first_non_empty([
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ])
        if chromium_path and Path(chromium_path).exists():
            launch_options["executable_path"] = chromium_path
        browser = await playwright.chromium.launch(**launch_options)
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        current_stage = "startup"
        try:
            requested_at_ms = time.time() * 1000
            current_stage = "browser"
            report("browser", "打开 ChatGPT 登录页")
            await page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=timeout_ms)
            await report_chatgpt_snapshot(page, "browser", email_label, progress, "登录页快照：")
            current_stage = "email"
            report("email", "提交邮箱")
            await submit_chatgpt_identifier(page, email, timeout_ms)
            await report_chatgpt_snapshot(page, "email", email_label, progress, "提交邮箱后快照：")
            current_stage = "email_wait"
            report("email_wait", "等待 ChatGPT 响应邮箱提交结果")
            state = await wait_for_chatgpt_state(
                page,
                {"password", "verification", "challenge", "blocked", "auth_error", "ok"},
                min(timeout_ms, 30000),
                progress=progress,
                progress_stage="email_wait",
                email_label=email_label,
            )
            if state == "email":
                current_stage = "email_retry"
                report("email_retry", "邮箱页仍未跳转，重试提交邮箱")
                await report_chatgpt_snapshot(page, "email_retry_before", email_label, progress, "重试前快照：")
                await submit_chatgpt_identifier(page, email, timeout_ms)
                await report_chatgpt_snapshot(page, "email_retry_after", email_label, progress, "重试后快照：")
                current_stage = "email_wait"
                state = await wait_for_chatgpt_state(
                    page,
                    {"password", "verification", "challenge", "blocked", "auth_error", "ok"},
                    min(timeout_ms, 30000),
                    progress=progress,
                    progress_stage="email_wait_retry",
                    email_label=email_label,
                )
            if state == "password":
                if not password:
                    return ChatGPTLoginResult(status="auth_error", reason="账号缺少 ChatGPT 登录密码。", stage="password")
                current_stage = "password"
                report("password", "提交密码")
                await submit_chatgpt_password(page, password, timeout_ms)
                await report_chatgpt_snapshot(page, "password", email_label, progress, "提交密码后快照：")
                current_stage = "password_wait"
                report("password_wait", "等待 ChatGPT 响应密码提交结果")
                state = await wait_for_chatgpt_state(
                    page,
                    {"verification", "challenge", "blocked", "auth_error", "ok"},
                    min(timeout_ms, 30000),
                    progress=progress,
                    progress_stage="password_wait",
                    email_label=email_label,
                )
            if state == "verification":
                current_stage = "mail"
                report("mail", "等待邮箱验证码")
                code = await code_fetcher(account, requested_at_ms)
                current_stage = "code"
                report("code", "提交邮箱验证码")
                await submit_chatgpt_code(page, code, timeout_ms)
                await report_chatgpt_snapshot(page, "code", email_label, progress, "提交验证码后快照：")
                current_stage = "code_wait"
                report("code_wait", "等待 ChatGPT 响应验证码提交结果")
                state = await wait_for_chatgpt_state(
                    page,
                    {"ok", "challenge", "blocked", "auth_error"},
                    min(timeout_ms, 30000),
                    progress=progress,
                    progress_stage="code_wait",
                    email_label=email_label,
                )
            if state == "ok":
                current_stage = "plan"
                report("plan", "检测订阅状态")
                plan_status, plan_reason = await detect_chatgpt_plan(page)
                report("done", "登录成功")
                return ChatGPTLoginResult(
                    status="ok",
                    reason="ChatGPT 登录成功。",
                    stage="done",
                    plan_status=plan_status,
                    plan_reason=plan_reason,
                )
            if state in {"blocked", "auth_error", "challenge"}:
                await report_chatgpt_snapshot(page, state, email_label, progress, "最终状态快照：")
                return ChatGPTLoginResult(status=state, reason=await visible_page_excerpt(page), stage=state)
            await report_chatgpt_snapshot(page, state or "unknown", email_label, progress, "未识别状态快照：")
            excerpt = await visible_page_excerpt(page)
            reason = f"页面状态未识别：{state or 'unknown'}。{excerpt}".strip()
            return ChatGPTLoginResult(status="browser_error", reason=reason[:500], stage=state or "unknown")
        except PlaywrightTimeoutError as exc:
            logger.warning("chatgpt_login_timeout email=%s stage=%s error=%s", email_label, current_stage, str(exc)[:500])
            return ChatGPTLoginResult(status="timeout", reason=f"{current_stage}: {str(exc)}"[:500], stage=current_stage or "timeout")
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("chatgpt_login_browser_error email=%s stage=%s error=%s", email_label, current_stage, str(exc)[:500])
            return ChatGPTLoginResult(status="browser_error", reason=f"{current_stage}: {str(exc)}"[:500], stage=current_stage or "browser")
        finally:
            await context.close()
            await browser.close()


async def visible_page_excerpt(page: Any) -> str:
    with contextlib.suppress(Exception):
        text = await page.locator("body").inner_text(timeout=3000)
        return re.sub(r"\s+", " ", text).strip()[:500]
    return ""


async def chatgpt_debug_snapshot(page: Any) -> str:
    url = str(getattr(page, "url", "") or "")
    with contextlib.suppress(Exception):
        title = await page.title()
    title = locals().get("title", "")
    selectors = {
        "email": 'input[type="email"], input[name="email"], input[autocomplete="email"]',
        "text": 'input[type="text"]',
        "password": 'input[type="password"], input[name="password"], input[autocomplete="current-password"]',
        "code": 'input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[aria-label*="code" i]',
        "captcha": 'iframe[src*="captcha"], iframe[src*="challenge"], [class*="captcha" i], [id*="captcha" i]',
    }
    flags: List[str] = []
    for name, selector in selectors.items():
        value = "?"
        with contextlib.suppress(Exception):
            value = "1" if await page.locator(selector).first.is_visible(timeout=800) else "0"
        flags.append(f"{name}={value}")
    buttons: List[str] = []
    with contextlib.suppress(Exception):
        raw_buttons = await page.locator("button").evaluate_all(
            """
            (buttons) => buttons
                .filter((button) => {
                    const style = window.getComputedStyle(button);
                    const rect = button.getBoundingClientRect();
                    return style.display !== "none"
                        && style.visibility !== "hidden"
                        && rect.width > 0
                        && rect.height > 0;
                })
                .slice(0, 8)
                .map((button) => (button.innerText || button.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim())
            """
        )
        buttons = [str(text or "")[:60] for text in raw_buttons if str(text or "").strip()]
    excerpt = await visible_page_excerpt(page)
    return f"url={url} title={title[:80]} inputs[{', '.join(flags)}] buttons={buttons} text={excerpt[:240]}"


async def report_chatgpt_snapshot(
    page: Any,
    stage: str,
    email_label: str,
    progress: Optional[ChatGPTProgressReporter] = None,
    prefix: str = "",
) -> None:
    snapshot = await chatgpt_debug_snapshot(page)
    message = f"{prefix}{snapshot}" if prefix else snapshot
    logger.info("chatgpt_login_snapshot email=%s stage=%s %s", email_label, stage, snapshot)
    if progress:
        progress(f"debug_{stage}", message[:900])


async def classify_chatgpt_page(page: Any) -> str:
    url = str(page.url or "").lower()
    text = (await visible_page_excerpt(page)).lower()
    if re.search(r"deleted|deactivated|suspended|disabled|banned|account.*closed|年龄|停用|封禁|删除", text):
        return "blocked"
    if re.search(r"incorrect password|wrong password|invalid password|authentication method|密码.*错误|登录方式", text):
        return "auth_error"
    if re.search(r"captcha|mfa|multi-factor|authenticator|push notification|verify your identity|cloudflare|手机号|验证码应用|人机", text):
        return "challenge"
    if await first_visible_locator(page, [
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[aria-label*="code" i]',
    ]):
        return "verification"
    if await first_visible_locator(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
    ]):
        return "password"
    if "chatgpt.com" in url and ("/auth" not in url and "/log-in" not in url) and re.search(r"new chat|message chatgpt|chatgpt", text):
        return "ok"
    if await first_visible_locator(page, [
        'input[type="email"]',
        'input[name="email"]',
        'input[autocomplete="email"]',
    ]):
        return "email"
    return "unknown"


async def wait_for_chatgpt_state(
    page: Any,
    target_states: Set[str],
    timeout_ms: int,
    classifier: Callable[[Any], Awaitable[str]] = classify_chatgpt_page,
    progress: Optional[ChatGPTProgressReporter] = None,
    progress_stage: str = "state",
    email_label: str = "",
) -> str:
    deadline = time.monotonic() + max(1, timeout_ms / 1000)
    last_state = ""
    last_reported_state = ""
    while True:
        last_state = await classifier(page)
        if last_state != last_reported_state:
            last_reported_state = last_state
            url = str(getattr(page, "url", "") or "")
            logger.info(
                "chatgpt_login_state email=%s stage=%s state=%s url=%s",
                email_label or "unknown",
                progress_stage,
                last_state,
                url,
            )
            if progress:
                progress(progress_stage, f"页面状态：{last_state or 'unknown'}，URL：{url[:180]}")
        if last_state in target_states:
            return last_state
        if time.monotonic() >= deadline:
            return last_state or "unknown"
        await page.wait_for_timeout(500)


async def first_visible_locator(page: Any, selectors: List[str]) -> Optional[Any]:
    for selector in selectors:
        with contextlib.suppress(Exception):
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=1000):
                return locator
    return None


async def click_submit(page: Any, source_locator: Optional[Any] = None) -> None:
    if source_locator:
        with contextlib.suppress(Exception):
            submitted = await source_locator.evaluate(
                """
                (element) => {
                    const forbidden = /(google|apple|phone|try it first)/i;
                    const isVisible = (node) => {
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== "none"
                            && style.visibility !== "hidden"
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    const textOf = (node) => [
                        node.innerText || "",
                        node.getAttribute("aria-label") || "",
                    ].join(" ");
                    const form = element.closest("form");
                    const root = form || element.parentElement || document;
                    const buttons = Array.from(root.querySelectorAll("button"))
                        .filter((button) => !button.disabled && isVisible(button))
                        .filter((button) => !forbidden.test(textOf(button)));
                    const submit = buttons.find((button) => button.type === "submit")
                        || buttons.find((button) => /(continue|next|log in|继续|登录)/i.test(textOf(button)));
                    if (submit) {
                        submit.click();
                        return true;
                    }
                    if (form && typeof form.requestSubmit === "function") {
                        form.requestSubmit();
                        return true;
                    }
                    return false;
                }
                """
            )
            if submitted:
                return
        with contextlib.suppress(Exception):
            await source_locator.press("Enter", timeout=5000)
            return
    button = await first_visible_locator(page, [
        'button[type="submit"]:not(:has-text("Google")):not(:has-text("Apple")):not(:has-text("phone"))',
        'button:has-text("Continue"):not(:has-text("Google"))',
        'button:has-text("继续")',
        'button:has-text("Next")',
        'button:has-text("Log in")',
    ])
    if button:
        await button.click()
    else:
        await page.keyboard.press("Enter")


async def submit_chatgpt_identifier(page: Any, email: str, timeout_ms: int) -> None:
    locator = await first_visible_locator(page, [
        'input[type="email"]',
        'input[name="email"]',
        'input[autocomplete="email"]',
        'input[type="text"]',
    ])
    if not locator:
        login_link = await first_visible_locator(page, [
            'a[href*="login"]',
            'button:has-text("Log in")',
            'button:has-text("登录")',
        ])
        if login_link:
            await login_link.click()
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 10000))
            locator = await first_visible_locator(page, [
                'input[type="email"]',
                'input[name="email"]',
                'input[autocomplete="email"]',
                'input[type="text"]',
            ])
    if not locator:
        raise RuntimeError("未找到 ChatGPT 邮箱输入框。")
    await locator.fill(email, timeout=timeout_ms)
    await click_submit(page, locator)


async def submit_chatgpt_password(page: Any, password: str, timeout_ms: int) -> None:
    locator = await first_visible_locator(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
    ])
    if not locator:
        raise RuntimeError("未找到 ChatGPT 密码输入框。")
    await locator.fill(password, timeout=timeout_ms)
    await click_submit(page, locator)


async def submit_chatgpt_code(page: Any, code: str, timeout_ms: int) -> None:
    if not code:
        raise RuntimeError("ChatGPT 登录验证码为空。")
    locator = await first_visible_locator(page, [
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[aria-label*="code" i]',
    ])
    if not locator:
        raise RuntimeError("未找到 ChatGPT 验证码输入框。")
    await locator.fill(code, timeout=timeout_ms)
    await click_submit(page, locator)


async def poll_chatgpt_code(
    account: Dict[str, Any],
    requested_at_ms: float,
    settings: Settings,
    fetcher: Any,
) -> str:
    deadline = time.monotonic() + max(1, settings.chatgpt_code_poll_seconds)
    interval = max(1, settings.chatgpt_code_poll_interval_seconds)
    last_error = ""
    while True:
        try:
            fetch_result = await fetcher(account, DEFAULT_MAIL_PAGE_SIZE, settings)
            if fetch_result.next_refresh_token:
                account["refreshToken"] = fetch_result.next_refresh_token
            match = extract_chatgpt_code_from_messages(fetch_result.messages, requested_at_ms)
            if match:
                return match["code"]
        except Exception as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            detail = f"未在邮箱中收到新的 OpenAI / ChatGPT 登录验证码。{last_error}".strip()
            raise HTTPException(status_code=504, detail=detail)
        await asyncio.sleep(interval)


def serialize_chatgpt_job(job: ChatGPTLoginJob) -> Dict[str, Any]:
    return {
        "ok": True,
        "jobId": job.job_id,
        "status": job.status,
        "total": job.total,
        "completed": job.completed,
        "running": job.running,
        "pending": job.pending,
        "success": job.success,
        "failed": job.failed,
        "cancelled": job.cancelled,
        "results": job.results,
        "accounts": job.accounts,
        "cancelRequested": job.cancel_requested,
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
    }


def chatgpt_job_account(email: str) -> Dict[str, Any]:
    return {
        "email": email,
        "status": "pending",
        "stage": "pending",
        "stageLabel": "等待开始",
        "ok": False,
        "reason": "",
        "events": [{
            "stage": "pending",
            "message": "等待开始",
            "at": now_iso(),
        }],
    }


def add_chatgpt_job_event(item: Dict[str, Any], stage: str, message: str) -> None:
    item["stage"] = stage
    item["stageLabel"] = message
    item.setdefault("events", []).append({
        "stage": stage,
        "message": message,
        "at": now_iso(),
    })


def create_app(
    env: Optional[Dict[str, str]] = None,
    outlook_fetcher: Optional[Any] = None,
    chatgpt_login_runner: Optional[Any] = None,
) -> FastAPI:
    settings = load_settings(env)
    configure_logging(settings.log_level)
    runtime = RuntimeConfig(settings)
    public_limiter = RateLimiter(settings.public_rate_limit_requests, settings.public_rate_limit_window_seconds)
    admin_limiter = RateLimiter(settings.admin_rate_limit_requests, settings.admin_rate_limit_window_seconds)
    cache = TTLCache(settings.message_cache_ttl_seconds)
    semaphore = asyncio.Semaphore(max(1, settings.upstream_max_concurrency))
    chatgpt_jobs: Dict[str, ChatGPTLoginJob] = {}
    chatgpt_job_tasks: Dict[str, asyncio.Task] = {}
    chatgpt_runner = chatgpt_login_runner or default_chatgpt_login_runner

    app = FastAPI(title="Edu Mail Web", version=settings.service_version)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def require_admin(request: Request) -> None:
        cookie_value = request.cookies.get("edu_mail_admin", "")
        session_id = parse_session_cookie(cookie_value, settings.session_secret)
        if not session_id:
            raise HTTPException(status_code=401, detail="请先登录管理员入口。")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return render_template("index.html")

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page() -> str:
        return render_template("admin.html")

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        runtime.refresh_from_disk()
        return {
            "ok": True,
            "service": settings.service_name,
            "version": settings.service_version,
            "apiTokenConfigured": bool(runtime.api_token),
            "baseUrl": runtime.base_url,
        }

    @app.post("/api/messages/code")
    async def query_code(request_body: CodeQueryRequest, request: Request) -> JSONResponse:
        raw_identifier = str(request_body.email or "").strip()
        try:
            email = normalize_email(raw_identifier)
        except ValueError:
            alias_account = find_outlook_account_by_alias(runtime, raw_identifier)
            if not alias_account:
                raise HTTPException(status_code=400, detail="请输入完整邮箱，或已配置的 Outlook 邮箱别名。")
            email = alias_account["email"]
        public_limiter.check(f"ip:{client_ip(request)}")
        public_limiter.check(f"email:{email}")
        limit = min(max(int(request_body.limit or DEFAULT_MAIL_PAGE_SIZE), 1), 50)
        runtime.refresh_from_disk()
        outlook_account = find_outlook_account(runtime, email)
        if outlook_account:
            provider = "outlook"
        elif is_outlook_domain(email):
            raise HTTPException(status_code=404, detail="该 Outlook / Hotmail 邮箱尚未导入，请先联系管理员导入账号。")
        elif is_edu_domain(email):
            provider = "edu"
        else:
            raise HTTPException(status_code=400, detail="当前仅支持已导入的 Outlook / Hotmail 邮箱，或 .edu 域名邮箱。")
        token_source = outlook_account.get("refreshToken", "") if outlook_account else runtime.api_token
        cache_key = f"{provider}:{email}:{limit}:{runtime.base_url}:{hashlib.sha256(token_source.encode()).hexdigest()[:12]}"
        cached = cache.get(cache_key)
        if cached is not None:
            return JSONResponse({**cached, "cached": True})

        if outlook_account:
            fetcher = outlook_fetcher or fetch_outlook_account_messages
            fetch_result = await fetcher(outlook_account, limit, settings)
            if fetch_result.next_refresh_token:
                outlook_account["refreshToken"] = fetch_result.next_refresh_token
                outlook_account["updatedAt"] = now_iso()
                runtime.persist()
            messages = fetch_result.messages
            for message in messages:
                if not str(message.get("address") or "").strip():
                    message["address"] = email
        else:
            messages = await fetch_messages_from_upstream(runtime, settings, semaphore, email, limit)

        filtered = []
        for message in messages:
            address = str(message.get("address") or "").lower()
            recipients = [str(item).lower() for item in message.get("recipients") or []]
            if provider == "outlook" or not address or address == email or email in recipients:
                filtered.append(message)
        sorted_messages = sort_messages_newest_first(filtered)
        match = next(
            (
                {
                    "code": code,
                    "message": message,
                }
                for message in sorted_messages
                if (code := extract_verification_code(message))
            ),
            None,
        )
        result = {
            "ok": True,
            "provider": provider,
            "email": email,
            "found": bool(match),
            "code": match["code"] if match else "",
            "message": summarize_message(match["message"]) if match else None,
            "recentMessages": [summarize_message(message) for message in sorted_messages[:5]],
            "cached": False,
        }
        cache.set(cache_key, result)
        return JSONResponse(result)

    @app.post("/api/admin/login")
    async def admin_login(request_body: AdminLoginRequest, request: Request, response: Response) -> Dict[str, Any]:
        admin_limiter.check(f"admin-login:{client_ip(request)}")
        if not settings.admin_password:
            raise HTTPException(status_code=503, detail="服务器尚未配置 ADMIN_PASSWORD。")
        if not settings.session_secret:
            raise HTTPException(status_code=503, detail="服务器尚未配置 SESSION_SECRET。")
        if not secrets.compare_digest(request_body.password, settings.admin_password):
            raise HTTPException(status_code=401, detail="管理员密码错误。")
        session_id = secrets.token_urlsafe(24)
        response.set_cookie(
            "edu_mail_admin",
            build_session_cookie(session_id, settings.session_secret),
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="lax",
            max_age=86400,
        )
        return {"ok": True}

    @app.post("/api/admin/logout")
    async def admin_logout(request: Request, response: Response) -> Dict[str, Any]:
        response.delete_cookie("edu_mail_admin")
        return {"ok": True}

    @app.get("/api/admin/config")
    async def get_admin_config(request: Request) -> Dict[str, Any]:
        require_admin(request)
        return {"ok": True, **public_config(runtime)}

    @app.post("/api/admin/config")
    async def set_admin_config(request_body: AdminConfigRequest, request: Request) -> Dict[str, Any]:
        require_admin(request)
        runtime.refresh_from_disk()
        if request_body.baseUrl is not None:
            runtime.base_url = normalize_base_url(request_body.baseUrl)
        if request_body.apiToken is not None and request_body.apiToken.strip():
            runtime.api_token = normalize_api_token(request_body.apiToken)
        runtime.persist()
        return {"ok": True, **public_config(runtime)}

    @app.get("/api/admin/outlook/accounts")
    async def list_outlook_accounts(
        request: Request,
        q: Optional[str] = "",
        used: Optional[str] = "",
        page: int = 1,
        pageSize: int = 10,
    ) -> Dict[str, Any]:
        require_admin(request)
        runtime.refresh_from_disk()
        accounts = filter_outlook_accounts(runtime.outlook_accounts, q, used)
        page_accounts, safe_page, safe_page_size, total_pages = paginate_items(accounts, page, pageSize)
        return {
            "ok": True,
            "total": len(runtime.outlook_accounts),
            "filtered": len(accounts),
            "query": str(q or "").strip(),
            "usedFilter": str(used or "").strip(),
            "page": safe_page,
            "pageSize": safe_page_size,
            "totalPages": total_pages,
            "accounts": [public_outlook_account(account) for account in page_accounts],
        }

    @app.post("/api/admin/outlook/import")
    async def import_outlook_accounts(request_body: OutlookImportRequest, request: Request) -> Dict[str, Any]:
        require_admin(request)
        parsed = parse_outlook_import_text(request_body.text)
        stats = upsert_outlook_accounts(runtime, parsed["accounts"]) if parsed["accounts"] else {
            "imported": 0,
            "created": 0,
            "updated": 0,
        }
        return {
            "ok": True,
            "total": parsed["total"],
            "imported": stats["imported"],
            "created": stats["created"],
            "updated": stats["updated"],
            "failed": len(parsed["errors"]),
            "errors": parsed["errors"],
            "accounts": [public_outlook_account(account) for account in runtime.outlook_accounts],
        }

    @app.post("/api/admin/outlook/export")
    async def export_outlook_accounts(request_body: OutlookExportRequest, request: Request) -> JSONResponse:
        require_admin(request)
        requested_emails = []
        for email in request_body.emails:
            try:
                requested_emails.append(normalize_email(email))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not requested_emails:
            raise HTTPException(status_code=400, detail="请选择要导出的邮箱。")
        runtime.refresh_from_disk()
        by_email = {account["email"]: account for account in runtime.outlook_accounts}
        lines = [outlook_import_line(by_email[email]) for email in requested_emails if email in by_email]
        if not lines:
            raise HTTPException(status_code=404, detail="没有找到可导出的邮箱。")
        return JSONResponse(
            {"ok": True, "count": len(lines), "lines": lines, "text": "\n".join(lines)},
            headers={"Content-Disposition": 'attachment; filename="outlook-accounts.txt"'},
        )

    @app.delete("/api/admin/outlook/accounts/{email:path}")
    async def delete_outlook_account(email: str, request: Request) -> Dict[str, Any]:
        require_admin(request)
        normalized_email = normalize_email(email)
        runtime.refresh_from_disk()
        before_count = len(runtime.outlook_accounts)
        runtime.outlook_accounts = [account for account in runtime.outlook_accounts if account.get("email") != normalized_email]
        if len(runtime.outlook_accounts) == before_count:
            raise HTTPException(status_code=404, detail="未找到 Outlook 账号。")
        runtime.persist(merge_disk_accounts=False)
        return {"ok": True}

    @app.post("/api/admin/outlook/accounts/{email:path}/used")
    async def set_outlook_account_used(email: str, request_body: OutlookUsedRequest, request: Request) -> Dict[str, Any]:
        require_admin(request)
        account = find_outlook_account(runtime, email)
        if not account:
            raise HTTPException(status_code=404, detail="未找到 Outlook 账号。")
        updated_account = dict(account)
        updated_account["used"] = request_body.used
        updated_account["updatedAt"] = now_iso()
        update_outlook_account(runtime, updated_account)
        runtime.refresh_from_disk()
        latest = find_outlook_account(runtime, email) or updated_account
        return {"ok": True, "account": public_outlook_account(latest)}

    @app.post("/api/admin/outlook/accounts/{email:path}/alias")
    async def set_outlook_account_alias(email: str, request_body: OutlookAliasRequest, request: Request) -> Dict[str, Any]:
        require_admin(request)
        account = find_outlook_account(runtime, email)
        if not account:
            raise HTTPException(status_code=404, detail="未找到 Outlook 账号。")
        alias = normalize_alias(request_body.alias)
        if alias:
            duplicate = find_outlook_account_by_alias(runtime, alias)
            if duplicate and duplicate.get("email") != account.get("email"):
                raise HTTPException(status_code=409, detail="别名已存在，请换一个。")
        updated_account = dict(account)
        updated_account["alias"] = alias
        updated_account["updatedAt"] = now_iso()
        update_outlook_account(runtime, updated_account)
        runtime.refresh_from_disk()
        latest = find_outlook_account(runtime, email) or updated_account
        return {"ok": True, "account": public_outlook_account(latest)}

    @app.post("/api/admin/outlook/delete")
    async def delete_outlook_accounts(request_body: OutlookDeleteRequest, request: Request) -> Dict[str, Any]:
        require_admin(request)
        requested_emails = []
        for email in request_body.emails:
            try:
                requested_emails.append(normalize_email(email))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not requested_emails:
            raise HTTPException(status_code=400, detail="请选择要删除的邮箱。")
        targets = set(requested_emails)
        runtime.refresh_from_disk()
        before_count = len(runtime.outlook_accounts)
        runtime.outlook_accounts = [account for account in runtime.outlook_accounts if account.get("email") not in targets]
        deleted_count = before_count - len(runtime.outlook_accounts)
        if deleted_count <= 0:
            raise HTTPException(status_code=404, detail="没有找到可删除的邮箱。")
        runtime.persist(merge_disk_accounts=False)
        return {"ok": True, "deleted": deleted_count}

    @app.post("/api/admin/outlook/code-tests")
    async def test_outlook_code_accounts(request_body: OutlookCodeTestRequest, request: Request) -> Dict[str, Any]:
        require_admin(request)
        requested_emails = []
        for email in request_body.emails:
            try:
                requested_emails.append(normalize_email(email))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not requested_emails:
            raise HTTPException(status_code=400, detail="请选择要测试收码的邮箱。")
        runtime.refresh_from_disk()
        by_email = {account["email"]: account for account in runtime.outlook_accounts}
        accounts = [by_email[email] for email in requested_emails if email in by_email]
        if not accounts:
            raise HTTPException(status_code=404, detail="没有找到可测试的 Outlook / Hotmail 账号。")
        fetcher = outlook_fetcher or fetch_outlook_account_messages
        interval_seconds = min(max(float(request_body.intervalSeconds or 0), 0.0), 10.0)
        results = []
        for index, account in enumerate(accounts):
            if index and interval_seconds:
                await asyncio.sleep(interval_seconds)
            try:
                result = await test_single_outlook_account(runtime, settings, account, fetcher)
                recent_messages = result.get("recentMessages") or []
                results.append({
                    "email": account["email"],
                    "ok": True,
                    "status": "ok",
                    "error": "",
                    "recentCount": len(recent_messages),
                    "recentMessages": recent_messages,
                    "account": result.get("account"),
                })
            except HTTPException as exc:
                latest = find_outlook_account(runtime, account["email"]) or account
                results.append({
                    "email": account["email"],
                    "ok": False,
                    "status": "error",
                    "error": str(exc.detail),
                    "recentCount": 0,
                    "recentMessages": [],
                    "account": public_outlook_account(latest),
                })
        success = sum(1 for item in results if item.get("ok"))
        failed = len(results) - success
        return {
            "ok": True,
            "total": len(results),
            "success": success,
            "failed": failed,
            "results": results,
        }

    @app.post("/api/admin/chatgpt/login-tests")
    async def create_chatgpt_login_test(request_body: ChatGPTLoginTestRequest, request: Request) -> Dict[str, Any]:
        require_admin(request)
        runtime.refresh_from_disk()
        if request_body.all:
            accounts = list(runtime.outlook_accounts)
        else:
            requested_emails = []
            for email in request_body.emails:
                try:
                    requested_emails.append(normalize_email(email))
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            requested = set(requested_emails)
            accounts = [account for account in runtime.outlook_accounts if account.get("email") in requested]
        if not accounts:
            raise HTTPException(status_code=404, detail="没有找到可检测的 Outlook / Hotmail 账号。")

        job = ChatGPTLoginJob(
            job_id=uuid.uuid4().hex,
            status="running",
            total=len(accounts),
            pending=len(accounts),
            accounts=[chatgpt_job_account(account["email"]) for account in accounts],
        )
        chatgpt_jobs[job.job_id] = job
        task = asyncio.create_task(run_chatgpt_login_job(job, accounts))
        chatgpt_job_tasks[job.job_id] = task
        app.state.chatgpt_last_task = task
        return {"ok": True, "jobId": job.job_id, "total": job.total}

    @app.get("/api/admin/chatgpt/login-tests/{job_id}")
    async def get_chatgpt_login_test(job_id: str, request: Request) -> Dict[str, Any]:
        require_admin(request)
        job = chatgpt_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="未找到 ChatGPT 登录检测任务。")
        task = chatgpt_job_tasks.get(job.job_id)
        if job.status == "running" and task and not task.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
        return serialize_chatgpt_job(job)

    @app.delete("/api/admin/chatgpt/login-tests/{job_id}")
    async def cancel_chatgpt_login_test(job_id: str, request: Request) -> Dict[str, Any]:
        require_admin(request)
        job = chatgpt_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="未找到 ChatGPT 登录检测任务。")
        if job.status in {"completed", "failed", "cancelled"}:
            return serialize_chatgpt_job(job)
        job.cancel_requested = True
        job.status = "cancelled"
        task = chatgpt_job_tasks.get(job.job_id)
        if task and not task.done():
            task.cancel()
        for item in job.accounts:
            if item.get("status") in {"pending", "running"}:
                item["status"] = "cancelled"
                item["ok"] = False
                item["reason"] = "任务已中断。"
                add_chatgpt_job_event(item, "cancelled", "任务已中断")
        job.completed = sum(1 for item in job.accounts if item.get("status") in {"ok", "blocked", "auth_error", "challenge", "mail_error", "browser_error", "timeout"})
        job.running = 0
        job.pending = 0
        job.success = sum(1 for item in job.accounts if is_chatgpt_success_status(item.get("status", "")))
        job.failed = sum(1 for item in job.accounts if item.get("status") not in {"ok", "blocked", "pending", "running", "cancelled"})
        job.cancelled = sum(1 for item in job.accounts if item.get("status") == "cancelled")
        job.updated_at = now_iso()
        return serialize_chatgpt_job(job)

    async def run_chatgpt_login_job(job: ChatGPTLoginJob, accounts: List[Dict[str, Any]]) -> None:
        limiter = asyncio.Semaphore(max(1, settings.chatgpt_login_concurrency))
        fetcher = outlook_fetcher or fetch_outlook_account_messages
        items_by_email = {item["email"]: item for item in job.accounts}

        def refresh_counts() -> None:
            job.pending = sum(1 for item in job.accounts if item.get("status") == "pending")
            job.running = sum(1 for item in job.accounts if item.get("status") == "running")
            job.completed = sum(1 for item in job.accounts if item.get("status") in {"ok", "blocked", "auth_error", "challenge", "mail_error", "browser_error", "timeout"})
            job.success = sum(1 for item in job.accounts if is_chatgpt_success_status(item.get("status", "")))
            job.failed = sum(1 for item in job.accounts if item.get("status") in {"auth_error", "challenge", "mail_error", "browser_error", "timeout"})
            job.cancelled = sum(1 for item in job.accounts if item.get("status") == "cancelled")
            job.updated_at = now_iso()

        async def run_one(account: Dict[str, Any]) -> None:
            async with limiter:
                item = items_by_email[account["email"]]
                if job.cancel_requested:
                    item["status"] = "cancelled"
                    item["reason"] = "任务已中断。"
                    add_chatgpt_job_event(item, "cancelled", "任务已中断")
                    refresh_counts()
                    return
                item["status"] = "running"
                add_chatgpt_job_event(item, "start", "开始检测")
                refresh_counts()
                result: ChatGPTLoginResult
                try:
                    async def code_fetcher(working_account: Dict[str, Any], requested_at_ms: float) -> str:
                        return await poll_chatgpt_code(working_account, requested_at_ms, settings, fetcher)

                    def progress(stage: str, message: str) -> None:
                        add_chatgpt_job_event(item, stage, message)
                        refresh_counts()

                    result = await chatgpt_runner(dict(account), settings, code_fetcher, progress=progress)
                    if not isinstance(result, ChatGPTLoginResult):
                        raise RuntimeError("ChatGPT 登录执行器返回了无效结果。")
                except asyncio.CancelledError:
                    item["status"] = "cancelled"
                    item["reason"] = "任务已中断。"
                    add_chatgpt_job_event(item, "cancelled", "任务已中断")
                    refresh_counts()
                    raise
                except HTTPException as exc:
                    result = ChatGPTLoginResult(status="mail_error", reason=str(exc.detail), stage="mail")
                except Exception as exc:
                    result = ChatGPTLoginResult(status="browser_error", reason=f"runner: {str(exc)}", stage="runner")

                latest = persist_chatgpt_login_result(runtime, account, result)
                item.update({
                    "email": account["email"],
                    "ok": is_chatgpt_success_status(result.status),
                    "status": result.status,
                    "reason": result.reason,
                    "stage": result.stage,
                    "planStatus": result.plan_status,
                    "planReason": result.plan_reason,
                    "account": public_outlook_account(latest),
                })
                add_chatgpt_job_event(item, result.stage or result.status, chatgpt_status_message(result.status, result.reason))
                job.results.append(item)
                refresh_counts()

        try:
            await asyncio.gather(*(run_one(account) for account in accounts))
            if job.status != "cancelled":
                job.status = "completed"
                refresh_counts()
        except asyncio.CancelledError:
            job.status = "cancelled"
            for item in job.accounts:
                if item.get("status") in {"pending", "running"}:
                    item["status"] = "cancelled"
                    item["reason"] = "任务已中断。"
                    add_chatgpt_job_event(item, "cancelled", "任务已中断")
            refresh_counts()
        except Exception as exc:
            job.status = "failed"
            job.results.append({
                "email": "",
                "ok": False,
                "status": "browser_error",
                "reason": str(exc),
                "stage": "job",
            })
            refresh_counts()
        finally:
            job.updated_at = now_iso()

    return app


def summarize_message(message: Dict[str, Any]) -> Dict[str, Any]:
    recipients = [str(item).strip() for item in message.get("recipients") or [] if str(item).strip()]
    return {
        "id": message.get("id", ""),
        "subject": message.get("subject", ""),
        "from": message.get("from", {}).get("emailAddress", {}).get("address", ""),
        "recipient": first_non_empty([message.get("address"), ", ".join(recipients)]),
        "receivedDateTime": message.get("receivedDateTime", ""),
        "preview": str(message.get("bodyPreview", ""))[:240],
    }


app = create_app()
