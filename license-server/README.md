# AgentIC License Server

Production role: authenticate Supabase users, create Lemon Squeezy checkouts, process billing webhooks, issue short-lived AgentIC entitlements, and store count-only usage events.

This service must never receive chip source code, PDK files, prompts, logs, TCL scripts, reports, waveforms, or artifacts.

## Endpoints

- `GET /health`
- `GET /license/status`
- `POST /checkout/create`
- `POST /usage/build`
- `POST /webhooks/lemonsqueezy`

Authenticated desktop calls must include:

```http
Authorization: Bearer <supabase_access_token>
```

## Supabase Setup

Run `schema.sql` in the Supabase SQL editor. Keep RLS enabled. The license server writes with the service role key; clients should only be able to read their own rows.

## VPS Deploy

```bash
cd /opt/agentic-license
cp .env.example .env
nano .env
docker compose up -d --build
curl http://127.0.0.1:8088/health
```

## Nginx

Copy `deploy/agentic-license.nginx.conf` to `/etc/nginx/sites-available/agentic-license`, replace `license.example.com`, enable it, and add TLS with Certbot.

```bash
sudo ln -s /etc/nginx/sites-available/agentic-license /etc/nginx/sites-enabled/agentic-license
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d license.example.com
```

## Desktop Environment

Desktop/frontend may know:

```env
VITE_SUPABASE_URL=
VITE_SUPABASE_ANON_KEY=
VITE_AGENTIC_LICENSE_SERVER_URL=https://license.example.com
AGENTIC_ENTITLEMENT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
```

Desktop/frontend must not know:

```env
SUPABASE_SERVICE_ROLE_KEY=
LEMON_SQUEEZY_API_KEY=
LEMON_SQUEEZY_WEBHOOK_SECRET=
ENTITLEMENT_JWT_SECRET=
```

## Desktop Communication Path

AgentIC Desktop never calls Lemon Squeezy directly and never receives merchant secrets.

```txt
React renderer
  Authorization: Bearer <Supabase access token>
  -> http://localhost:7860/license/status

Local FastAPI backend
  forwards the same bearer token only
  -> https://license.example.com/license/status

License server
  verifies Supabase user
  checks subscription rows
  returns signed_entitlement

Local FastAPI backend
  verifies signed_entitlement with AGENTIC_ENTITLEMENT_PUBLIC_KEY
  gates /chat/converse, /tools/install, workspace reads, and usage reporting
```

Checkout uses the same pattern:

```txt
React renderer -> localhost:7860/checkout/create
Local backend -> license server /checkout/create
License server -> Lemon Squeezy API
Desktop opens returned checkout_url in the browser
```

Production entitlement signing should use `ENTITLEMENT_JWT_PRIVATE_KEY` on the license server and `AGENTIC_ENTITLEMENT_PUBLIC_KEY` in the packaged desktop backend environment. The HS256 `ENTITLEMENT_JWT_SECRET` fallback is for development only.
