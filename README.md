# MailPass

`mail.amazingzz.xyz` 的公开邮箱验证码查询页。页面免登录，Edu Bearer token 和 Outlook refresh token 仅在服务端保存和使用。

## Environment

复制 `.env.example` 为 `.env`，再填入真实密钥。不要把 `.env` 提交到仓库。

## Outlook Import

管理页支持粘贴文本或选择文本文件批量导入 Outlook / Hotmail 账号。每行一个账号：

```text
邮箱号----密码----客户端 ID----刷新令牌
```

密码字段用于兼容账号池格式，当前版本不使用密码读信。Outlook 读信通过 Microsoft OAuth refresh token 换取 access token，再读取 Inbox/Junk 最新邮件。

## Local Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
ADMIN_PASSWORD=replace-me SESSION_SECRET=dev-secret uvicorn app:app --host 0.0.0.0 --port 8091
```

## Docker

```bash
docker build -t edu-mail-web .
docker run -d --name edu-mail-web \
  -p 127.0.0.1:8091:8091 \
  -v edu-mail-web-data:/app/data \
  -e ADMIN_PASSWORD=replace-me \
  -e SESSION_SECRET=replace-with-random-secret \
  -e SESSION_COOKIE_SECURE=1 \
  -e EDU_MAIL_API_TOKEN=replace-token \
  edu-mail-web
```

## Nginx

See `deploy/nginx-mail.amazingzz.xyz.conf` for the production server block.

```nginx
limit_req_zone $binary_remote_addr zone=mail_public:10m rate=2r/s;
limit_req_zone $binary_remote_addr zone=mail_admin_login:10m rate=10r/m;

server {
    listen 80;
    server_name mail.amazingzz.xyz;

    client_max_body_size 64k;

    location /api/messages/code {
        limit_req zone=mail_public burst=8 nodelay;
        proxy_pass http://127.0.0.1:8091;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/admin/login {
        limit_req zone=mail_admin_login burst=5 nodelay;
        proxy_pass http://127.0.0.1:8091;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:8091;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
