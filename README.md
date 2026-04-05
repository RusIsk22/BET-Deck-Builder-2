# BET Deck Builder Agent

Standalone FastAPI service that combines a Claude-powered chat agent with the BET PPTX build engine. No external agent builders (Dify etc.) needed.

## Architecture

```
[Browser Chat UI]
       │
       ▼
[FastAPI on Railway]
  ├── POST /chat     → Claude API → (optional) build .pptx → Supabase → download link
  ├── POST /build    → Direct outline → .pptx (legacy backup)
  ├── GET  /health   → Status check
  └── GET  /         → Chat frontend
```

## Setup

### 1. Create GitHub Repo

```bash
git init
git add .
git commit -m "BET Deck Builder Agent v2"
git remote add origin git@github.com:YOUR_USER/bet-deck-agent.git
git push -u origin main
```

### 2. Deploy on Railway

1. New Project → Deploy from GitHub Repo
2. Set environment variables:
   - `ANTHROPIC_API_KEY` — your Anthropic API key
   - `SUPABASE_URL` — `https://qzprrlqunszxrnwpzhvs.supabase.co`
   - `SUPABASE_KEY` — Supabase service role key
   - `BUILD_SECRET` — Bearer token for the `/build` endpoint (optional)
3. Railway auto-detects the Dockerfile
4. Set the target port in Railway networking settings (8000)

### 3. Test

```bash
# Health check
curl https://YOUR-APP.up.railway.app/health

# Chat
curl -X POST https://YOUR-APP.up.railway.app/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Erstelle eine Präsentation zum Thema Digitalisierung bei Stadtwerken, 6 Folien", "history": []}'
```

### 4. Open Chat UI

Visit `https://YOUR-APP.up.railway.app/` in your browser.

## Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI service (chat + build + frontend) |
| `build_deck.py` | PPTX generation engine (python-pptx) |
| `Master_Ergebnis.pptx` | BET corporate design template |
| `static/index.html` | Chat frontend |
| `Dockerfile` | Container config |
| `requirements.txt` | Python dependencies |
