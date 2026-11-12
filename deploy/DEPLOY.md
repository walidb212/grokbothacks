# Deploy: demo3.ai-vance.de

Server: aivance-cloud (46.224.24.118). The shared Caddy container (`caddy`, Docker network `web`, config `/opt/caddy/Caddyfile`) does HTTPS. This app adds two containers to that network:

| Container | What | URL |
|---|---|---|
| `demo3-api` | FastAPI, contract from README (`/analyze`, `/analysis/{job_id}`) | `https://demo3.ai-vance.de/api/...` |
| `demo3-web` | Static UI from `ui/` (nginx), incl. the ElevenLabs voice analyst | `https://demo3.ai-vance.de/` |

Until the real pipeline is wired in, the API returns `mock/report.json` with `"mock": true`.

## 1. Upload code (Mac, from the repo root)

```bash
cd ~/grokbothacks && pwd     # must print /Users/cyruslang/grokbothacks
ssh root@46.224.24.118 "mkdir -p /opt/demo3/app"
rsync -avz --chown=demo3:demo3 --exclude node_modules --exclude .next --exclude .env --exclude .git --exclude __pycache__ ./ root@46.224.24.118:/opt/demo3/app/
```

## 2. Build and start (server)

```bash
ssh root@46.224.24.118
cd /opt/demo3/app
docker builder prune -f                       # disk is at 86 %, frees build cache only
cp .env.example .env && nano .env && chmod 600 .env
docker compose up -d --build                  # api + web
docker compose ps
```

## 3. Add the Caddy block (server, once)

```bash
cp /opt/caddy/Caddyfile /opt/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)-before-demo3
cat /opt/demo3/app/deploy/Caddyfile.demo3 >> /opt/caddy/Caddyfile
docker inspect caddy --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker exec caddy caddy reload   --config /etc/caddy/Caddyfile --adapter caddyfile
```

Use the destination path from `docker inspect` if it is not `/etc/caddy/Caddyfile`. If `validate` fails, do not reload; restore the backup.

## 4. Check

```bash
curl https://demo3.ai-vance.de/api/health                    # {"ok":true}
curl https://demo3.ai-vance.de/api/analysis/demo-stratford   # mock report
```

Open https://demo3.ai-vance.de: the Stratford sample loads automatically. Voice needs `ELEVENLABS_API_KEY` and `ELEVENLABS_AGENT_ID` in `.env` (see `voice/README.md`).

## Redeploy

```bash
# Mac
cd ~/grokbothacks && rsync -avz --chown=demo3:demo3 --exclude node_modules --exclude .next --exclude .env --exclude .git --exclude __pycache__ ./ root@46.224.24.118:/opt/demo3/app/
# Server
cd /opt/demo3/app && docker compose up -d --build
```

## Switching to a Next.js UI later

`deploy/web.Dockerfile` builds a Next.js app from `web/` (needs `output: "standalone"`). Point the `web` service in `docker-compose.yml` at it; keep container name `demo3-web` and port 3000.

## After the event

Remove the demo3 block from `/opt/caddy/Caddyfile`, validate, reload; `docker compose down`; rotate keys and the root password.
