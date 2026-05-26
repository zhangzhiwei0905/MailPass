import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException


os.environ.setdefault("ADMIN_PASSWORD", "secret-admin")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from app import (  # noqa: E402
    ChatGPTLoginResult,
    FetchResult,
    click_submit,
    classify_chatgpt_page,
    extract_chatgpt_code_from_messages,
    extract_verification_code,
    public_outlook_account,
    normalize_outlook_account,
    find_plan_type,
    wait_for_chatgpt_state,
    mask_token,
    normalize_api_token,
    normalize_email,
    normalize_messages,
    parse_outlook_import_text,
    mask_secret,
    create_app,
    is_chatgpt_success_status,
    summarize_message,
)


class EduMailHelpersTest(unittest.TestCase):
    def test_normalize_api_token_accepts_bearer_header(self):
        self.assertEqual(
            normalize_api_token("Authorization: Bearer abc.def-123"),
            "abc.def-123",
        )
        self.assertEqual(normalize_api_token("Bearer raw-token"), "raw-token")

    def test_mask_token_keeps_secret_hidden(self):
        self.assertEqual(mask_token("abcdefghijklmnopqrstuvwxyz"), "abcd...wxyz")
        self.assertEqual(mask_token("short"), "configured")
        self.assertEqual(mask_token(""), "")

    def test_normalize_email_rejects_invalid_addresses(self):
        self.assertEqual(normalize_email(" User@Example.EDU "), "user@example.edu")
        with self.assertRaises(ValueError):
            normalize_email("not-an-email")

    def test_normalize_messages_supports_common_payload_shapes(self):
        payload = {
            "data": {
                "messages": [
                    {
                        "id": "m1",
                        "alias": "user@example.edu",
                        "subject": "Your code",
                        "from": "noreply@example.com",
                        "body": "Use 123456 to sign in",
                        "created_at": 1710000000,
                    }
                ]
            }
        }

        messages = normalize_messages(payload)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["address"], "user@example.edu")
        self.assertEqual(messages[0]["subject"], "Your code")
        self.assertIn("123456", messages[0]["bodyPreview"])
        self.assertEqual(messages[0]["receivedDateTime"], "2024-03-09T16:00:00+00:00")

    def test_extract_verification_code_prefers_contextual_code(self):
        message = {
            "subject": "Verify your account",
            "bodyPreview": "Your verification code is 987654. Ref id 123123.",
        }

        self.assertEqual(extract_verification_code(message), "987654")

    def test_parse_outlook_import_text_reports_invalid_lines(self):
        parsed = parse_outlook_import_text(
            "\n".join([
                "a@outlook.com----pass-a----client-a----refresh-a",
                "bad-line",
                "b@hotmail.com----pass-b----client-b----refresh-b",
            ])
        )

        self.assertEqual(len(parsed["accounts"]), 2)
        self.assertEqual(parsed["accounts"][0]["email"], "a@outlook.com")
        self.assertEqual(parsed["accounts"][1]["clientId"], "client-b")
        self.assertEqual(parsed["errors"], [{"line": 2, "reason": "格式应为：邮箱号----密码----客户端 ID----刷新令牌"}])

    def test_summarize_message_exposes_recipient_for_alias_queries(self):
        summary = summarize_message({
            "id": "m1",
            "address": "alias-user@outlook.com",
            "subject": "Login code",
            "from": {"emailAddress": {"address": "sender@example.com"}},
            "bodyPreview": "Your verification code is 654321.",
            "receivedDateTime": "2026-05-22T00:00:00+00:00",
            "recipients": ["alias-user@outlook.com", "copy@example.com"],
        })

        self.assertEqual(summary["recipient"], "alias-user@outlook.com")

    def test_mask_secret_hides_imported_credentials(self):
        self.assertEqual(mask_secret("abcdefghijklmnop"), "abcd...mnop")
        self.assertEqual(mask_secret("short"), "configured")

    def test_wait_for_chatgpt_state_skips_transient_email_state(self):
        class FakePage:
            def __init__(self):
                self.url = "https://chatgpt.com/auth/login"
                self.calls = 0

            async def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, _selector):
                return self

            @property
            def first(self):
                return self

            async def is_visible(self, timeout=1000):
                return False

        async def fake_classifier(page):
            page.calls += 1
            return "email" if page.calls < 3 else "password"

        page = FakePage()
        state = __import__("asyncio").run(wait_for_chatgpt_state(
            page,
            {"password", "verification", "challenge", "blocked", "auth_error", "ok"},
            timeout_ms=500,
            classifier=fake_classifier,
        ))

        self.assertEqual(state, "password")
        self.assertEqual(page.calls, 3)

    def test_click_submit_uses_input_form_before_global_social_buttons(self):
        class FakeLocator:
            def __init__(self):
                self.script = ""
                self.enter_pressed = False

            async def evaluate(self, script):
                self.script = script
                return True

            async def press(self, _key, timeout=5000):
                self.enter_pressed = True

        class FakePage:
            def __init__(self):
                self.keyboard_pressed = False

            def locator(self, _selector):
                raise AssertionError("global submit lookup should not run after input-form submit")

            @property
            def keyboard(self):
                return self

            async def press(self, _key):
                self.keyboard_pressed = True

        locator = FakeLocator()
        page = FakePage()
        __import__("asyncio").run(click_submit(page, locator))

        self.assertIn("google|apple|phone|try it first", locator.script)
        self.assertFalse(locator.enter_pressed)
        self.assertFalse(page.keyboard_pressed)

    def test_chatgpt_page_classifier_prefers_code_input_over_generic_text_input(self):
        class FakePage:
            url = "https://auth.openai.com/email-verification"

            def locator(self, selector):
                return FakeLocator(selector)

        class FakeLocator:
            def __init__(self, selector):
                self.selector = selector

            @property
            def first(self):
                return self

            async def is_visible(self, timeout=1000):
                return "inputmode" in self.selector

            async def inner_text(self, timeout=3000):
                return "Check your inbox Code Continue"

        state = __import__("asyncio").run(classify_chatgpt_page(FakePage()))

        self.assertEqual(state, "verification")

    def test_public_outlook_account_includes_chatgpt_plan_status(self):
        account = normalize_outlook_account({
            "email": "user@outlook.com",
            "password": "pass",
            "clientId": "client",
            "refreshToken": "refresh",
            "chatgptLastPlanStatus": "plus",
            "chatgptLastPlanReason": "Plus plan detected",
        })

        public = public_outlook_account(account)

        self.assertEqual(public["chatgptLastPlanStatus"], "plus")
        self.assertEqual(public["chatgptLastPlanReason"], "Plus plan detected")

    def test_blocked_chatgpt_status_counts_as_completed_verification(self):
        self.assertTrue(is_chatgpt_success_status("blocked"))

    def test_find_plan_type_reads_nested_session_payload(self):
        value, path = find_plan_type({
            "user": {
                "accounts": [{
                    "subscription": {
                        "planType": "plus",
                    },
                }],
            },
        })

        self.assertEqual(value, "plus")
        self.assertIn("planType", path)


class EduMailApiTest(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            self.skipTest(f"fastapi is not installed: {exc}")

        self.TestClient = TestClient

    def test_public_query_requires_configured_token(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "EDU_MAIL_API_TOKEN": "",
            "PUBLIC_RATE_LIMIT_REQUESTS": "10",
        })
        client = self.TestClient(app)

        response = client.post("/api/messages/code", json={"email": "user@example.edu"})

        self.assertEqual(response.status_code, 503)
        self.assertIn("尚未配置", response.json()["detail"])

    def test_admin_config_requires_login_and_masks_saved_token(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)

        blocked = client.get("/api/admin/config")
        self.assertEqual(blocked.status_code, 401)

        login = client.post("/api/admin/login", json={"password": "secret-admin"})
        self.assertEqual(login.status_code, 200)

        saved = client.post(
            "/api/admin/config",
            json={
                "baseUrl": "https://inbox.beastxa.com",
                "apiToken": "Bearer abcdefghijklmnopqrstuvwxyz",
            },
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["apiTokenMasked"], "abcd...wxyz")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", saved.text)

    def test_public_rate_limit_applies_to_repeated_requests(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "EDU_MAIL_API_TOKEN": "",
            "PUBLIC_RATE_LIMIT_REQUESTS": "1",
            "PUBLIC_RATE_LIMIT_WINDOW_SECONDS": "60",
        })
        client = self.TestClient(app)

        first = client.post("/api/messages/code", json={"email": "user@example.edu"})
        second = client.post("/api/messages/code", json={"email": "user@example.edu"})

        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 429)

    def test_outlook_import_requires_login_and_returns_masked_accounts(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)
        payload = {
            "text": "user@outlook.com----mail-password----client-123456----refresh-abcdef",
        }

        blocked = client.post("/api/admin/outlook/import", json=payload)
        self.assertEqual(blocked.status_code, 401)

        self.assertEqual(client.post("/api/admin/login", json={"password": "secret-admin"}).status_code, 200)
        imported = client.post("/api/admin/outlook/import", json=payload)
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["imported"], 1)
        self.assertEqual(imported.json()["created"], 1)
        self.assertEqual(imported.json()["updated"], 0)
        self.assertEqual(imported.json()["failed"], 0)
        self.assertEqual(imported.json()["total"], 1)

        accounts = client.get("/api/admin/outlook/accounts")
        self.assertEqual(accounts.status_code, 200)
        body = accounts.json()
        self.assertEqual(body["accounts"][0]["email"], "user@outlook.com")
        self.assertEqual(body["accounts"][0]["passwordMasked"], "mail...word")
        self.assertEqual(body["accounts"][0]["clientIdMasked"], "clie...3456")
        self.assertEqual(body["accounts"][0]["refreshTokenMasked"], "refr...cdef")
        self.assertNotIn("mail-password", accounts.text)
        self.assertNotIn("refresh-abcdef", accounts.text)

    def test_outlook_import_reports_created_updated_and_failed_counts(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={"text": "user@outlook.com----old-pass----client-old----refresh-old"},
        )

        imported = client.post(
            "/api/admin/outlook/import",
            json={
                "text": "\n".join([
                    "user@outlook.com----new-pass----client-new----refresh-new",
                    "new@hotmail.com----pass----client-id----refresh-token",
                    "bad-line",
                ])
            },
        )

        self.assertEqual(imported.status_code, 200)
        body = imported.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["imported"], 2)
        self.assertEqual(body["created"], 1)
        self.assertEqual(body["updated"], 1)
        self.assertEqual(body["failed"], 1)
        self.assertEqual([account["email"] for account in body["accounts"]], ["new@hotmail.com", "user@outlook.com"])

    def test_outlook_accounts_support_fuzzy_email_search(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={
                "text": "\n".join([
                    "alice.demo@outlook.com----pass----client-a----refresh-a",
                    "bob-test@hotmail.com----pass----client-b----refresh-b",
                    "carol@live.com----pass----client-c----refresh-c",
                ])
            },
        )

        response = client.get("/api/admin/outlook/accounts?q=TEST")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["filtered"], 1)
        self.assertEqual([account["email"] for account in body["accounts"]], ["bob-test@hotmail.com"])

    def test_outlook_accounts_support_alias_and_search_by_alias(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={
                "text": "\n".join([
                    "alpha@outlook.com----pass-a----client-a----refresh-a",
                    "beta@outlook.com----pass-b----client-b----refresh-b",
                ])
            },
        )

        changed = client.post("/api/admin/outlook/accounts/beta@outlook.com/alias", json={"alias": "OpenAI 注册池"})

        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["account"]["alias"], "OpenAI 注册池")
        client.post("/api/admin/outlook/import", json={"text": "beta@outlook.com----new-pass----new-client----new-refresh"})
        response = client.get("/api/admin/outlook/accounts?q=注册")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["filtered"], 1)
        self.assertEqual(body["accounts"][0]["email"], "beta@outlook.com")
        self.assertEqual(body["accounts"][0]["alias"], "OpenAI 注册池")

    def test_outlook_alias_must_be_unique(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={
                "text": "\n".join([
                    "alpha@outlook.com----pass-a----client-a----refresh-a",
                    "beta@outlook.com----pass-b----client-b----refresh-b",
                ])
            },
        )
        self.assertEqual(
            client.post("/api/admin/outlook/accounts/alpha@outlook.com/alias", json={"alias": "openai"}).status_code,
            200,
        )

        duplicate = client.post("/api/admin/outlook/accounts/beta@outlook.com/alias", json={"alias": " OpenAI "})

        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("别名已存在", duplicate.json()["detail"])

    def test_public_query_can_use_outlook_alias(self):
        async def fake_fetcher(account, limit, settings):
            return FetchResult(
                messages=[
                    {
                        "id": "m1",
                        "address": account["email"],
                        "subject": "Login code",
                        "from": {"emailAddress": {"address": "sender@example.com"}},
                        "bodyPreview": "Your verification code is 654321.",
                        "raw": "",
                        "receivedDateTime": "2026-05-22T00:00:00+00:00",
                    }
                ],
                next_refresh_token="",
                provider="outlook",
            )

        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        }, outlook_fetcher=fake_fetcher)
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={"text": "alias-user@outlook.com----pass----client-id----refresh-token"},
        )
        client.post("/api/admin/outlook/accounts/alias-user@outlook.com/alias", json={"alias": "openai-login"})

        response = client.post("/api/messages/code", json={"email": "openai-login"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "alias-user@outlook.com")
        self.assertEqual(response.json()["code"], "654321")

    def test_outlook_accounts_support_pagination(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={
                "text": "\n".join(
                    f"user-{index}@outlook.com----pass----client-{index}----refresh-{index}"
                    for index in range(1, 6)
                )
            },
        )

        response = client.get("/api/admin/outlook/accounts?page=2&pageSize=2")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["filtered"], 5)
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["pageSize"], 2)
        self.assertEqual(body["totalPages"], 3)
        self.assertEqual([account["email"] for account in body["accounts"]], ["user-3@outlook.com", "user-4@outlook.com"])

    def test_outlook_accounts_support_used_marker_and_filter(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={
                "text": "\n".join([
                    "alpha@outlook.com----pass-a----client-a----refresh-a",
                    "beta@outlook.com----pass-b----client-b----refresh-b",
                ])
            },
        )

        imported = client.get("/api/admin/outlook/accounts")
        self.assertEqual([account["used"] for account in imported.json()["accounts"]], [False, False])

        changed = client.post("/api/admin/outlook/accounts/alpha@outlook.com/used", json={"used": True})

        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["account"]["used"])
        used = client.get("/api/admin/outlook/accounts?used=used")
        unused = client.get("/api/admin/outlook/accounts?used=unused")
        self.assertEqual([account["email"] for account in used.json()["accounts"]], ["alpha@outlook.com"])
        self.assertEqual([account["email"] for account in unused.json()["accounts"]], ["beta@outlook.com"])

    def test_outlook_export_requires_login_and_returns_full_import_lines(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={
                "text": "\n".join([
                    "alpha@outlook.com----pass-a----client-a----refresh-a",
                    "beta@outlook.com----pass-b----client-b----refresh-b",
                ])
            },
        )
        blocked = self.TestClient(app).post("/api/admin/outlook/export", json={"emails": ["alpha@outlook.com"]})
        self.assertEqual(blocked.status_code, 401)

        response = client.post("/api/admin/outlook/export", json={"emails": ["beta@outlook.com"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["lines"], ["beta@outlook.com----pass-b----client-b----refresh-b"])
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))

    def test_outlook_bulk_delete_removes_selected_accounts(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={
                "text": "\n".join([
                    "alpha@outlook.com----pass-a----client-a----refresh-a",
                    "beta@outlook.com----pass-b----client-b----refresh-b",
                    "gamma@outlook.com----pass-c----client-c----refresh-c",
                ])
            },
        )

        response = client.post(
            "/api/admin/outlook/delete",
            json={"emails": ["alpha@outlook.com", "gamma@outlook.com"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 2)
        accounts = client.get("/api/admin/outlook/accounts").json()["accounts"]
        self.assertEqual([account["email"] for account in accounts], ["beta@outlook.com"])

    def test_outlook_accounts_are_read_from_shared_config_file(self):
        data_dir = tempfile.mkdtemp(prefix="edu-mail-web-shared-test-")
        env = {
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": data_dir,
        }
        first_client = self.TestClient(create_app(env))
        second_client = self.TestClient(create_app(env))

        first_client.post("/api/admin/login", json={"password": "secret-admin"})
        second_client.post("/api/admin/login", json={"password": "secret-admin"})
        imported = first_client.post(
            "/api/admin/outlook/import",
            json={"text": "shared@outlook.com----pass----client-id----refresh-token"},
        )
        self.assertEqual(imported.status_code, 200)

        accounts = second_client.get("/api/admin/outlook/accounts")

        self.assertEqual(accounts.status_code, 200)
        self.assertEqual([account["email"] for account in accounts.json()["accounts"]], ["shared@outlook.com"])

    def test_stale_admin_config_update_preserves_outlook_accounts(self):
        data_dir = tempfile.mkdtemp(prefix="edu-mail-web-shared-test-")
        env = {
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": data_dir,
        }
        first_client = self.TestClient(create_app(env))
        second_client = self.TestClient(create_app(env))

        first_client.post("/api/admin/login", json={"password": "secret-admin"})
        second_client.post("/api/admin/login", json={"password": "secret-admin"})
        first_client.post(
            "/api/admin/outlook/import",
            json={"text": "shared@outlook.com----pass----client-id----refresh-token"},
        )
        saved = second_client.post(
            "/api/admin/config",
            json={"baseUrl": "https://inbox.beastxa.com", "apiToken": "Bearer changed-token"},
        )
        self.assertEqual(saved.status_code, 200)

        accounts = first_client.get("/api/admin/outlook/accounts")

        self.assertEqual([account["email"] for account in accounts.json()["accounts"]], ["shared@outlook.com"])

    def test_stale_public_query_refresh_update_preserves_newer_imports(self):
        async def fake_fetcher(account, limit, settings):
            return FetchResult(messages=[], next_refresh_token="new-refresh-token", provider="outlook")

        data_dir = tempfile.mkdtemp(prefix="edu-mail-web-shared-test-")
        env = {
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": data_dir,
        }
        stale_client = self.TestClient(create_app(env, outlook_fetcher=fake_fetcher))
        fresh_client = self.TestClient(create_app(env, outlook_fetcher=fake_fetcher))
        stale_client.post("/api/admin/login", json={"password": "secret-admin"})
        fresh_client.post("/api/admin/login", json={"password": "secret-admin"})
        stale_client.post(
            "/api/admin/outlook/import",
            json={"text": "a@outlook.com----pass----client-id----old-refresh-token"},
        )
        fresh_client.post(
            "/api/admin/outlook/import",
            json={"text": "b@outlook.com----pass----client-id----refresh-token"},
        )

        response = stale_client.post("/api/messages/code", json={"email": "a@outlook.com"})

        self.assertEqual(response.status_code, 200)
        accounts = fresh_client.get("/api/admin/outlook/accounts").json()["accounts"]
        self.assertEqual([account["email"] for account in accounts], ["a@outlook.com", "b@outlook.com"])

    def test_public_query_routes_outlook_account_to_outlook_provider(self):
        async def fake_fetcher(account, limit, settings):
            return FetchResult(
                messages=[
                    {
                        "id": "m1",
                        "address": "user@outlook.com",
                        "subject": "Login code",
                        "from": {"emailAddress": {"address": "sender@example.com"}},
                        "bodyPreview": "Your verification code is 112233.",
                        "raw": "",
                        "receivedDateTime": "2026-05-22T00:00:00+00:00",
                    }
                ],
                next_refresh_token="next-refresh-token",
                provider="outlook",
            )

        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        }, outlook_fetcher=fake_fetcher)
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={"text": "user@outlook.com----pass----client-id----old-refresh-token"},
        )

        response = client.post("/api/messages/code", json={"email": "user@outlook.com"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "outlook")
        self.assertEqual(response.json()["code"], "112233")
        accounts = client.get("/api/admin/outlook/accounts").json()["accounts"]
        self.assertEqual(accounts[0]["refreshTokenMasked"], "next...oken")

    def test_public_query_uses_outlook_account_email_as_recipient_fallback(self):
        async def fake_fetcher(account, limit, settings):
            return FetchResult(
                messages=[
                    {
                        "id": "m1",
                        "subject": "Login code",
                        "from": {"emailAddress": {"address": "sender@example.com"}},
                        "bodyPreview": "Your verification code is 112233.",
                        "raw": "",
                        "receivedDateTime": "2026-05-22T00:00:00+00:00",
                    }
                ],
                next_refresh_token="",
                provider="outlook",
            )

        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        }, outlook_fetcher=fake_fetcher)
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={"text": "user@outlook.com----pass----client-id----refresh-token"},
        )

        response = client.post("/api/messages/code", json={"email": "user@outlook.com"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"]["recipient"], "user@outlook.com")
        self.assertEqual(response.json()["recentMessages"][0]["recipient"], "user@outlook.com")

    def test_admin_outlook_test_endpoints_are_removed(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={"text": "user@outlook.com----pass----client-id----refresh-token"},
        )

        single = client.post("/api/admin/outlook/accounts/user%40outlook.com/test")
        bulk = client.post("/api/admin/outlook/test-all")

        self.assertIn(single.status_code, {404, 405})
        self.assertEqual(bulk.status_code, 404)

    def test_admin_can_batch_test_selected_outlook_code_statuses(self):
        calls = []

        async def fake_fetcher(account, limit, settings):
            calls.append(account["email"])
            if account["email"].startswith("bad"):
                raise HTTPException(status_code=502, detail="refresh token expired")
            return FetchResult(
                messages=[{
                    "id": f"msg-{account['email']}",
                    "subject": "Login code",
                    "from": {"emailAddress": {"address": "sender@example.com"}},
                    "bodyPreview": "Your verification code is 112233.",
                    "receivedDateTime": "2026-05-26T00:01:00+00:00",
                }],
                next_refresh_token=f"next-{account['email']}",
                provider="outlook",
            )

        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        }, outlook_fetcher=fake_fetcher)
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={"text": "\n".join([
                "good@outlook.com----pass----client-good----refresh-good",
                "bad@outlook.com----pass----client-bad----refresh-bad",
            ])},
        )

        response = client.post("/api/admin/outlook/code-tests", json={
            "emails": ["good@outlook.com", "bad@outlook.com"],
            "intervalSeconds": 0,
        })

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["success"], 1)
        self.assertEqual(body["failed"], 1)
        self.assertEqual(calls, ["good@outlook.com", "bad@outlook.com"])
        results = {item["email"]: item for item in body["results"]}
        self.assertTrue(results["good@outlook.com"]["ok"])
        self.assertFalse(results["bad@outlook.com"]["ok"])
        self.assertIn("refresh token expired", results["bad@outlook.com"]["error"])
        accounts = {account["email"]: account for account in client.get("/api/admin/outlook/accounts").json()["accounts"]}
        self.assertEqual(accounts["good@outlook.com"]["lastTestStatus"], "ok")
        self.assertEqual(accounts["good@outlook.com"]["lastError"], "")
        self.assertTrue(accounts["good@outlook.com"]["refreshTokenMasked"].startswith("next..."))
        self.assertNotIn("next-good@outlook.com", accounts["good@outlook.com"]["refreshTokenMasked"])
        self.assertEqual(accounts["bad@outlook.com"]["lastTestStatus"], "error")
        self.assertIn("refresh token expired", accounts["bad@outlook.com"]["lastError"])

    def test_admin_can_start_chatgpt_login_batch_and_persist_statuses(self):
        async def fake_runner(account, settings, code_fetcher, progress=None):
            if progress:
                progress("browser", "打开 ChatGPT 登录页")
            if account["email"].startswith("blocked"):
                return ChatGPTLoginResult(
                    status="blocked",
                    reason="account deactivated",
                    stage="final",
                )
            code = await code_fetcher(account, 0)
            return ChatGPTLoginResult(
                status="ok",
                reason=f"code {code}",
                stage="done",
                plan_status="plus",
                plan_reason="Plus plan detected",
            )

        async def fake_fetcher(account, limit, settings):
            return FetchResult(
                messages=[{
                    "id": f"msg-{account['email']}",
                    "subject": "OpenAI login code",
                    "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
                    "bodyPreview": "Your OpenAI login code is 123456.",
                    "receivedDateTime": "2026-05-26T00:01:00+00:00",
                    "mailbox": "INBOX",
                }],
                provider="outlook",
            )

        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
            "CHATGPT_LOGIN_CONCURRENCY": "1",
        }, outlook_fetcher=fake_fetcher, chatgpt_login_runner=fake_runner)
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={"text": "\n".join([
                "good@outlook.com----pass----client-a----refresh-a",
                "blocked@outlook.com----pass----client-b----refresh-b",
            ])},
        )

        created = client.post("/api/admin/chatgpt/login-tests", json={"all": True})

        self.assertEqual(created.status_code, 200)
        created_body = created.json()
        self.assertTrue(created_body["ok"])
        self.assertEqual(created_body["total"], 2)
        job = client.get(f"/api/admin/chatgpt/login-tests/{created_body['jobId']}")
        self.assertEqual(job.status_code, 200)
        job_body = job.json()
        self.assertEqual(job_body["status"], "completed")
        self.assertEqual(job_body["success"], 2)
        self.assertEqual(job_body["failed"], 0)
        self.assertEqual(job_body["pending"], 0)
        self.assertEqual(job_body["running"], 0)
        self.assertEqual(job_body["cancelled"], 0)
        job_accounts = {item["email"]: item for item in job_body["accounts"]}
        self.assertEqual(job_accounts["good@outlook.com"]["stage"], "done")
        self.assertIn("browser", [event["stage"] for event in job_accounts["good@outlook.com"]["events"]])
        accounts = {account["email"]: account for account in client.get("/api/admin/outlook/accounts").json()["accounts"]}
        self.assertEqual(accounts["good@outlook.com"]["chatgptLastLoginStatus"], "ok")
        self.assertEqual(accounts["good@outlook.com"]["chatgptLastPlanStatus"], "plus")
        self.assertIn("Plus", accounts["good@outlook.com"]["chatgptLastPlanReason"])
        self.assertEqual(accounts["blocked@outlook.com"]["chatgptLastLoginStatus"], "blocked")
        self.assertIn("deactivated", accounts["blocked@outlook.com"]["chatgptLastLoginReason"])

    def test_admin_can_cancel_chatgpt_login_batch(self):
        release_runner = threading.Event()

        async def slow_runner(account, settings, code_fetcher, progress=None):
            if progress:
                progress("browser", "打开 ChatGPT 登录页")
            while not release_runner.is_set():
                await __import__("asyncio").sleep(0.02)
            return ChatGPTLoginResult(status="ok", reason="done", stage="done")

        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
            "CHATGPT_LOGIN_CONCURRENCY": "1",
        }, chatgpt_login_runner=slow_runner)
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})
        client.post(
            "/api/admin/outlook/import",
            json={"text": "\n".join([
                "one@outlook.com----pass----client-a----refresh-a",
                "two@outlook.com----pass----client-b----refresh-b",
            ])},
        )
        created = client.post("/api/admin/chatgpt/login-tests", json={"all": True})
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["jobId"]

        cancelled = client.delete(f"/api/admin/chatgpt/login-tests/{job_id}")
        release_runner.set()
        self.assertEqual(cancelled.status_code, 200)
        body = cancelled.json()
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["cancelled"], 2)
        self.assertEqual(body["completed"], 0)
        accounts = {item["email"]: item for item in body["accounts"]}
        self.assertEqual(accounts["one@outlook.com"]["status"], "cancelled")
        self.assertEqual(accounts["two@outlook.com"]["stage"], "cancelled")

    def test_chatgpt_login_batch_rejects_missing_selected_accounts(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        }, chatgpt_login_runner=lambda *_args, **_kwargs: None)
        client = self.TestClient(app)
        client.post("/api/admin/login", json={"password": "secret-admin"})

        response = client.post("/api/admin/chatgpt/login-tests", json={"emails": ["missing@outlook.com"]})

        self.assertEqual(response.status_code, 404)
        self.assertIn("没有找到", response.json()["detail"])

    def test_extract_chatgpt_code_requires_openai_mail_after_request_time(self):
        messages = [
            {
                "id": "old",
                "subject": "OpenAI login code",
                "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
                "bodyPreview": "Your OpenAI login code is 111111.",
                "receivedDateTime": "2026-05-26T00:00:00+00:00",
            },
            {
                "id": "unrelated",
                "subject": "Security code",
                "from": {"emailAddress": {"address": "noreply@example.com"}},
                "bodyPreview": "Your login code is 222222.",
                "receivedDateTime": "2026-05-26T00:02:00+00:00",
            },
            {
                "id": "new",
                "subject": "Your ChatGPT code",
                "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
                "bodyPreview": "Enter this code to continue: 333333.",
                "receivedDateTime": "2026-05-26T00:03:00+00:00",
            },
        ]

        match = extract_chatgpt_code_from_messages(messages, 1779753660000)

        self.assertEqual(match["code"], "333333")
        self.assertEqual(match["message"]["id"], "new")

    def test_public_query_rejects_unimported_outlook_domain_without_edu_upstream(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "EDU_MAIL_API_TOKEN": "edu-token",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)

        response = client.post("/api/messages/code", json={"email": "missing@outlook.com"})

        self.assertEqual(response.status_code, 404)
        self.assertIn("尚未导入", response.json()["detail"])

    def test_public_query_routes_domain_containing_edu_to_edu_provider(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("Authorization")
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "id": "m1",
                            "alias": "btilpydspexielcqw@reimeijia.edu.kg",
                            "subject": "Edu login code",
                            "from": "noreply@example.com",
                            "body": "Your verification code is 778899.",
                            "created_at": 1710000000,
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        original_async_client = httpx.AsyncClient

        def patched_async_client(*args, **kwargs):
            kwargs["transport"] = transport
            return original_async_client(*args, **kwargs)

        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "EDU_MAIL_API_TOKEN": "edu-token",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)

        with patch("httpx.AsyncClient", patched_async_client):
            response = client.post("/api/messages/code", json={"email": "btilpydspexielcqw@reimeijia.edu.kg"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "edu")
        self.assertEqual(response.json()["code"], "778899")
        self.assertIn("/public-api/messages", captured["url"])
        self.assertEqual(captured["authorization"], "Bearer edu-token")

    def test_public_query_rejects_unsupported_domain_without_edu_upstream(self):
        app = create_app({
            "ADMIN_PASSWORD": "secret-admin",
            "SESSION_SECRET": "test-session-secret",
            "EDU_MAIL_API_TOKEN": "edu-token",
            "DATA_DIR": tempfile.mkdtemp(prefix="edu-mail-web-test-"),
        })
        client = self.TestClient(app)

        response = client.post("/api/messages/code", json={"email": "someone@example.com"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("仅支持", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
