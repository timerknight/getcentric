# Centric n8n Workflow Setup Guide

## Overview

You need 3 n8n workflows that run in sequence:

1. **Discovery** (weekly cron) → finds CPA firms, filters, captures screenshots
2. **Analysis** (triggered by discovery) → AI scoring, email drafting, Telegram approval
3. **Outreach** (triggered by Telegram) → sends email, schedules follow-ups, logs to CRM

All workflows call your Flask API server deployed on Railway.

---

## Prerequisites

1. Deploy the Flask server to Railway:
   - Push the `automation/` folder to a GitHub repo
   - Connect Railway to the repo
   - Add all env vars from `.env.example`
   - Railway auto-detects the Dockerfile and deploys
   - Note your Railway URL (e.g., `https://centric-api.up.railway.app`)

2. Create an Airtable base called "Centric CRM" with these tables:
   - **Firms**: place_id, name, address, phone, website, rating, review_count, status, score, template_rec, email_subject, email_body, created_at, contacted_at
   - **Outreach Log**: firm_id, email_sent_at, follow_up_1_at, follow_up_2_at, response, response_date

3. Create a Telegram bot via @BotFather, get the token, and get your chat_id

---

## Workflow 1: Discovery (runs weekly)

### Node 1: Schedule Trigger
- Type: Schedule Trigger
- Cron: `0 9 * * 1` (every Monday at 9 AM)

### Node 2: Set Territory
- Type: Set
- Values:
  ```
  city = "Sacramento"
  state = "CA"
  zip_codes = ["95814","95815","95816","95817","95818","95819","95820","95821","95822","95823","95824","95825"]
  ```

### Node 3: Discover Firms
- Type: HTTP Request
- Method: POST
- URL: `{{$env.API_URL}}/api/discover`
- Body (JSON):
  ```json
  {
    "city": "{{$json.city}}",
    "state": "{{$json.state}}",
    "zip_codes": {{$json.zip_codes}}
  }
  ```
- Timeout: 120 seconds

### Node 4: Filter Firms
- Type: HTTP Request
- Method: POST
- URL: `{{$env.API_URL}}/api/filter`
- Body: `{ "firms": {{$json.firms}} }`

### Node 5: Check CRM for Duplicates
- Type: Airtable (Search)
- Table: Firms
- For each qualified firm, check if place_id already exists
- Filter out duplicates

### Node 6: Split Into Items
- Type: Split In Batches
- Batch Size: 1
- Input: qualified firms not in CRM

### Node 7: Capture Website
- Type: HTTP Request
- Method: POST
- URL: `{{$env.API_URL}}/api/capture`
- Body:
  ```json
  {
    "url": "{{$json.website}}",
    "firm_name": "{{$json.name}}"
  }
  ```
- Timeout: 60 seconds

### Node 8: Save to Airtable
- Type: Airtable (Create)
- Table: Firms
- Map: place_id, name, address, phone, website, rating, review_count
- Set status = "captured"

### Node 9: Trigger Analysis Workflow
- Type: Execute Workflow
- Pass firm data + capture data to Workflow 2

---

## Workflow 2: Analysis + Email Draft + Approval

### Node 1: Workflow Trigger
- Type: Execute Workflow Trigger
- Receives: firm data + capture data from Workflow 1

### Node 2: AI Analysis
- Type: HTTP Request
- Method: POST
- URL: `{{$env.API_URL}}/api/analyze`
- Body:
  ```json
  {
    "firm": {{$json.firm}},
    "capture": {{$json.capture}}
  }
  ```
- Timeout: 60 seconds

### Node 3: Score Gate
- Type: IF
- Condition: `{{$json.composite_score}}` < 6
- True → continue to email draft
- False → update Airtable status to "skipped_good_site"

### Node 4: Draft Email (True branch)
- Type: HTTP Request
- Method: POST
- URL: `{{$env.API_URL}}/api/draft-email`
- Body:
  ```json
  {
    "firm": {{$json.firm}},
    "analysis": {{$json.analysis}},
    "showcase_url": "https://centric.design"
  }
  ```

### Node 5: Update Airtable
- Type: Airtable (Update)
- Table: Firms
- Update: score, template_rec, email_subject, email_body, status = "pending_approval"

### Node 6: Send to Telegram
- Type: HTTP Request
- Method: POST
- URL: `{{$env.API_URL}}/api/telegram/send-approval`
- Body:
  ```json
  {
    "firm": {{$json.firm}},
    "analysis": {{$json.analysis}},
    "email": {{$json.email}}
  }
  ```

---

## Workflow 3: Outreach (triggered by Telegram)

### Node 1: Webhook Trigger
- Type: Webhook
- Method: POST
- Path: `/telegram-callback`
- n8n registers this URL; set it as the Telegram bot webhook:
  `https://api.telegram.org/bot<TOKEN>/setWebhook?url=<N8N_WEBHOOK_URL>`

### Node 2: Parse Action
- Type: Set
- Extract: action, approval_id from callback_query.data

### Node 3: Route by Action
- Type: Switch
- Routes:
  - "approve" → Node 4 (Send Email)
  - "skip" → Node 7 (Update CRM as skipped)
  - "edit" → Node 8 (Reply with "send edited version")

### Node 4: Send Email (approve branch)
- Type: HTTP Request
- Method: POST
- URL: `{{$env.API_URL}}/api/send-email`
- Body:
  ```json
  {
    "to_email": "{{$json.firm_email}}",
    "to_name": "{{$json.firm_name}}",
    "subject": "{{$json.email_subject}}",
    "body": "{{$json.email_body}}",
    "from_email": "hello@centric.design",
    "from_name": "Timur from Centric"
  }
  ```

### Node 5: Log to Airtable
- Type: Airtable (Update)
- Table: Firms → status = "contacted", contacted_at = now
- Table: Outreach Log → Create record with email_sent_at = now

### Node 6: Schedule Follow-ups
- Type: Wait
- Wait: 3 days
- Then: Send follow-up email #1 (shorter version citing same issues)
- Wait: 4 more days
- Then: Send follow-up email #2 (final touch, different angle)

### Node 7: Skip (skip branch)
- Type: Airtable (Update)
- Status = "skipped_manual"

---

## Environment Variables for n8n

Set these in n8n's Settings → Variables:

```
API_URL = https://your-railway-app.up.railway.app
AIRTABLE_API_KEY = your_airtable_key
AIRTABLE_BASE_ID = your_base_id
```

---

## Testing Sequence

1. Deploy Flask server → hit `/health` to confirm it's running
2. Hit `/api/test-keys` to verify all API keys are configured
3. Run Workflow 1 with a single zip code to test discovery
4. Manually trigger Workflow 2 with one firm to test analysis
5. Check Telegram for the approval message
6. Click APPROVE to test the full send flow
7. Verify the email arrived and Airtable was updated

---

## Cost Estimates (500 firms/month)

| Service | Monthly Cost |
|---------|-------------|
| Railway (Flask server) | $5 |
| Google Places API | $15 |
| Claude API (analysis + email) | $10 |
| SendGrid | $20 |
| Airtable | $0 (free tier) |
| n8n | $24 (your subscription) |
| Telegram | $0 |
| **Total** | **~$74/month** |
