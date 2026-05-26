# Edu Mail Web Agent Guide

This file is the operating guide for agents working in this directory. Follow it for all files under `/Users/zhangzhiwei/Documents/vscodeProjects/edu-mail-web`.

## Project Summary

Edu Mail Web is a small FastAPI service for `mail.amazingzz.xyz`.

- Public users query mailbox verification codes without login.
- Admin users manage the upstream Edu Mail token and imported Outlook/Hotmail accounts.
- Edu bearer tokens, Outlook refresh tokens, admin password, and session secret must stay server-side only.
- The app is branded as MailPass in the static HTML/CSS assets.

Core runtime files:

- `app.py` - FastAPI app, settings, rate limiting, Edu upstream client, Outlook Microsoft Graph client, admin API, and page rendering.
- `static/index.html`, `static/main.js`, `static/styles.css` - public query page.
- `static/admin.html`, `static/admin.js`, `static/styles.css` - admin UI.
- `static/assets/mailpass-icon*.png` - MailPass icons.
- `tests/test_app.py` - unittest coverage for helpers, API auth, config persistence, Outlook import/export/search/alias/used flags, and provider routing.
- `Dockerfile` - production image using Python 3.11, gunicorn, and uvicorn worker.
- `deploy/nginx-mail.amazingzz.xyz.conf` - intended standalone Nginx server block.

## Local Development

Use the existing virtual environment if present, or create one:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Run locally:

```bash
ADMIN_PASSWORD=secret-admin SESSION_SECRET=test-session-secret DATA_DIR=/private/tmp/edu-mail-web-local PORT=8091 .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8091
```

Run tests:

```bash
ADMIN_PASSWORD=secret-admin SESSION_SECRET=test-session-secret .venv/bin/python -m unittest tests/test_app.py
```

Useful smoke checks:

```bash
curl -sS http://127.0.0.1:8091/healthz
curl -sS -X POST http://127.0.0.1:8091/api/messages/code \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@example.edu"}'
```

The public API intentionally uses `POST /api/messages/code`. Admin login is `POST /api/admin/login`, and admin pages use an `edu_mail_admin` cookie.

## Configuration

Settings are loaded from environment variables in `app.py`.

Important variables:

- `PORT` - defaults to `8091`.
- `BIND_HOST` - defaults to `0.0.0.0`.
- `EDU_MAIL_BASE_URL` - production uses `https://inbox.beastxa.com`.
- `EDU_MAIL_API_TOKEN` - secret, never print or commit.
- `ADMIN_PASSWORD` - secret, never print or commit.
- `SESSION_SECRET` - secret, never print or commit.
- `SESSION_COOKIE_SECURE` - production uses secure cookies behind HTTPS.
- `PUBLIC_RATE_LIMIT_REQUESTS`, `PUBLIC_RATE_LIMIT_WINDOW_SECONDS` - public query limiter.
- `ADMIN_RATE_LIMIT_REQUESTS`, `ADMIN_RATE_LIMIT_WINDOW_SECONDS` - admin login limiter.
- `UPSTREAM_TIMEOUT_SECONDS`, `UPSTREAM_MAX_CONCURRENCY` - upstream request controls.
- `OUTLOOK_BULK_TEST_CONCURRENCY` - admin bulk-test concurrency.
- `MESSAGE_CACHE_TTL_SECONDS` - short public message cache.
- `DATA_DIR` - config persistence directory; production maps this to `/app/data`.
- `CONFIG_FILE` - optional explicit JSON config path.

The persisted runtime config is a JSON file with `edu`, `outlookAccounts`, and `updatedAt`. It contains secrets and account credentials, so inspect metadata only unless the user explicitly asks for secret recovery.

## Production Deployment

Production is reachable through `ssh zzw`.

Observed production facts on 2026-05-26:

- Remote host alias: `zzw`.
- Remote user: `zhangzhiwei`.
- Remote app directory: `/home/zhangzhiwei/edu-mail-web`.
- Runtime data directory on host: `/home/zhangzhiwei/edu-mail-web-data`.
- Data mount in container: `/home/zhangzhiwei/edu-mail-web-data -> /app/data`.
- Container name: `edu-mail-web`.
- Image tag: `edu-mail-web:latest`.
- Container restart policy: `unless-stopped`.
- Docker network mode: `bridge`.
- Container command: `gunicorn -k uvicorn.workers.UvicornWorker -w 1 -b 0.0.0.0:8091 app:app`.
- Port mapping: `127.0.0.1:8091 -> 8091/tcp`.
- Health endpoint: `GET http://127.0.0.1:8091/healthz`.
- Public health endpoint: `GET https://mail.amazingzz.xyz/healthz`.
- `HEAD /healthz` returns `405`; use GET for health checks.

Production environment currently includes these expected values or secrets:

- `ADMIN_PASSWORD=<redacted>`
- `ADMIN_RATE_LIMIT_REQUESTS=5`
- `ADMIN_RATE_LIMIT_WINDOW_SECONDS=300`
- `BIND_HOST=0.0.0.0`
- `DATA_DIR=/app/data`
- `EDU_MAIL_API_TOKEN=<redacted>`
- `EDU_MAIL_BASE_URL=https://inbox.beastxa.com`
- `MESSAGE_CACHE_TTL_SECONDS=8`
- `PORT=8091`
- `PUBLIC_RATE_LIMIT_REQUESTS=12`
- `PUBLIC_RATE_LIMIT_WINDOW_SECONDS=60`
- `SESSION_COOKIE_SECURE=<redacted>`
- `SESSION_SECRET=<redacted>`
- `UPSTREAM_MAX_CONCURRENCY=20`
- `UPSTREAM_TIMEOUT_SECONDS=15`

The remote app directory contains `.env`; do not copy or display it unless the user explicitly asks and understands it contains secrets. The host data file `/home/zhangzhiwei/edu-mail-web-data/config.json` is owned by root because the container writes it. Account/token edits should go through the admin UI/API or container process, not manual local edits, unless ownership and merge behavior are handled carefully.

Remote copies may include macOS AppleDouble files such as `._app.py` and `._static`. Avoid packaging those during future deploys.

## Nginx

The repository has a standalone Nginx config at `deploy/nginx-mail.amazingzz.xyz.conf`.

On the server, the live mail server block was observed inside `/etc/nginx/conf.d/sub2api.conf`, after an unrelated `sub2api.amazingzz.xyz` server block. Do not assume the file name matches the repo file. Confirm the live file before changing Nginx:

```bash
ssh zzw 'grep -Rsl "mail.amazingzz.xyz\|127.0.0.1:8091\|edu-mail-web" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null'
```

Observed live Nginx behavior:

- HTTP `mail.amazingzz.xyz` redirects to HTTPS.
- ACME challenges use `/var/www/certbot/mail`.
- TLS cert paths are `/etc/letsencrypt/live/mail.amazingzz.xyz/fullchain.pem` and `privkey.pem`.
- `client_max_body_size` is `64k` for mail.
- `POST /api/messages/code` is rate-limited with zone `mail_public`, burst `8`, and proxies to `http://127.0.0.1:8091`.
- `POST /api/admin/login` is rate-limited with zone `mail_admin_login`, burst `5`, and proxies to `http://127.0.0.1:8091`.
- All other paths proxy to `http://127.0.0.1:8091`.

After Nginx edits, run `sudo nginx -t` and reload only after the config test passes.

## Deployment Workflow

There is no git repository in this local directory at the time this guide was written. Treat deploys as file/image based unless that changes.

Recommended deployment shape:

1. Run local tests.
2. Copy only source files needed for the app, excluding `.venv`, `__pycache__`, `*.pyc`, `.omx`, and `._*` AppleDouble files.
3. Update `/home/zhangzhiwei/edu-mail-web` on `zzw`.
4. Rebuild `edu-mail-web:latest` from `/home/zhangzhiwei/edu-mail-web`.
5. Recreate/restart the `edu-mail-web` container with the same port, data mount, restart policy, and env values.
6. Verify `GET http://127.0.0.1:8091/healthz` on the server.
7. Verify `GET https://mail.amazingzz.xyz/healthz` publicly.
8. If the change touches public query behavior, probe `POST /api/messages/code` with a safe test mailbox or expected error path.

Before replacing the container, inspect the current command and env names so secrets and mount wiring are preserved:

```bash
ssh zzw 'docker inspect edu-mail-web --format "Name={{.Name}} Image={{.Config.Image}} Restart={{.HostConfig.RestartPolicy.Name}} Cmd={{json .Config.Cmd}}"'
ssh zzw 'docker port edu-mail-web && docker inspect edu-mail-web --format "{{range .Mounts}}{{println .Source \"->\" .Destination}}{{end}}"'
```

Never paste full `docker inspect` env output into chat or docs without redacting secret values.

## Safety Rules

- Do not commit or print actual values for `EDU_MAIL_API_TOKEN`, `ADMIN_PASSWORD`, `SESSION_SECRET`, Outlook passwords, client IDs, or refresh tokens.
- Public responses and admin list responses should keep secrets masked. Preserve the masking tests when changing API output.
- Outlook accounts are keyed by normalized email, aliases must stay unique case-insensitively, and stale config writes must preserve newer imported accounts.
- Unimported Outlook/Hotmail/Live/MSN addresses should not fall through to the Edu upstream.
- Domains containing `.edu` can be routed to the Edu provider when an Edu token is configured.
- Keep rate limits in place for public code lookup and admin login.
- Use `GET /healthz` for health checks. A `405` on HEAD is not a service failure.

## Verification Expectations

For code changes, run:

```bash
ADMIN_PASSWORD=secret-admin SESSION_SECRET=test-session-secret .venv/bin/python -m unittest tests/test_app.py
```

For Docker/deploy changes, additionally verify:

```bash
ssh zzw 'curl -sS -m 5 http://127.0.0.1:8091/healthz'
curl -sS -m 8 https://mail.amazingzz.xyz/healthz
```

If changing static UI, inspect both `/` and `/admin` locally or on production after login. Keep the MailPass branding and existing visual language consistent across public and admin pages.
