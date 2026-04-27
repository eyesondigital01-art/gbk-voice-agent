# GBK Group AI Voice Agent — Deployment Guide

## Deploy on Railway (10 minutes)

### Step 1 — Create GitHub Repo
```
1. Go to github.com → New Repository
2. Name: gbk-voice-agent
3. Private repo
4. Upload these files:
   - server.py
   - requirements.txt
   - Procfile
   - railway.toml
```

### Step 2 — Deploy on Railway
```
1. Go to railway.app → sign in with GitHub
2. Click "New Project"
3. Select "Deploy from GitHub repo"
4. Choose: gbk-voice-agent
5. Railway will auto-detect Python and start building
```

### Step 3 — Add Environment Variables
```
In Railway dashboard → your project → Variables tab:

SARVAM_API_KEY     = (from app.sarvam.ai → API Keys)
OPENAI_API_KEY     = (from platform.openai.com → API Keys)
MAKE_WEBHOOK_URL   = (from Make.com Scenario 3 webhook)
VOBIZ_AUTH_ID      = SA_CCRV76JF
VOBIZ_AUTH_TOKEN   = a7805f9aae3624476572ac2f04d6c0d0...
PUBLIC_URL         = (Railway gives you this after deploy — see Step 4)
```

### Step 4 — Get Your Public URL
```
1. After deploy succeeds, Railway shows your URL like:
   https://gbk-voice-agent-production.up.railway.app

2. Go back to Variables → set PUBLIC_URL to this URL

3. Railway will auto-redeploy
```

### Step 5 — Update Make.com Scenario 1 (Trigger Call)
```
Change the answer_url in your HTTP module body to:

{
  "from": "912271264191",
  "to": "{{1.phone}}",
  "answer_url": "https://gbk-voice-agent-production.up.railway.app/answer",
  "hangup_url": "https://gbk-voice-agent-production.up.railway.app/hangup",
  "machine_detection": "true",
  "time_limit": 300
}
```

### Step 6 — Build Make.com Scenario 3 (Post-Call → GHL)
```
Module 1: Custom Webhook
  → Name: GBK Post Call Data
  → Copy webhook URL → paste as MAKE_WEBHOOK_URL env var in Railway

Module 2: Router
  Route 1 — Filter: extracted_data.lead_score = HOT
    → GHL Update Contact
    → Tag: lead-hot
    → Custom fields: bhk, budget, timeline, visit_day

  Route 2 — Filter: extracted_data.lead_score = WARM
    → GHL Update Contact
    → Tag: lead-warm

  Route 3 — Fallback (COLD)
    → GHL Update Contact
    → Tag: lead-cold

The webhook receives this JSON:
{
  "call_uuid": "...",
  "from_number": "912271264191",
  "to_number": "+919876543210",
  "call_duration": 125.5,
  "status": "completed",
  "lead_name": "Rahul",
  "lead_project": "Vishwajeet Heights",
  "extracted_data": {
    "bhk_preference": "2BHK",
    "budget_range": "45 lakh",
    "purchase_purpose": "personal",
    "purchase_timeline": "within 3 months",
    "site_visit_booked": "Yes",
    "visit_day": "Saturday",
    "lead_score": "HOT",
    "call_summary": "Lead interested in 2BHK in Vishwajeet Heights..."
  },
  "conversation_turns": 8,
  "timestamp": "2026-04-27T10:30:00"
}
```

### Step 7 — Test End to End
```
1. Go to Railway → check logs → verify "Server starting" message
2. Open browser → visit your PUBLIC_URL → should see health check JSON
3. In Make.com Scenario 1 → Run Once → trigger a test call to your phone
4. Pick up → you should hear Priya greeting in Hindi
5. Speak → AI responds → full conversation
6. Hang up → check Make.com Scenario 3 → verify data received
7. Check GHL → verify contact updated with tags and fields
```

## Architecture

```
Make.com Scenario 1          Python Server (Railway)        Make.com Scenario 3
─────────────────           ──────────────────────          ─────────────────
GHL Lead Created            /answer → XML + WS URL         Receives post-call
     ↓                          ↓                               ↓
Sleep 2s                   /ws WebSocket stream             Router: HOT/WARM/COLD
     ↓                     Lead audio → Sarvam STT              ↓
HTTP POST to Vobiz         → OpenAI GPT-4o-mini            GHL Update Contact
(trigger call)             → Sarvam TTS → audio back            ↓
                               ↓                           WhatsApp alert (HOT)
                          /hangup → extract data
                          → POST to Make webhook
```

## Costs
```
Per minute of conversation:
- Sarvam STT: ~₹0.50/min
- Sarvam TTS: ~₹0.25/min  
- OpenAI GPT-4o-mini: ~₹0.15/min
- Vobiz telephony: ~₹0.80/min
- Railway hosting: ~$5/month (free tier available)

Total: ~₹1.70/min (much cheaper than Bolna's ₹3-5/min)
```
