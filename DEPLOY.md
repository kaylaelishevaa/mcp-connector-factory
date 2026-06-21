# Deploy guide (generic)

This replaces the original, infra-specific runbooks. It describes the deploy
shape both servers share — a Docker container behind a Cloudflare named tunnel,
connected to Claude as a custom connector via a static bearer token. Substitute
your own host for the `203.0.113.10` / `deploy@<DROPLET_IP>` placeholders.

The two servers are identical to deploy; only the env vars and ports differ
(Lark → `8080`, Web → `8081`). Pick one and repeat for the other.

## 0. Prerequisites

- A small Linux host (a cloud "droplet" is plenty) with Docker + Docker Compose.
- A Cloudflare account with a domain on it (for the **named** tunnel + stable
  hostname). The ephemeral `trycloudflare.com` quick tunnel works for testing but
  gives you a new random URL on every restart — fine to prove the connector,
  not for a connector you paste into Claude once.
- SSH access as a non-root deploy user, e.g. `deploy@<DROPLET_IP>`.

## 1. Configure the environment

Copy the template and fill it in. Never commit the result.

```bash
cp .env.example .env
```

Generate the inbound bearer token (this is the secret you'll paste into Claude):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put it in `.env` as the server's token var:

- **Lark:** `LARK_MCP_TOKEN`, plus the Lark app credentials (`LARK_APP_ID`,
  `LARK_APP_SECRET`, `LARK_BASE_ID`) — all from env, none hardcoded.
- **Web:** `WEB_MCP_TOKEN`, plus the outbound service-account creds
  (`WEB_SERVICE_EMAIL` / `WEB_SERVICE_PASSWORD`) the client logs in with.

Leave every `*_WRITE_ENABLED` flag at its template default until you have watched
the read traffic and decided you want writes (see the security-posture section of
the [README](README.md)).

## 2. Build and run

```bash
docker compose build
docker compose up -d
```

Health check on the host:

```bash
curl -s localhost:8080/ | jq    # Lark   (use 8081 for Web)
```

The compose file defines two services: the FastAPI app (internal port only) and a
`cloudflared` daemon that exposes it.

## 3. Expose via a Cloudflare named tunnel

Create a **named** (remotely-managed) tunnel in the Cloudflare Zero Trust
dashboard and point a public hostname at the container:

- Public hostname: `lark-mcp.example.com` (or `web-mcp.example.com`)
- Service: `http://lark-mcp:8080` (the compose service name + internal port)

Cloudflare issues a tunnel token; put it in `.env` as `CF_TUNNEL_TOKEN` so the
`cloudflared` service can authenticate. This gives you a **stable** HTTPS URL that
survives restarts — which matters, because Claude stores the connector URL.

> Quick-tunnel fallback for testing only:
> `cloudflared tunnel --no-autoupdate --url http://lark-mcp:8080`
> then grab the printed `*.trycloudflare.com` URL from `docker compose logs
> cloudflared`. It changes every restart.

## 4. Connect it to Claude

In the Claude app → **Settings → Connectors → Add custom connector**:

- **URL:** your stable tunnel hostname, e.g. `https://lark-mcp.example.com`
- **OAuth Client ID:** anything (the OAuth facade ignores it) — e.g. `operator`
- **OAuth Client Secret:** paste the **bearer token** from step 1.

The server's OAuth facade accepts the connector's OAuth handshake and hands the
bearer token straight back as the access token (see the README's "lessons
learned" — this is a single-user shortcut, not real OAuth). Claude then sends that
token as `Authorization: Bearer …` on every MCP call.

Confirm the connector lists its tools, then try a query.

## 5. Redeploy

```bash
# from your workstation
rsync -az --exclude .venv --exclude .env ./ deploy@<DROPLET_IP>:/home/deploy/<app>/
ssh deploy@<DROPLET_IP> 'cd /home/deploy/<app> && docker compose build && docker compose up -d'
```

`scripts/deploy.sh` and `scripts/setup_droplet.sh` automate steps 2–3 and 5; read
them before running and replace the placeholder host with your own.

## Operational notes

- **Token rotation:** the bearer token is the single inbound credential. Rotate it
  on a schedule; rotating means regenerating it in `.env`, `docker compose up -d`,
  and re-pasting it into Claude's connector secret field.
- **Audit log:** every tool call lands in the JSON-line log under the container's
  log root; writes additionally land in an append-only audit stream with
  before/after snapshots. That stream is your after-the-fact review and, for Lark,
  pairs with native revision history for rollback.
- **Rate limits:** the SaaS backend (Lark) caps at ~100 req/min/app and the free
  Cloudflare tunnel has no SLA — both are natural ceilings for a single-user
  connector, not something the server enforces today.
