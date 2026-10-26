# Headless on Railway: OpenShorts as a remote MCP server for Claude Code

This runs the self-host (BYOK) backend on Railway with no dashboard: the REST
API plus the MCP endpoint at `/mcp`, protected by one shared token. Claude Code
(or any MCP client) connects to it over Streamable HTTP and drives the whole
pipeline: process a video, poll, list clips, re-caption, recut and publish.

## How it is protected

Self-host has no user accounts, so on a public URL anyone could spend your
Gemini key and post to your social accounts. Setting `OPENSHORTS_ACCESS_TOKEN`
turns on `access_gate.py`, and every request must then carry
`Authorization: Bearer <token>` (or `X-API-Key: <token>`). Only these stay open:

- `/health` and `/health/ready`, for Railway's health check.
- `PUT /api/uploads/<uuid>`, the upload slot an authenticated `create_upload`
  call hands out (the random slot id is the capability).
- Clip and thumbnail URLs carrying a valid `?exp=&sig=` signature. The MCP
  tools and webhooks return those, so a clip link opens in a browser for
  `MEDIA_URL_TTL_SECONDS` (24 h by default) and is dead after that.

## Railway service

`railway.json` sets the Dockerfile build, the `/health/ready` health check, a
60 s drain window, and `requiredMountPath`: a deploy refuses to start without
the volume, so clips never land on the ephemeral disk by mistake.

| Setting | Value |
| --- | --- |
| Source | this GitHub repo, Dockerfile build (from `railway.json`) |
| Volume | mounted at `/app/output` (jobs, clips, resume manifests) |
| Public domain | generated `*.up.railway.app`, routed to port 8000 |

### Variables

| Variable | Value | Why |
| --- | --- | --- |
| `OPENSHORTS_ACCESS_TOKEN` | a long random string | Turns the gate on. Required. |
| `GEMINI_API_KEY` | your key | Picks the moments and writes titles. Required unless `LLM_BASE_URL` points at your own model. |
| `UPLOAD_POST_API_KEY` | your key | Optional: needed only for `publish_clip`. |
| `PUBLIC_API_URL` | `https://${{RAILWAY_PUBLIC_DOMAIN}}` | Makes clip and upload URLs absolute. |
| `PORT` | `8000` | The port uvicorn listens on, so Railway routes there. |
| `TORCH_INDEX_URL` | `https://download.pytorch.org/whl/cpu` | Build arg: CPU-only torch, skipping ~3 GB of CUDA libraries. |
| `RAILWAY_RUN_UID` | `0` | Railway mounts volumes as root; the image runs as a non-root user. |
| `MAX_CONCURRENT_JOBS` | `1` | CPU-only box: one job at a time. Raise it once you have seen memory use in the metrics. |
| `DRAIN_TIMEOUT_SECONDS` | `20` | A service with a volume stops before the new deploy starts, so a long drain only delays it. Interrupted jobs resume from their manifest on the volume. |
| `PROXY_DRAIN_SECONDS` | `0` | Same reason: there is no overlapping instance to hand traffic to. |
| `YOUTUBE_COOKIES` | Netscape-format cookies | Optional, see below. |

Generate a token with `openssl rand -hex 32`.

### YouTube from a datacenter IP

YouTube often answers cloud IPs with "Sign in to confirm you're not a bot".
The image already ships the bgutil PO-token provider. If downloads still fail,
set `YOUTUBE_COOKIES`, or `STATIC_PROXY_URLS` (comma-separated proxies). Uploading
the file (below) or a direct video URL always works.

## Connect Claude Code

```bash
export OPENSHORTS_ACCESS_TOKEN=...   # same value as on Railway
claude mcp add --transport http openshorts https://<your-domain>/mcp \
  --header "Authorization: Bearer $OPENSHORTS_ACCESS_TOKEN"
```

Or check it into a project as `.mcp.json`. Claude Code expands `${VAR}` from
your environment, so the token stays out of the file:

```json
{
  "mcpServers": {
    "openshorts": {
      "type": "http",
      "url": "https://<your-domain>/mcp",
      "headers": { "Authorization": "Bearer ${OPENSHORTS_ACCESS_TOKEN}" }
    }
  }
}
```

Fully headless (scripts, cron, CI):

```bash
claude -p "Clip https://www.youtube.com/watch?v=... into 3 shorts. I own this video. \
Wait until the job finishes, then print each clip's title and video_url." \
  --allowedTools "mcp__openshorts"
```

`--allowedTools "mcp__openshorts"` pre-approves every tool on that server; list
single tools (`mcp__openshorts__process_video`, ...) to keep `publish_clip`
behind a prompt.

### Local files

`create_upload` returns an `upload_url`; Claude Code PUTs the file with
`curl -T video.mp4 <upload_url>` (no header needed) and then calls
`process_video` with the `upload_id`.

### Downloading clips

`list_clips` returns signed `video_url`s: `curl -o clip.mp4 "<video_url>"`. To
fetch a bare `/videos/...` path, send the bearer header instead. Finished jobs
stay on the volume for `JOB_RETENTION_SECONDS` (24 h in self-host).

## Smoke test

```bash
curl -s https://<your-domain>/health/ready                     # {"status":"ready"}
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<your-domain>/mcp   # 401
curl -s https://<your-domain>/mcp \
  -H "Authorization: Bearer $OPENSHORTS_ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 300
```

## Pulling upstream updates

This repository keeps the full history of
[mutonby/openshorts](https://github.com/mutonby/openshorts), so updates merge in:

```bash
git remote add upstream https://github.com/mutonby/openshorts.git
git fetch upstream && git merge upstream/main
```

The Railway changes touch upstream files in only a few small places:
`Dockerfile` (the `TORCH_INDEX_URL` arg), `app.py` (the middleware line and
webhook URL signing), `mcp_server.py` (clip URL signing) and `mcp_stdio.py`
(the token fallback). Everything else is new files.
