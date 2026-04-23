"""
NBP AI Proxy — tiny Flask service that accepts a school name,
calls Claude with web search, and returns structured research.

Env vars (set in Railway):
  ANTHROPIC_API_KEY   required — Nathan's Anthropic API key
  TOOL_SECRET         required — shared secret the frontend sends as X-NBP-Key
  ALLOWED_ORIGINS     optional — comma-separated list of allowed origins
                                  (defaults to NBP domains)
  PORT                set by Railway automatically

Endpoints:
  GET  /                → health check
  POST /research        → { schoolName } → { ok, research }
"""

import os
import json
import re
from flask import Flask, request, jsonify, make_response
import httpx

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TOOL_SECRET       = os.environ.get("TOOL_SECRET", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER      = os.environ.get("GITHUB_OWNER", "nathangbingle")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "nbp-school-proposals")
GITHUB_BRANCH     = os.environ.get("GITHUB_BRANCH", "main")

DEFAULT_ORIGINS = [
    "https://nathangbingle.github.io",
    "https://nathanbinglephotography.com",
    "https://www.nathanbinglephotography.com",
    # Local dev
    "http://localhost:8000",
    "http://localhost:8080",
    "http://127.0.0.1:8000",
]
_extra = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = set(DEFAULT_ORIGINS + [o.strip() for o in _extra.split(",") if o.strip()])

SYSTEM_PROMPT = """You are researching a US school to fill in a photography proposal. Given a school name, use web search to find the school's official information and return a single JSON object with these exact fields:

{
  "schoolName": "Full official name",
  "schoolShort": "Short version used colloquially (e.g. 'Banks Trail' for 'Banks Trail Middle School')",
  "mascot": "Mascot name (e.g. 'Bobcats', 'Patriots', or empty string if unknown)",
  "cityState": "City, ST (e.g. 'Fort Mill, SC')",
  "governingState": "Full state name (e.g. 'South Carolina')",
  "gradeRange": "Grade levels (e.g. 'K-5', '6-8', '9-12', 'K-12')",
  "schoolType": "One of: public elementary school, public middle school, public high school, public K-12 school, private school, private K-8 school, classical public charter school, public charter school, virtual public school",
  "hasSeniors": true if 12th grade is served, false otherwise,
  "primaryColor": "Hex like #1E3A8A, based on school's official colors if findable",
  "accentColor": "Hex like #D4A54C, the secondary/accent color",
  "colorsConfidence": "high | medium | low",
  "isCharter": true if charter,
  "governedBy": "Charter authorizing body if applicable, empty string otherwise",
  "managedBy": "Management organization if applicable, empty string otherwise",
  "notes": "One short sentence of anything noteworthy for proposal context"
}

Rules:
- Return ONLY the JSON object, no commentary, no markdown fences
- Use empty string "" for fields you cannot confidently determine
- For hasSeniors: infer from gradeRange
- For colors: only provide hex codes if you find them stated on the school's official site. Otherwise return empty strings and colorsConfidence: "low"
- Prefer the darker, more saturated color as primaryColor and lighter/brighter as accentColor
- Keep schoolShort under 20 characters
"""


def cors_headers(request_origin):
    """Return CORS headers. Echo the origin if allowed, otherwise the first default."""
    origin = request_origin if request_origin in ALLOWED_ORIGINS else DEFAULT_ORIGINS[0]
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-NBP-Key",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "service": "nbp-ai-proxy",
        "status": "ok",
        "has_api_key": bool(ANTHROPIC_API_KEY) and not ANTHROPIC_API_KEY.startswith("REPLACE"),
        "has_secret": bool(TOOL_SECRET),
        "has_github_token": bool(GITHUB_TOKEN),
    })


@app.route("/research", methods=["OPTIONS"])
def research_options():
    origin = request.headers.get("Origin", "")
    resp = make_response("", 204)
    for k, v in cors_headers(origin).items():
        resp.headers[k] = v
    return resp


@app.route("/research", methods=["POST"])
def research():
    origin = request.headers.get("Origin", "")
    cors = cors_headers(origin)

    def reply(payload, status=200):
        resp = make_response(jsonify(payload), status)
        for k, v in cors.items():
            resp.headers[k] = v
        return resp

    # Auth
    if TOOL_SECRET:
        provided = request.headers.get("X-NBP-Key", "")
        if provided != TOOL_SECRET:
            return reply({"error": "Unauthorized"}, 401)

    if not ANTHROPIC_API_KEY:
        return reply({"error": "ANTHROPIC_API_KEY not set on backend"}, 500)

    data = request.get_json(silent=True) or {}
    school_name = (data.get("schoolName") or "").strip()
    if not school_name or len(school_name) > 200:
        return reply({"error": "schoolName required (1-200 chars)"}, 400)

    try:
        with httpx.Client(timeout=60.0) as client:
            api_res = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 1500,
                    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                    "system": SYSTEM_PROMPT,
                    "messages": [{
                        "role": "user",
                        "content": f'Research this school and return the JSON: "{school_name}"'
                    }],
                },
            )
    except httpx.RequestError as e:
        return reply({"error": f"Upstream request failed: {e}"}, 502)

    if api_res.status_code != 200:
        return reply({
            "error": f"Anthropic API returned {api_res.status_code}",
            "detail": api_res.text[:500],
        }, 502)

    result = api_res.json()
    text_blocks = [b for b in result.get("content", []) if b.get("type") == "text"]
    last_text = text_blocks[-1]["text"] if text_blocks else ""

    # Strip markdown fences and parse JSON
    cleaned = re.sub(r"^```(?:json)?\s*", "", last_text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    parsed = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", last_text)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    if not parsed:
        return reply({
            "error": "Could not parse research response",
            "raw": last_text[:400],
        }, 502)

    return reply({"ok": True, "research": parsed})


@app.route("/publish", methods=["OPTIONS"])
def publish_options():
    origin = request.headers.get("Origin", "")
    resp = make_response("", 204)
    for k, v in cors_headers(origin).items():
        resp.headers[k] = v
    return resp


@app.route("/publish", methods=["POST"])
def publish():
    """Push an HTML file into the nbp-school-proposals repo."""
    origin = request.headers.get("Origin", "")
    cors = cors_headers(origin)

    def reply(payload, status=200):
        resp = make_response(jsonify(payload), status)
        for k, v in cors.items():
            resp.headers[k] = v
        return resp

    if TOOL_SECRET and request.headers.get("X-NBP-Key", "") != TOOL_SECRET:
        return reply({"error": "Unauthorized"}, 401)

    if not GITHUB_TOKEN:
        return reply({"error": "GITHUB_TOKEN not set on backend"}, 500)

    data = request.get_json(silent=True) or {}
    filename = (data.get("filename") or "").strip()
    html_content = data.get("html", "")

    # Validate filename: allow only slug-like names ending in .html
    if not filename or not re.match(r"^[a-z0-9][a-z0-9-]{0,80}\.html$", filename):
        return reply({"error": "Invalid filename. Must be a slug ending in .html"}, 400)
    if not html_content or len(html_content) > 500_000:
        return reply({"error": "html required (1 byte - 500 KB)"}, 400)

    import base64
    encoded = base64.b64encode(html_content.encode("utf-8")).decode("ascii")
    api = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{filename}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    # Check if file exists (need sha to overwrite)
    existing_sha = None
    try:
        with httpx.Client(timeout=20.0) as client:
            head = client.get(f"{api}?ref={GITHUB_BRANCH}", headers=headers)
            if head.status_code == 200:
                existing_sha = head.json().get("sha")
    except httpx.RequestError:
        pass

    payload = {
        "message": f"{'Update' if existing_sha else 'Add'} proposal: {filename}",
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }
    if existing_sha:
        payload["sha"] = existing_sha

    try:
        with httpx.Client(timeout=30.0) as client:
            put = client.put(api, headers=headers, json=payload)
    except httpx.RequestError as e:
        return reply({"error": f"GitHub request failed: {e}"}, 502)

    if put.status_code not in (200, 201):
        err = put.json() if put.text else {}
        return reply({"error": err.get("message", f"GitHub returned {put.status_code}")}, 502)

    pages_url = f"https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}/{filename}"
    return reply({
        "ok": True,
        "url": pages_url,
        "updated": bool(existing_sha),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
