"""
Centric Automation Server
Flask API that n8n workflows call for each pipeline phase.
Uses Google Gemini for AI analysis and email drafting.
"""

import os
import re
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

# Simple in-memory set of contacted firm place_ids (persists until redeploy)
contacted_firms = set()


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
            # Check dedup
            pid = firm.get("place_id", "")
            if pid in contacted_firms:
                firm["skip_reason"] = "already_contacted"
                skipped.append(firm)
            else:
                qualified.append(firm)
    return jsonify({"qualified": qualified, "no_website": no_website, "skipped": skipped, "counts": {"qualified": len(qualified), "no_website": len(no_website), "skipped": len(skipped)}})


# -- Dedup endpoints --

@app.route("/api/mark-contacted", methods=["POST"])
def mark_contacted():
    """Mark a firm as contacted so it won't be processed again."""
    place_id = request.json.get("place_id", "")
    firm_name = request.json.get("firm_name", "")
    if place_id:
        contacted_firms.add(place_id)
        log.info(f"Marked as contacted: {firm_name} ({place_id}). Total: {len(contacted_firms)}")
    return jsonify({"marked": True, "place_id": place_id, "total_contacted": len(contacted_firms)})


@app.route("/api/contacted-list", methods=["GET"])
def contacted_list():
    """View all contacted firm IDs."""
    return jsonify({"contacted": list(contacted_firms), "total": len(contacted_firms)})


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


# -- Helpers --

def safe_parse_json(raw_text):
    text = raw_text
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        fixed = []
        in_str = False
        esc = False
        for ch in text:
            if esc:
                fixed.append(ch)
                esc = False
                continue
            if ch == '\\' and in_str:
                fixed.append(ch)
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                fixed.append(ch)
                continue
            if in_str and ch == '\n':
                fixed.append('\\n')
                continue
            if in_str and ch == '\r':
                continue
            if in_str and ch == '\t':
                fixed.append('\\t')
                continue
            fixed.append(ch)
        return json.loads(''.join(fixed))
    except Exception:
        pass
    try:
        return json.loads(text.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' '))
    except Exception:
        pass
    return None


def extract_analysis_fallback():
    return {
        "composite_score": 4,
        "visual_design": {"score": 4, "issues": ["Unable to parse detailed analysis"], "outreach_hooks": ["Your website could benefit from a modern redesign"]},
        "seo_health": {"score": 4, "issues": ["SEO analysis unavailable"], "outreach_hooks": ["SEO improvements could increase your visibility"]},
        "mobile_quality": {"score": 4, "issues": ["Mobile analysis unavailable"], "outreach_hooks": ["Mobile optimization could improve user experience"]},
        "content_quality": {"score": 4, "issues": ["Content analysis unavailable"], "outreach_hooks": ["Content improvements could attract more clients"]},
        "top_3_hooks": ["Your website could benefit from a modern redesign", "SEO improvements could increase your local visibility", "Mobile optimization would improve the experience for phone users"],
        "template_recommendation": {"archetype": "small_local_practice", "template": "neighbours", "reasoning": "Default recommendation"},
        "firm_personality": "Local CPA practice",
    }


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
        analysis = safe_parse_json(response.text)
        if analysis is None:
            log.warning(f"Analysis JSON parse failed, using fallback. Raw: {response.text[:200]}")
            analysis = extract_analysis_fallback()
        analysis["model"] = "gemini-2.5-flash"
        return jsonify(analysis)
    except Exception as e:
        log.error(f"Analysis error: {e}")
        fallback = extract_analysis_fallback()
        fallback["model"] = "gemini-2.5-flash"
        fallback["parse_error"] = str(e)
        return jsonify(fallback)


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

IMPORTANT: The top_3_hooks must be written in plain business language that a non-technical CPA firm owner would understand. Do NOT use technical jargon like "schema markup", "viewport meta tag", "tap targets", or "SSL certificate". Instead write things like:
- "Your firm doesn't appear in Google's local results map when people search for CPAs nearby"
- "Your website is hard to use on phones -- buttons are too small to tap and text is hard to read"
- "Visitors see a 'Not Secure' warning in their browser when they visit your site"
- "Your site takes over 5 seconds to load -- most visitors leave after 3"
- "There's no way for potential clients to book a consultation or contact you easily"

Return ONLY a JSON object (no markdown fences):
{{"composite_score": <1-10>, "visual_design": {{"score": <1-10>, "issues": [...], "outreach_hooks": [...]}}, "seo_health": {{"score": <1-10>, "issues": [...], "outreach_hooks": [...]}}, "mobile_quality": {{"score": <1-10>, "issues": [...], "outreach_hooks": [...]}}, "content_quality": {{"score": <1-10>, "issues": [...], "outreach_hooks": [...]}}, "top_3_hooks": ["plain language hook 1", "plain language hook 2", "plain language hook 3"], "template_recommendation": {{"archetype": "small_local_practice", "template": "neighbours|cornerstone|honest|local_roots|trusted_advisor", "reasoning": "why"}}, "firm_personality": "brief description"}}

Scoring: 1-3 severely outdated, 4-5 below average, 6-7 acceptable (skip), 8-10 modern (skip).
Templates: neighbours=warm/family, cornerstone=established/prestigious, honest=modern/direct, local_roots=community-grounded, trusted_advisor=premium/high-net-worth."""


# -- Phase 4: Email Draft (Template-based) --

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
    template_name = template_rec.get("template", "neighbours").replace("_", "-")
    template_url = f"{showcase_url}/templates/{template_name}.html"
    firm_name = firm.get("name", "your firm")
    rating = firm.get("rating", "")
    review_count = firm.get("review_count", 0)
    issue_1 = top_hooks[0] if len(top_hooks) > 0 else "Your firm doesn't show up in Google's local results when people search for CPAs nearby"
    issue_2 = top_hooks[1] if len(top_hooks) > 1 else "Your website is difficult to use on mobile phones"
    issue_3 = top_hooks[2] if len(top_hooks) > 2 else "The site design looks dated compared to other firms in your area"

    # Ask Gemini for just a subject line and a one-sentence compliment
    prompt = f"""For a CPA firm called {firm_name} with a {rating}-star Google rating and {review_count} reviews, write two things:
1. A short email subject line (under 60 chars) that references their firm name, is not salesy, sounds like a peer reaching out
2. A one-sentence genuine compliment about their practice based on their rating and reviews

Return ONLY JSON with two fields. No line breaks inside values.
{{"subject": "subject here", "compliment": "one sentence compliment here"}}"""

    try:
        response = model.generate_content(prompt, generation_config=genai.types.GenerationConfig(temperature=0.4, max_output_tokens=200, response_mime_type="application/json"))
        parts = safe_parse_json(response.text)
        if parts is None:
            parts = {}
        subject = parts.get("subject", f"Quick note about {firm_name}'s website")
        compliment = parts.get("compliment", f"Your {rating}-star rating with {review_count} reviews says a lot about the quality of your work.")
    except Exception:
        subject = f"Quick note about {firm_name}'s website"
        compliment = f"Your {rating}-star rating with {review_count} reviews says a lot about the quality of your work."

    if not rating or rating == 0:
        compliment = "It's clear from your online presence that you've built a solid practice."

    body = (
        f"Hi,\n"
        f"\n"
        f"I came across {firm_name} while researching CPA firms in the area -- {compliment}\n"
        f"\n"
        f"I took a quick look at your website and noticed a few things that might be costing you potential clients:\n"
        f"\n"
        f"  -> {issue_1}\n"
        f"  -> {issue_2}\n"
        f"  -> {issue_3}\n"
        f"\n"
        f"I run Centric -- we build modern, SEO-optimized websites exclusively for CPA firms. We don't work with restaurants or dentists. Just accountants. That focus means every template, every page, every CTA is built around how accounting clients actually search and make decisions.\n"
        f"\n"
        f"I actually mocked up what a refreshed version of your site could look like using one of our CPA-specific templates. You can preview it here:\n"
        f"\n"
        f"{template_url}\n"
        f"\n"
        f"No pressure at all -- if you like what you see, I'd love a quick 10-minute call to walk through the specifics. If the timing isn't right, no worries.\n"
        f"\n"
        f"Best,\n"
        f"Temir Gulyayev\n"
        f"Founder, Centric\n"
        f"getcentric.design | hello@getcentric.design"
    )

    preview_text = f"I noticed a few things about {firm_name}'s website that might be worth a look"

    return jsonify({"subject": subject, "preview_text": preview_text, "body": body, "template_url": template_url, "firm_name": firm_name})


# -- Phase 4B: Follow-up Emails --

@app.route("/api/follow-up-1", methods=["POST"])
def follow_up_1():
    """Follow-up #1: sent 3 days after initial email. Short, references original, adds urgency."""
    data = request.json
    firm_name = data.get("firm_name", "your firm")
    template_url = data.get("template_url", "https://getcentric.design")

    subject = f"Following up -- {firm_name}'s website"

    body = (
        f"Hi,\n"
        f"\n"
        f"I sent a note a few days ago about {firm_name}'s website. Wanted to make sure it didn't get buried.\n"
        f"\n"
        f"The quick version: I found a few specific issues that are likely costing you visibility on Google and making it harder for potential clients to reach you on their phones.\n"
        f"\n"
        f"I put together a preview of what a refreshed site could look like for your firm:\n"
        f"\n"
        f"{template_url}\n"
        f"\n"
        f"Happy to walk you through it in 10 minutes -- or just take a look and let me know what you think.\n"
        f"\n"
        f"Best,\n"
        f"Temir Gulyayev\n"
        f"Founder, Centric\n"
        f"getcentric.design | hello@getcentric.design"
    )

    return jsonify({"subject": subject, "body": body})


@app.route("/api/follow-up-2", methods=["POST"])
def follow_up_2():
    """Follow-up #2: sent 7 days after initial email. Final touch, different angle, social proof."""
    data = request.json
    firm_name = data.get("firm_name", "your firm")
    template_url = data.get("template_url", "https://getcentric.design")

    subject = f"Last note -- {firm_name}"

    body = (
        f"Hi,\n"
        f"\n"
        f"This is my last follow-up -- I know you're busy running a practice.\n"
        f"\n"
        f"One thing I didn't mention: over 50% of people searching for a CPA now do it from their phone. If your site doesn't work well on mobile, those potential clients are going to a competitor who shows up first on Google.\n"
        f"\n"
        f"We've helped other small CPA firms fix exactly this. The preview I built for {firm_name} is still live here:\n"
        f"\n"
        f"{template_url}\n"
        f"\n"
        f"If the timing isn't right, no worries at all. But if you're curious, I'm happy to show you a version customized for your firm -- takes 5 minutes on a call.\n"
        f"\n"
        f"All the best,\n"
        f"Temir Gulyayev\n"
        f"Founder, Centric\n"
        f"getcentric.design | hello@getcentric.design"
    )

    return jsonify({"subject": subject, "body": body})


# -- Phase 5: Telegram Approval --

@app.route("/api/telegram/send-approval", methods=["POST"])
def send_telegram_approval():
    import requests as req
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
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
        tg_result = resp.json()
        if not tg_result.get("ok"):
            log.warning(f"Telegram Markdown failed: {tg_result}, retrying plain text")
            message_plain = message.replace('*', '').replace('_', '')
            resp = req.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": message_plain, "reply_markup": keyboard})
            tg_result = resp.json()
        return jsonify({"sent": tg_result.get("ok", False), "approval_id": approval_id, "telegram_response": tg_result})
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
    from sendgrid.helpers.mail import Mail, Content
    data = request.json
    sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
    body_text = data.get("body", "")
    body_html = body_text.replace('\n', '<br>\n')
    message = Mail(
        from_email=(data.get("from_email", "hello@getcentric.design"), data.get("from_name", "Temir from Centric")),
        to_emails=data["to_email"],
        subject=data["subject"],
    )
    message.add_content(Content("text/plain", body_text))
    message.add_content(Content("text/html", body_html))
    try:
        response = sg.send(message)
        # Mark firm as contacted after successful send
        place_id = data.get("place_id", "")
        if place_id:
            contacted_firms.add(place_id)
            log.info(f"Email sent and firm marked as contacted: {place_id}")
        return jsonify({"sent": True, "status_code": response.status_code, "to": data["to_email"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -- Test & Health --

@app.route("/api/test-telegram", methods=["GET"])
def test_telegram():
    import requests as req
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    resp = req.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": "Test from Centric server"})
    return jsonify({"token_length": len(bot_token), "chat_id": chat_id, "telegram_response": resp.json()})


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
