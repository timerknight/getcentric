# Centric Automation Server

Flask API server that powers the automated CPA firm discovery and outreach pipeline. Designed to be deployed on Railway and orchestrated by n8n workflows.

## Architecture

```
n8n (orchestrator)
  ├── Workflow 1: Discovery    → /api/discover → /api/filter → /api/capture
  ├── Workflow 2: Analysis     → /api/analyze → /api/draft-email → /api/telegram/send-approval
  └── Workflow 3: Outreach     → /api/send-email → Airtable CRM logging
```

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check |
| `/api/test-keys` | GET | Verify all API keys are configured |
| `/api/discover` | POST | Find CPA firms via Google Places API |
| `/api/filter` | POST | Apply category, size, and chain filters |
| `/api/capture` | POST | Screenshot + scrape a website via Playwright |
| `/api/analyze` | POST | 4-module AI analysis via Claude |
| `/api/draft-email` | POST | Draft personalized outreach email |
| `/api/telegram/send-approval` | POST | Send approval request to Telegram |
| `/api/telegram/webhook` | POST | Receive Telegram button callbacks |
| `/api/send-email` | POST | Send approved email via SendGrid |

## Quick Start

1. Clone this repo
2. Copy `.env.example` to `.env` and fill in your API keys
3. `pip install -r requirements.txt`
4. `playwright install chromium`
5. `python server.py`
6. Hit `http://localhost:8080/api/test-keys` to verify

## Deploy to Railway

1. Push to GitHub
2. Connect Railway to the repo
3. Add env vars from `.env.example`
4. Railway auto-deploys via Dockerfile

## n8n Setup

See `N8N_SETUP_GUIDE.md` for complete workflow configuration instructions.

## Cost

~$74/month to process 500 firms/month (all APIs + hosting + n8n).
