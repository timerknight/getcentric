"""
Centric Automation Server
Flask API that n8n workflows call for each pipeline phase.
Deploy to Railway or any Docker host.
"""

import os
import json
import base64
import hashlib
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("centric")

# ── Phase 1: Discovery ──────────────────────────────────────────────

@app.route("/api/discover", methods=["POST"])
def discover_firms():
    """
    Search Google Places for CPA firms in a territory.
    Input: { "city": "Sacramento", "state": "CA", "zip_codes": ["95814","95816",...] }
    Output: List of firms with metadata
    """
    import requests

    data = request.json
    city = data.get("city", "Sacramento")
    state = data.get("state", "CA")
    zip_codes = data.get("zip_codes", [])
    api_key = os.environ["GOOGLE_PLACES_API_KEY"]

    queries = ["CPA firm", "tax accountant", "accounting firm", "certified public accountant"]
    all_firms = {}

    for zip_code in zip_codes:
        for query in queries:
            search_text = f"{query} {zip_code} {city} {state}"
            url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
            params = {"query": search_text, "key": api_key}

            try:
                resp = requests.get(url, params=params, timeout=10)
                results = resp.json().get("results", [])

                for place in results:
                    pid = place.get("place_id")
                    if pid and pid not in all_firms:
                        all_firms[pid] = {
                            "place_id": pid,
                            "name": place.get("name", ""),
                            "address": place.get("formatted_address", ""),
                            "rating": place.get("rating", 0),
                            "review_count": place.get("user_ratings_total", 0),
                            "lat": place["geometry"]["location"]["lat"],
                            "lng": place["geometry"]["location"]["lng"],
                            "types": place.get("types", []),
                            "business_status": place.get("business_status", ""),
                        }
            except Exception as e:
                log.error(f"Places API error for {zip_code}/{query}: {e}")

    # Get website URLs via Place Details for each firm
    firms_with_details = []
    for pid, firm in all_firms.items():
        try:
            detail_url = "https://maps.googleapis.com/maps/api/place/details/json"
            detail_params = {
                "place_id": pid,
                "fields": "website,formatted_phone_number,opening_hours,url",
                "key": api_key,
            }
            detail_resp = requests.get(detail_url, params=detail_params, timeout=10)
            details = detail_resp.json().get("result", {})

            firm["website"] = details.get("website", "")
            firm["phone"] = details.get("formatted_phone_number", "")
            firm["google_url"] = details.get("url", "")
            firm["has_hours"] = bool(details.get("opening_hours"))

            firms_with_details.append(firm)
        except Exception as e:
            log.error(f"Details API error for {pid}: {e}")
            firm["website"] = ""
            firms_with_details.append(firm)

    return jsonify({
        "total_found": len(firms_with_details),
        "firms": firms_with_details,
        "search_params": {"city": city, "state": state, "zip_count": len(zip_codes)},
    })


@app.route("/api/filter", methods=["POST"])
def filter_firms():
    """
    Apply category, size, and website filters.
    Input: { "firms": [...] }
    Output: { "qualified": [...], "no_website": [...], "skipped": [...] }
    """
    firms = request.json.get("firms", [])

    skip_types = {"accounting", "tax_preparation_service", "financial_planner"}
    skip_keywords = ["h&r block", "jackson hewitt", "liberty tax", "turbotax",
                     "deloitte", "kpmg", "ernst & young", "pricewaterhousecoopers", "pwc", "ey"]

    qualified = []
    no_website = []
    skipped = []

    for firm in firms:
        name_lower = firm.get("name", "").lower()

        # Skip big chains and Big 4
        if any(kw in name_lower for kw in skip_keywords):
            firm["skip_reason"] = "chain_or_big4"
            skipped.append(firm)
            continue

        # Skip firms with too many reviews (likely large firm)
        if firm.get("review_count", 0) > 200:
            firm["skip_reason"] = "likely_large_firm"
            skipped.append(firm)
            continue

        # Route firms without websites to separate list
        if not firm.get("website"):
            no_website.append(firm)
            continue

        qualified.append(firm)

    return jsonify({
        "qualified": qualified,
        "no_website": no_website,
        "skipped": skipped,
        "counts": {
            "qualified": len(qualified),
            "no_website": len(no_website),
            "skipped": len(skipped),
        },
    })


# ── Phase 2: Capture ────────────────────────────────────────────────

@app.route("/api/capture", methods=["POST"])
def capture_website():
    """
    Screenshot and scrape a CPA firm's website using Playwright.
    Input: { "url": "https://example.com", "firm_name": "..." }
    Output: { "screenshots": {desktop, mobile, fullpage}, "scraped": {...}, "tech": {...} }
    """
    from playwright.sync_api import sync_playwright

    url = request.json.get("url")
    if not url:
        return jsonify({"error": "url required"}), 400

    result = {"screenshots": {}, "scraped": {}, "tech": {}, "url": url}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            # Desktop screenshot
            desktop = browser.new_page(viewport={"width": 1280, "height": 900})
            desktop.goto(url, timeout=30000, wait_until="networkidle")

            result["screenshots"]["desktop"] = base64.b64encode(
                desktop.screenshot(type="jpeg", quality=80)
            ).decode()

            result["screenshots"]["fullpage"] = base64.b64encode(
                desktop.screenshot(type="jpeg", quality=60, full_page=True)
            ).decode()

            # Scrape content from desktop page
            result["scraped"] = desktop.evaluate("""() => {
                const getText = (sel) => {
                    const el = document.querySelector(sel);
                    return el ? el.textContent.trim() : '';
                };
                const getAll = (sel) => [...document.querySelectorAll(sel)].map(e => e.textContent.trim()).filter(Boolean);
                const getMeta = (name) => {
                    const el = document.querySelector(`meta[name="${name}"], meta[property="${name}"]`);
                    return el ? el.getAttribute('content') : '';
                };

                return {
                    title: document.title || '',
                    meta_description: getMeta('description'),
                    og_title: getMeta('og:title'),
                    og_description: getMeta('og:description'),
                    h1s: getAll('h1'),
                    h2s: getAll('h2'),
                    h3s: getAll('h3'),
                    body_text: document.body?.innerText?.substring(0, 5000) || '',
                    links: [...document.querySelectorAll('a[href]')].map(a => ({
                        text: a.textContent.trim().substring(0, 80),
                        href: a.href
                    })).slice(0, 50),
                    images: [...document.querySelectorAll('img')].map(img => ({
                        src: img.src,
                        alt: img.alt || '',
                        width: img.naturalWidth,
                        height: img.naturalHeight
                    })).slice(0, 30),
                    forms: [...document.querySelectorAll('form')].map(f => ({
                        action: f.action,
                        fields: [...f.querySelectorAll('input,textarea,select')].map(i => i.name || i.type)
                    })),
                    phone_links: [...document.querySelectorAll('a[href^="tel:"]')].map(a => a.href),
                    email_links: [...document.querySelectorAll('a[href^="mailto:"]')].map(a => a.href.replace('mailto:', '')),
                    schema: (() => {
                        const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
                        return scripts.map(s => { try { return JSON.parse(s.textContent); } catch { return null; } }).filter(Boolean);
                    })(),
                    copyright_text: (() => {
                        const body = document.body?.innerText || '';
                        const match = body.match(/©\\s*(\\d{4})/);
                        return match ? match[1] : '';
                    })()
                };
            }""")

            # Tech signals
            result["tech"] = desktop.evaluate("""() => {
                const has = (sel) => !!document.querySelector(sel);
                return {
                    has_viewport_meta: has('meta[name="viewport"]'),
                    has_analytics: has('script[src*="googletagmanager"], script[src*="google-analytics"]'),
                    has_ssl: location.protocol === 'https:',
                    cms_hints: (() => {
                        const gen = document.querySelector('meta[name="generator"]');
                        if (gen) return gen.content;
                        if (document.querySelector('link[href*="wp-content"]')) return 'WordPress';
                        if (document.querySelector('meta[name="wix-dynamic-custom-elements"]')) return 'Wix';
                        if (document.querySelector('meta[content*="squarespace"]')) return 'Squarespace';
                        return 'unknown';
                    })()
                };
            }""")

            # Performance
            timing = desktop.evaluate("""() => {
                const t = performance.timing;
                return { load_time_ms: t.loadEventEnd - t.navigationStart };
            }""")
            result["tech"]["load_time_ms"] = timing.get("load_time_ms", 0)

            desktop.close()

            # Mobile screenshot
            mobile = browser.new_page(viewport={"width": 375, "height": 812},
                                       user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)")
            mobile.goto(url, timeout=30000, wait_until="networkidle")

            result["screenshots"]["mobile"] = base64.b64encode(
                mobile.screenshot(type="jpeg", quality=80)
            ).decode()

            # Mobile-specific checks
            result["tech"]["mobile_checks"] = mobile.evaluate("""() => {
                const viewport = document.querySelector('meta[name="viewport"]');
                const hasOverflow = document.body.scrollWidth > window.innerWidth;
                const smallTaps = [...document.querySelectorAll('a, button, input')].filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && (r.width < 48 || r.height < 48);
                }).length;
                const totalTaps = document.querySelectorAll('a, button, input').length;
                const hasHamburger = !!document.querySelector('[class*="hamburger"], [class*="mobile-menu"], [class*="nav-toggle"], [aria-label*="menu"]');
                const phoneIsLink = !!document.querySelector('a[href^="tel:"]');

                return {
                    viewport_present: !!viewport,
                    horizontal_overflow: hasOverflow,
                    small_tap_targets: smallTaps,
                    total_tap_targets: totalTaps,
                    has_mobile_nav: hasHamburger,
                    phone_is_tappable: phoneIsLink
                };
            }""")

            mobile.close()
            browser.close()

    except Exception as e:
        log.error(f"Capture error for {url}: {e}")
        result["error"] = str(e)

    return jsonify(result)


# ── Phase 3: AI Analysis ────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
def analyze_website():
    """
    Send captured data to Claude for 4-module analysis.
    Input: { "firm": {...}, "capture": {...} }
    Output: { "score": 1-10, "issues": [...], "template_rec": "...", "email_hooks": [...] }
    """
    import anthropic

    firm = request.json.get("firm", {})
    capture = request.json.get("capture", {})

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Build the analysis prompt
    prompt = build_analysis_prompt(firm, capture)

    # Build message with screenshots as images
    content = []

    # Add desktop screenshot
    if capture.get("screenshots", {}).get("desktop"):
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": capture["screenshots"]["desktop"],
            },
        })
        content.append({"type": "text", "text": "Above: Desktop screenshot (1280x900)"})

    # Add mobile screenshot
    if capture.get("screenshots", {}).get("mobile"):
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": capture["screenshots"]["mobile"],
            },
        })
        content.append({"type": "text", "text": "Above: Mobile screenshot (375x812)"})

    # Add the analysis prompt text
    content.append({"type": "text", "text": prompt})

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{"role": "user", "content": content}],
        )

        # Parse JSON from response
        response_text = response.content[0].text
        # Extract JSON from response (handle potential markdown wrapping)
        json_str = response_text
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0]

        analysis = json.loads(json_str.strip())
        analysis["raw_response"] = response_text
        analysis["api_cost"] = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

        return jsonify(analysis)

    except Exception as e:
        log.error(f"Analysis error: {e}")
        return jsonify({"error": str(e)}), 500


def build_analysis_prompt(firm, capture):
    """Build the comprehensive analysis prompt for Claude."""

    scraped = capture.get("scraped", {})
    tech = capture.get("tech", {})
    mobile = tech.get("mobile_checks", {})

    return f"""You are analyzing a CPA firm's website for quality and identifying specific issues that can be used in outreach to sell them a redesign.

FIRM CONTEXT:
- Name: {firm.get('name', 'Unknown')}
- Location: {firm.get('address', 'Unknown')}
- Google Rating: {firm.get('rating', 'N/A')} ({firm.get('review_count', 0)} reviews)
- Website: {capture.get('url', 'Unknown')}

SCRAPED DATA:
- Page Title: {scraped.get('title', '')}
- Meta Description: {scraped.get('meta_description', 'MISSING')}
- H1 Tags: {json.dumps(scraped.get('h1s', []))}
- H2 Tags: {json.dumps(scraped.get('h2s', [])[:5])}
- Has Contact Form: {bool(scraped.get('forms', []))}
- Phone Links (tel:): {json.dumps(scraped.get('phone_links', []))}
- Email Links: {json.dumps(scraped.get('email_links', []))}
- Schema Markup: {'Present' if scraped.get('schema') else 'MISSING'}
- Copyright Year: {scraped.get('copyright_text', 'Not found')}
- Body Text (first 2000 chars): {scraped.get('body_text', '')[:2000]}

TECH SIGNALS:
- SSL/HTTPS: {tech.get('has_ssl', False)}
- Viewport Meta: {tech.get('has_viewport_meta', False)}
- Analytics: {tech.get('has_analytics', False)}
- CMS: {tech.get('cms_hints', 'unknown')}
- Load Time: {tech.get('load_time_ms', 0)}ms

MOBILE CHECKS:
- Viewport Tag: {mobile.get('viewport_present', False)}
- Horizontal Overflow: {mobile.get('horizontal_overflow', False)}
- Small Tap Targets: {mobile.get('small_tap_targets', 0)} of {mobile.get('total_tap_targets', 0)}
- Mobile Navigation: {mobile.get('has_mobile_nav', False)}
- Phone Tappable: {mobile.get('phone_is_tappable', False)}

INSTRUCTIONS:
Analyze the website across 4 dimensions and provide your response as a JSON object with this exact structure:

{{
  "composite_score": <1-10 float>,
  "visual_design": {{
    "score": <1-10>,
    "issues": ["issue1", "issue2", ...],
    "outreach_hooks": ["hook1", "hook2"]
  }},
  "seo_health": {{
    "score": <1-10>,
    "issues": ["issue1", "issue2", ...],
    "outreach_hooks": ["hook1", "hook2"]
  }},
  "mobile_quality": {{
    "score": <1-10>,
    "issues": ["issue1", "issue2", ...],
    "outreach_hooks": ["hook1", "hook2"]
  }},
  "content_quality": {{
    "score": <1-10>,
    "issues": ["issue1", "issue2", ...],
    "outreach_hooks": ["hook1", "hook2"]
  }},
  "top_3_hooks": [
    "Most compelling outreach hook with specific data",
    "Second most compelling hook",
    "Third most compelling hook"
  ],
  "template_recommendation": {{
    "archetype": "small_local_practice",
    "template": "neighbours|cornerstone|honest|local_roots|trusted_advisor",
    "reasoning": "Why this template fits this firm"
  }},
  "firm_personality": "brief description of the firm's apparent style and positioning"
}}

SCORING GUIDE:
- 1-3: Severely outdated, major issues across all dimensions
- 4-5: Below average, multiple significant problems
- 6-7: Acceptable but room for improvement (likely skip for outreach)
- 8-10: Modern, professional, well-optimized (definitely skip)

TEMPLATE MATCHING:
- neighbours: Warm, family-oriented firms emphasizing community and trust
- cornerstone: Established, prestigious firms with 20+ year track record
- honest: Modern, direct, no-nonsense firms that value transparency
- local_roots: Community-grounded firms deeply tied to their local area
- trusted_advisor: Premium advisory firms serving high-net-worth clients

Respond ONLY with the JSON object. No markdown, no explanation."""


# ── Phase 4: Email Draft ────────────────────────────────────────────

@app.route("/api/draft-email", methods=["POST"])
def draft_email():
    """
    Draft personalized outreach email based on analysis.
    Input: { "firm": {...}, "analysis": {...}, "showcase_url": "..." }
    Output: { "subject": "...", "body": "...", "preview_text": "..." }
    """
    import anthropic

    firm = request.json.get("firm", {})
    analysis = request.json.get("analysis", {})
    showcase_url = request.json.get("showcase_url", "https://centric.design")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    template_rec = analysis.get("template_recommendation", {})
    top_hooks = analysis.get("top_3_hooks", [])

    prompt = f"""Draft a cold outreach email to a CPA firm owner about redesigning their website.

FIRM: {firm.get('name')} in {firm.get('address')}
SCORE: {analysis.get('composite_score')}/10
TOP 3 ISSUES FOUND:
1. {top_hooks[0] if len(top_hooks) > 0 else 'N/A'}
2. {top_hooks[1] if len(top_hooks) > 1 else 'N/A'}
3. {top_hooks[2] if len(top_hooks) > 2 else 'N/A'}

RECOMMENDED TEMPLATE: {template_rec.get('template', 'neighbours')}
TEMPLATE REASONING: {template_rec.get('reasoning', '')}
SHOWCASE URL: {showcase_url}/templates/{template_rec.get('template', 'neighbours').replace('_', '-')}.html

RULES:
- Subject line: specific, not salesy, references their firm by name
- Opening: compliment something genuine about their practice
- Body: cite 2-3 specific website issues with data points
- CTA: soft ask, link to the recommended template as "here's what your site could look like"
- Tone: professional peer, not pushy vendor
- Length: 150-200 words max
- Sign as "Timur" from Centric

Respond as JSON:
{{
  "subject": "...",
  "preview_text": "First 80 chars that appear in inbox preview",
  "body": "Full email body in plain text with line breaks as \\n"
}}

Respond ONLY with JSON."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = response.content[0].text
        json_str = response_text
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0]

        email = json.loads(json_str.strip())
        return jsonify(email)

    except Exception as e:
        log.error(f"Email draft error: {e}")
        return jsonify({"error": str(e)}), 500


# ── Phase 5: Telegram Approval ──────────────────────────────────────

@app.route("/api/telegram/send-approval", methods=["POST"])
def send_telegram_approval():
    """
    Send an approval request to Telegram with APPROVE/SKIP/EDIT buttons.
    Input: { "firm": {...}, "analysis": {...}, "email": {...} }
    """
    import requests as req

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    firm = request.json.get("firm", {})
    analysis = request.json.get("analysis", {})
    email = request.json.get("email", {})

    # Create a unique ID for this approval
    approval_id = hashlib.md5(
        f"{firm.get('place_id', '')}{datetime.now().isoformat()}".encode()
    ).hexdigest()[:12]

    message = f"""🏢 *{firm.get('name', 'Unknown Firm')}*
📍 {firm.get('address', '')}
⭐ {firm.get('rating', 'N/A')} ({firm.get('review_count', 0)} reviews)
🌐 {firm.get('website', 'No website')}

📊 Score: *{analysis.get('composite_score', 'N/A')}/10*
🎯 Template: *{analysis.get('template_recommendation', {}).get('template', 'N/A')}*

📧 *Email Subject:* {email.get('subject', '')}

📝 *Email Preview:*
{email.get('body', '')[:300]}...

Top Issues:
{chr(10).join(f'• {h}' for h in analysis.get('top_3_hooks', [])[:3])}"""

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ APPROVE", "callback_data": f"approve_{approval_id}"},
                {"text": "⏭ SKIP", "callback_data": f"skip_{approval_id}"},
                {"text": "✏️ EDIT", "callback_data": f"edit_{approval_id}"},
            ]
        ]
    }

    try:
        resp = req.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            },
        )
        return jsonify({"sent": True, "approval_id": approval_id, "telegram_response": resp.json()})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """
    Receive callback from Telegram buttons.
    n8n listens on this webhook to trigger the next phase.
    """
    data = request.json
    callback = data.get("callback_query", {})
    action_data = callback.get("data", "")

    if not action_data:
        return jsonify({"ok": True})

    parts = action_data.split("_", 1)
    action = parts[0]  # approve, skip, or edit
    approval_id = parts[1] if len(parts) > 1 else ""

    log.info(f"Telegram callback: {action} for {approval_id}")

    return jsonify({
        "action": action,
        "approval_id": approval_id,
        "timestamp": datetime.now().isoformat(),
    })


# ── Phase 6: Send Email ─────────────────────────────────────────────

@app.route("/api/send-email", methods=["POST"])
def send_email():
    """
    Send the approved email via SendGrid.
    Input: { "to_email": "...", "to_name": "...", "subject": "...", "body": "...", "from_email": "...", "from_name": "..." }
    """
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    data = request.json
    sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])

    message = Mail(
        from_email=(data.get("from_email", "hello@centric.design"), data.get("from_name", "Timur from Centric")),
        to_emails=data["to_email"],
        subject=data["subject"],
        plain_text_content=data["body"],
    )

    try:
        response = sg.send(message)
        return jsonify({
            "sent": True,
            "status_code": response.status_code,
            "to": data["to_email"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Health Check ────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "centric-automation", "timestamp": datetime.now().isoformat()})


# ── Key Validation ──────────────────────────────────────────────────

@app.route("/api/test-keys", methods=["GET"])
def test_keys():
    """Verify all API keys are configured."""
    keys = {
        "GOOGLE_PLACES_API_KEY": bool(os.environ.get("GOOGLE_PLACES_API_KEY")),
        "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "SENDGRID_API_KEY": bool(os.environ.get("SENDGRID_API_KEY")),
        "TELEGRAM_BOT_TOKEN": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "TELEGRAM_CHAT_ID": bool(os.environ.get("TELEGRAM_CHAT_ID")),
    }
    all_set = all(keys.values())
    return jsonify({"all_keys_set": all_set, "keys": keys})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("DEBUG", "false").lower() == "true")
