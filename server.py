"""
Centric Automation Server
Flask API that n8n workflows call for each pipeline phase.
Uses Google Gemini for AI analysis and email drafting.
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


# -- Phase 1: Discovery --

@app.route("/api/discover", methods=["POST"])
def discover_firms():
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
    firms_with_details = []
    for pid, firm in all_firms.items():
        try:
            detail_url = "https://maps.googleapis.com/maps/api/place/details/json"
            detail_params = {"place_id": pid, "fields": "website,formatted_phone_number,opening_hours,url", "key": api_key}
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
    return jsonify({"total_found": len(firms_with_details), "firms": firms_with_details, "search_params": {"city": city, "state": state, "zip_count": len(zip_codes)}})


@app.route("/api/filter", methods=["POST"])
def filter_firms():
    firms = request.json.get("firms", [])
    skip_keywords = ["h&r block", "jackson hewitt", "liberty tax", "turbotax", "deloitte", "kpmg", "ernst & young", "pricewaterhousecoopers", "pwc", "ey"]
    qualified, no_website, skipped = [], [], []
    for firm in firms:
        name_lower = firm.get("name", "").lower()
        if any(kw in name_lower for kw in skip_keywords):
            firm["skip_reason"] = "chain_or_big4"
            skipped.append(firm)
        elif firm.get("review_count", 0) > 200:
            firm["skip_reason"] = "likely_large_firm"
            skipped.append(firm)
        elif not firm.get("website"):
            no_website.append(firm)
        else:
            qualified.append(firm)
    return jsonify({"qualified": qualified, "no_website": no_website, "skipped": skipped, "counts": {"qualified": len(qualified), "no_website": len(no_website), "skipped": len(skipped)}})


# -- Phase 2: Capture --

@app.route("/api/capture", methods=["POST"])
def capture_website():
    from playwright.sync_api import sync_playwright
    url = request.json.get("url")
    if not url:
        return jsonify({"error": "url required"}), 400
    result = {"screenshots": {}, "scraped": {}, "tech": {}, "url": url}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            desktop = browser.new_page(viewport={"width": 1280, "height": 900})
            desktop.goto(url, timeout=30000, wait_until="networkidle")
            result["screenshots"]["desktop"] = base64.b64encode(desktop.screenshot(type="jpeg", quality=80)).decode()
            result["screenshots"]["fullpage"] = base64.b64encode(desktop.screenshot(type="jpeg", quality=60, full_page=True)).decode()
            result["scraped"] = desktop.evaluate("""() => {
                const getAll = (sel) => [...document.querySelectorAll(sel)].map(e => e.textContent.trim()).filter(Boolean);
                const getMeta = (name) => { const el = document.querySelector('meta[name="'+name+'"], meta[property="'+name+'"]'); return el ? el.getAttribute('content') : ''; };
                return {
                    title: document.title || '', meta_description: getMeta('description'),
                    h1s: getAll('h1'), h2s: getAll('h2'), h3s: getAll('h3'),
                    body_text: document.body?.innerText?.substring(0, 5000) || '',
                    images: [...document.querySelectorAll('img')].map(img => ({src: img.src, alt: img.alt || ''})).slice(0, 30),
                    forms: [...document.querySelectorAll('form')].map(f => ({action: f.action, fields: [...f.querySelectorAll('input,textarea,select')].map(i => i.name || i.type)})),
                    phone_links: [...document.querySelectorAll('a[href^="tel:"]')].map(a => a.href),
                    email_links: [...document.querySelectorAll('a[href^="mailto:"]')].map(a => a.href.replace('mailto:', '')),
                    schema: (() => { const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')]; return scripts.map(s => { try { return JSON.parse(s.textContent); } catch { return null; } }).filter(Boolean); })(),
                    copyright_text: (() => { const m = (document.body?.innerText || '').match(/©\\s*(\\d{4})/); return m ? m[1] : ''; })()
                };
            }""")
            result["tech"] = desktop.evaluate("""() => {
                const has = (sel) => !!document.querySelector(sel);
                return {
                    has_viewport_meta: has('meta[name="viewport"]'), has_analytics: has('script[src*="googletagmanager"], script[src*="google-analytics"]'),
                    has_ssl: location.protocol === 'https:',
                    cms_hints: (() => { const g = document.querySelector('meta[name="generator"]'); if (g) return g.content; if (document.querySelector('link[href*="wp-content"]')) return 'WordPress'; if (document.querySelector('meta[name="wix-dynamic-custom-elements"]')) return 'Wix'; return 'unknown'; })()
                };
            }""")
            timing = desktop.evaluate("() => { const t = performance.timing; return { load_time_ms: t.loadEventEnd - t.navigationStart }; }")
            result["tech"]["load_time_ms"] = timing.get("load_time_ms", 0)
            desktop.close()
            mobile = browser.new_page(viewport={"width": 375, "height": 812}, user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)")
            mobile.goto(url, timeout=30000, wait_until="networkidle")
            result["screenshots"]["mobile"] = base64.b64encode(mobile.screenshot(type="jpeg", quality=80)).decode()
            result["tech"]["mobile_checks"] = mobile.evaluate("""() => {
                const vp = document.querySelector('meta[name="viewport"]');
                const overflow = document.body.scrollWidth > window.innerWidth;
                const smTaps = [...document.querySelectorAll('a, button, input')].filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (r.width < 48 || r.height < 48); }).length;
                const totalTaps = document.querySelectorAll('a, button, input').length;
                return { viewport_present: !!vp, horizontal_overflow: overflow, small_tap_targets: smTaps, total_tap_targets: totalTaps, has_mobile_nav: !!document.querySelector('[class*="hamburger"], [class*="mobile-menu"], [class*="nav-toggle"], [aria-label*="menu"]'), phone_is_tappable: !!document.querySelector('a[href^="tel:"]') };
            }""")
            mobile.close()
            browser.close()
    except Exception as e:
        log.error(f"Capture error for {url}: {e}")
        result["error"] = str(e)
    return jsonify(result)


# -- Helper: fix newlines inside JSON strings --

def fix_json_strings(text):
    """Walk through text char by char and escape literal newlines inside JSON string values."""
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string and ch == '\n':
            result.append('\\n')
            continue
        if in_string and ch == '\r':
            continue
        if in_string and ch == '\t':
            result.append('\\t')
            continue
        result.append(ch)
    return ''.join(result)


# -- Phase 3: AI Analysis (Gemini) --

@app.route("/api/analyze", methods=["POST"])
def analyze_website():
    import google.generativeai as genai
    firm = request.json.get("firm", {})
    capture = request.json.get("capture", {})
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = build_analysis_prompt(firm, capture)
    parts = []
    if capture.get("screenshots", {}).get("desktop"):
        parts.append({"mime_type": "image/jpeg", "data": base64.b64decode(capture["screenshots"]["desktop"])})
        parts.append("Above: Desktop screenshot (1280x900)")
    if capture.get("screenshots", {}).get("mobile"):
        parts.append({"mime_type": "image/jpeg", "data": base64.b64decode(capture["screenshots"]["mobile"])})
        parts.append("Above: Mobile screenshot (375x812)")
    parts.append(prompt)
    try:
        response = model.generate_content(parts, generation_config=genai.types.GenerationConfig(temperature=0.3, max_output_tokens=4000, response_mime_type="application/json"))
        clean = fix_json_strings(response.text)
        analysis = json.loads(clean)
        analysis["model"] = "gemini-2.5-flash"
        return jsonify(analysis)
    except Exception as e:
        log.error(f"Analysis error: {e}")
        return jsonify({"error": str(e)}), 500


def build_analysis_prompt(firm, capture):
    scraped = capture.get("scraped", {})
    tech = capture.get("tech", {})
    mobile = tech.get("mobile_checks", {})
    return f"""You are analyzing a CPA firm's website for quality and identifying specific issues for outreach.

FIRM: {firm.get('name', 'Unknown')} | {firm.get('address', 'Unknown')} | Rating: {firm.get('rating', 'N/A')} ({firm.get('review_count', 0)} reviews) | {capture.get('url', '')}

SCRAPED: Title: {scraped.get('title', '')} | Meta: {scraped.get('meta_description', 'MISSING')} | H1: {json.dumps(scraped.get('h1s', []))} | H2: {json.dumps(scraped.get('h2s', [])[:5])} | Forms: {bool(scraped.get('forms', []))} | tel: links: {json.dumps(scraped.get('phone_links', []))} | Schema: {'Yes' if scraped.get('schema') else 'MISSING'} | Copyright: {scraped.get('copyright_text', '?')}
Body: {scraped.get('body_text', '')[:2000]}

TECH: SSL: {tech.get('has_ssl', False)} | Viewport: {tech.get('has_viewport_meta', False)} | Analytics: {tech.get('has_analytics', False)} | CMS: {tech.get('cms_hints', '?')} | Load: {tech.get('load_time_ms', 0)}ms

MOBILE: Viewport: {mobile.get('viewport_present', False)} | Overflow: {mobile.get('horizontal_overflow', False)} | Small taps: {mobile.get('small_tap_targets', 0)}/{mobile.get('total_tap_targets', 0)} | Mobile nav: {mobile.get('has_mobile_nav', False)} | Phone tappable: {mobile.get('phone_is_tappable', False)}

Return ONLY a JSON object (no markdown fences):
{{"composite_score": <1-10>, "visual_design": {{"score": <1-10>, "issues": [...], "outreach_hooks": [...]}}, "seo_health": {{"score": <1-10>, "issues": [...], "outreach_hooks": [...]}}, "mobile_quality": {{"score": <1-10>, "issues": [...], "outreach_hooks": [...]}}, "content_quality": {{"score": <1-10>, "issues": [...], "outreach_hooks": [...]}}, "top_3_hooks": ["hook1", "hook2", "hook3"], "template_recommendation": {{"archetype": "small_local_practice", "template": "neighbours|cornerstone|honest|local_roots|trusted_advisor", "reasoning": "why"}}, "firm_personality": "brief description"}}

Scoring: 1-3 severely outdated, 4-5 below average, 6-7 acceptable (skip), 8-10 modern (skip).
Templates: neighbours=warm/family, cornerstone=established/prestigious, honest=modern/direct, local_roots=community-grounded, trusted_advisor=premium/high-net-worth."""


# -- Phase 4: Email Draft (Gemini) --

@app.route("/api/draft-email", methods=["POST"])
def draft_email():
    import google.generativeai as genai
    firm = request.json.get("firm", {})
    analysis = request.json.get("analysis", {})
    showcase_url = request.json.get("showcase_url", "https://getcentric.design")
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-2.5-flash")
    template_rec = analysis.get("template_recommendation", {})
    top_hooks = analysis.get("top_3_hooks", [])
    prompt = f"""Draft a cold outreach email to a CPA firm owner about redesigning their website.

FIRM: {firm.get('name')} in {firm.get('address')}
SCORE: {analysis.get('composite_score')}/10
ISSUES: 1. {top_hooks[0] if len(top_hooks) > 0 else 'N/A'} 2. {top_hooks[1] if len(top_hooks) > 1 else 'N/A'} 3. {top_hooks[2] if len(top_hooks) > 2 else 'N/A'}
TEMPLATE: {template_rec.get('template', 'neighbours')}
URL: {showcase_url}/templates/{template_rec.get('template', 'neighbours').replace('_', '-')}.html

Rules: Subject references firm name. Opening compliments their practice. Body cites 2-3 specific issues. CTA links to template. Tone: professional peer. 150-200 words max. Sign as Timur from Centric.

Return ONLY JSON. CRITICAL: In the body field use [BR] where you want line breaks. Do NOT use actual line breaks inside any string value.
{{"subject": "subject here", "preview_text": "preview here", "body": "First paragraph[BR][BR]Second paragraph[BR][BR]Best,[BR]Timur"}}"""

try:
        response = model.generate_content(prompt, generation_config=genai.types.GenerationConfig(temperature=0.4, max_output_tokens=1000, response_mime_type="application/json"))
        raw = response.text
        # Try normal JSON parse first
        try:
            clean = raw.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
            email = json.loads(clean)
        except json.JSONDecodeError:
            # Fallback: extract fields with regex
            import re
            subject_m = re.search(r'"subject"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
            preview_m = re.search(r'"preview_text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
            body_m = re.search(r'"body"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
            email = {
                "subject": subject_m.group(1) if subject_m else "Website redesign for your firm",
                "preview_text": preview_m.group(1) if preview_m else "I noticed some issues with your website",
                "body": body_m.group(1) if body_m else raw[:500],
            }
            log.info("Used regex fallback for email parsing")
        if 'body' in email:
            email['body'] = email['body'].replace('[BR]', '\n').replace('\\n', '\n')
        return jsonify(email)
    except Exception as e:
        log.error(f"Email draft error: {e}")
        return jsonify({"error": str(e)}), 500


# -- Phase 5: Telegram Approval --

@app.route("/api/telegram/send-approval", methods=["POST"])
def send_telegram_approval():
    import requests as req
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    firm = request.json.get("firm", {})
    analysis = request.json.get("analysis", {})
    email = request.json.get("email", {})
    approval_id = hashlib.md5(f"{firm.get('place_id', '')}{datetime.now().isoformat()}".encode()).hexdigest()[:12]
    message = f"""🏢 *{firm.get('name', 'Unknown Firm')}*
📍 {firm.get('address', '')}
⭐ {firm.get('rating', 'N/A')} ({firm.get('review_count', 0)} reviews)
🌐 {firm.get('website', 'No website')}

📊 Score: *{analysis.get('composite_score', 'N/A')}/10*
🎯 Template: *{analysis.get('template_recommendation', {}).get('template', 'N/A')}*

📧 *Subject:* {email.get('subject', '')}

📝 *Preview:*
{email.get('body', '')[:300]}...

Top Issues:
{chr(10).join(f'• {h}' for h in analysis.get('top_3_hooks', [])[:3])}"""
    keyboard = {"inline_keyboard": [[{"text": "✅ APPROVE", "callback_data": f"approve_{approval_id}"}, {"text": "⏭ SKIP", "callback_data": f"skip_{approval_id}"}, {"text": "✏️ EDIT", "callback_data": f"edit_{approval_id}"}]]}
    try:
        resp = req.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown", "reply_markup": keyboard})
        return jsonify({"sent": True, "approval_id": approval_id, "telegram_response": resp.json()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/telegram/webhook", methods=["POST"])
def telegram_webhook():
    data = request.json
    callback = data.get("callback_query", {})
    action_data = callback.get("data", "")
    if not action_data:
        return jsonify({"ok": True})
    parts = action_data.split("_", 1)
    action = parts[0]
    approval_id = parts[1] if len(parts) > 1 else ""
    log.info(f"Telegram callback: {action} for {approval_id}")
    return jsonify({"action": action, "approval_id": approval_id, "timestamp": datetime.now().isoformat()})


# -- Phase 6: Send Email --

@app.route("/api/send-email", methods=["POST"])
def send_email():
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
    data = request.json
    sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
    message = Mail(from_email=(data.get("from_email", "hello@getcentric.design"), data.get("from_name", "Timur from Centric")), to_emails=data["to_email"], subject=data["subject"], plain_text_content=data["body"])
    try:
        response = sg.send(message)
        return jsonify({"sent": True, "status_code": response.status_code, "to": data["to_email"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "centric-automation", "timestamp": datetime.now().isoformat()})


@app.route("/api/test-keys", methods=["GET"])
def test_keys():
    keys = {
        "GOOGLE_PLACES_API_KEY": bool(os.environ.get("GOOGLE_PLACES_API_KEY")),
        "GEMINI_API_KEY": bool(os.environ.get("GEMINI_API_KEY")),
        "SENDGRID_API_KEY": bool(os.environ.get("SENDGRID_API_KEY")),
        "TELEGRAM_BOT_TOKEN": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "TELEGRAM_CHAT_ID": bool(os.environ.get("TELEGRAM_CHAT_ID")),
    }
    return jsonify({"all_keys_set": all(keys.values()), "keys": keys})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("DEBUG", "false").lower() == "true")
