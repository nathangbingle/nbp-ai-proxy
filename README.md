# nbp-ai-proxy

Tiny Flask service that proxies school-research requests from the NBP proposal builder to Anthropic's Claude API.

Deployed on Railway.

## Env vars

- `ANTHROPIC_API_KEY` — Nathan's key from console.anthropic.com
- `TOOL_SECRET` — shared secret the frontend sends as `X-NBP-Key`
- `ALLOWED_ORIGINS` — optional, extra origins (CSV) on top of the hardcoded NBP domains
- `PORT` — set automatically by Railway

## Endpoints

- `GET /` — health check
- `POST /research` — `{ schoolName }` → `{ ok, research: {...} }`
