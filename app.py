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
import base64
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

SYSTEM_PROMPT = """You are researching a US school to fill in a photography proposal. Given a school name, use web search to find the school's official information and return a single JSON object.

REQUIRED SEARCHES — perform these in order:

1. School identity: search for the school's full name to confirm name, location, grade range, mascot, type (public/private/charter).

2. Brand colors: search for the school's STATED colors in text. Try queries like "[School Name] school colors", "[School Name] colors mascot", "[School Name] About". Schools usually state their colors in their About page, history page, handbook, or Wikipedia entry (e.g. "Our school colors are navy and gold"). Also check the school's main website (.org, .k12, .net, .edu) for any text that names the colors. Convert color names into hex codes (e.g. "navy" → "#1E3A8A", "gold" → "#D4A54C", "kelly green" → "#4CBB17", "maroon" → "#800000", "Carolina blue" → "#7BAFD4"). Return the hex codes you would use to represent those named colors faithfully. DO NOT use a school district's colors as a fallback for a specific school — find that specific school's own colors. If you genuinely cannot find the school's stated colors after multiple searches, return empty strings and colorsConfidence: "low".

3. Staff contacts: search for "[School Name] principal" and "[School Name] staff directory" and "[School Name] contact" to find:
   - Principal: name + email
   - Assistant Principal(s): name + email (if multiple, return the first)
   - Yearbook coordinator / advisor: name + email (often a teacher; sometimes called yearbook sponsor)
   - Secretary or front office / main office: name + email (may just be a generic info@ address)
   Many schools list staff emails on their site. If no specific email is published, leave the email empty — DO NOT invent or guess email addresses.

4. School logo: visit the school's official website homepage and find the SCHOOL-SPECIFIC logo image. This is usually in the page header (top of the page), often inside an <img> tag, sometimes inside a logo container. PRIORITIZE the school's own crest/mascot logo (e.g. a Coyote head, an Eagle, a school monogram) over a district-wide shield. If the page only shows a district logo (e.g. "Fort Mill Schools" or "CMS"), still return that — but flag it in logoNote.

   Return the FULL absolute URL to the logo image (e.g. https://www.example.k12.sc.us/cms/lib/images/logo.png). PNG and SVG are preferred; JPG is acceptable. AVOID returning:
   - Tiny favicons (32x32 favicon.ico)
   - Generic stock icons or social media share images
   - Decorative banner photos that aren't actually the logo
   - URLs that 404 or require login

   If you cannot confidently find a usable logo URL, return empty string and set logoConfidence: "low".

Return EXACTLY this JSON shape:

{
  "schoolName": "Full official name",
  "schoolShort": "Short colloquial version under 20 chars (e.g. 'Banks Trail' for 'Banks Trail Middle School')",
  "mascot": "Mascot name only (e.g. 'Bobcats', 'Patriots') or empty string",
  "cityState": "City, ST",
  "governingState": "Full state name",
  "gradeRange": "e.g. 'K-5', '6-8', '9-12', 'K-12'",
  "schoolType": "One of: public elementary school, public middle school, public high school, public K-12 school, private school, private K-8 school, classical public charter school, public charter school, virtual public school",
  "hasSeniors": true if 12th grade,
  "officialWebsite": "URL of the school's main official website (the one you used for color extraction)",
  "primaryColor": "Hex code for the school's primary color (darker/dominant), e.g. '#1E3A8A'. Convert color names to hex.",
  "accentColor": "Hex code for the school's secondary/accent color (lighter/brighter), e.g. '#D4A54C'",
  "colorsConfidence": "high if the school's website or About page explicitly states the colors, medium if a reliable third-party source (Wikipedia, news article) names them, low if defaulted/unknown",
  "colorSource": "Short note: where you found the colors stated, e.g. 'About page on banks-trail.k12.sc.us' or 'Wikipedia entry'",
  "isCharter": true if charter,
  "governedBy": "Charter authorizing body or empty",
  "managedBy": "Management organization or empty",
  "logoUrl": "Full absolute URL to the school's logo image, or empty string if not found",
  "logoConfidence": "high if found a clear school-specific logo on the official site, medium if only a district shield is available, low if not found or unsure",
  "logoNote": "Short note about logo source/type, e.g. 'Coyote mascot logo from school homepage header' or 'District shield only — no school-specific logo found'",
  "contacts": [
    {"role": "Principal",            "name": "...", "email": "..."},
    {"role": "Assistant Principal",  "name": "...", "email": "..."},
    {"role": "Yearbook Coordinator", "name": "...", "email": "..."},
    {"role": "Secretary",            "name": "...", "email": "..."}
  ],
  "notes": "One short sentence of context for the proposal (e.g. 'New school opening fall 2026; expanding to add 6th grade next year.')"
}

Rules:
- Return ONLY the JSON object, no commentary, no markdown fences
- Always return all four contact roles in the contacts array; use empty strings for fields you can't find
- NEVER invent or guess emails — if you can't find a real published email, leave it empty
- Colors must be valid 6-digit hex (#RRGGBB)
- Prefer darker/more saturated as primaryColor, lighter/brighter as accentColor
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
                    "max_tokens": 3500,
                    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}],
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


# ─────────────────────────────────────────────────────────────────────────────
# Logo resolver — fetches a logo URL server-side, returns base64 data URL.
# Why this exists:
#   - Bypasses browser CORS restrictions on cross-origin school websites
#   - Embeds the image into the published HTML so it stays alive even if
#     the school redesigns their site and the original URL 404s later
#   - Validates that the URL actually resolves to a usable image
# ─────────────────────────────────────────────────────────────────────────────
ALLOWED_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/jpg", "image/gif",
    "image/svg+xml", "image/webp",
}
MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB cap


@app.route("/logo-resolve", methods=["OPTIONS"])
def logo_resolve_options():
    origin = request.headers.get("Origin", "")
    resp = make_response("", 204)
    for k, v in cors_headers(origin).items():
        resp.headers[k] = v
    return resp


@app.route("/logo-resolve", methods=["POST"])
def logo_resolve():
    """Take a logo URL and return base64 data URL embeddable directly in HTML."""
    origin = request.headers.get("Origin", "")
    if request.headers.get("X-NBP-Key") != TOOL_SECRET:
        return reply({"error": "Unauthorized"}, 401)

    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return reply({"error": "Valid http(s) URL required"}, 400)

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            r = client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; NBP-Logo-Resolver/1.0)",
                    "Accept": "image/png,image/jpeg,image/svg+xml,image/webp,image/gif,*/*",
                },
            )
    except httpx.RequestError as e:
        return reply({"error": f"Fetch failed: {e}"}, 502)

    if r.status_code != 200:
        return reply({"error": f"Logo URL returned {r.status_code}"}, 502)

    content_type = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_IMAGE_MIMES:
        # Some servers serve SVG as text/xml or similar — try to recover from URL extension
        ext = url.lower().rsplit(".", 1)[-1] if "." in url else ""
        guess = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp",
        }.get(ext)
        if guess:
            content_type = guess
        else:
            return reply({"error": f"Unsupported content type: {content_type}"}, 415)

    body_bytes = r.content
    if len(body_bytes) == 0:
        return reply({"error": "Empty response"}, 502)
    if len(body_bytes) > MAX_LOGO_BYTES:
        return reply({"error": f"Logo too large: {len(body_bytes)} bytes (max 2 MB)"}, 413)

    encoded = base64.b64encode(body_bytes).decode("ascii")
    data_url = f"data:{content_type};base64,{encoded}"

    return reply({
        "ok": True,
        "dataUrl": data_url,
        "contentType": content_type,
        "bytes": len(body_bytes),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
