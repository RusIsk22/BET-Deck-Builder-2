"""
BET Deck Builder — Standalone Agent Service

FastAPI service combining:
- POST /chat   → Multi-turn conversation with Claude API (BET Deck Builder Agent)
- POST /build  → Direct outline.json → .pptx build (legacy/backup)
- GET  /health → Health check
- GET  /       → Chat frontend
"""

import os
import re
import json
import uuid
import time
import tempfile
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

import anthropic
import httpx

from build_deck import build

# ── Config ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BUILD_SECRET = os.environ.get("BUILD_SECRET", "")
SUPABASE_BUCKET = "decks"
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "Master_Ergebnis.pptx")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bet-agent")

# ── FastAPI App ─────────────────────────────────────────────────────────
app = FastAPI(title="BET Deck Builder Agent", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── System Prompt ───────────────────────────────────────────────────────
SYSTEM_PROMPT = """Du bist der BET Deck Builder Agent — ein KI-Berater der Beratern hilft, 
professionelle PowerPoint-Präsentationen im BET Corporate Design zu erstellen.

Du arbeitest in einem strikten 3-Phasen-Workflow:

## Phase 0 — Metadaten sammeln
Bevor du irgendetwas analysierst, brauchst du:
1. **Titel** — Was soll auf der Titelfolie stehen?
2. **Fußzeile** — Format: `[Projektname] | [Monat Jahr]` (z.B. "Angebot Stadtwerk XY | März 2026")
3. **Untertitel** — Optional, für die Titelfolie

Falls der Berater Content mitliefert, frag trotzdem erst nach den Metadaten.

## Phase 1 — Inhaltsanalyse
Analysiere den Input und identifiziere:
- Zentrale These / Kernbotschaft?
- Gleichwertige Punkte?
- Vergleiche oder Alternativen?
- Zahlenlogik oder Daten?
- Prozess oder Sequenz?

Gruppiere in Content-Blöcke (3-5 für 8-14 Folien). Präsentiere die Analyse und warte auf Bestätigung.

## Phase 2 — Struktur bauen
Für jede Folie:
1. **Visual Type wählen** (Hero, Kacheln, Bild+Text, Prozess)
2. **Titel als Aussage** formulieren (NICHT als Thema-Label). "Das Problem ist nicht Kapital – sondern Struktur" statt "Ausgangssituation"
3. **Layout zuweisen** (siehe unten)

Präsentiere die Struktur und warte auf Bestätigung.

## Phase 3 — outline.json generieren
Wenn der Berater bestätigt, generiere das finale JSON.

**WICHTIG: Das JSON MUSS in einem speziellen Block stehen:**
```json
|||OUTLINE_START|||
{
  "title": "...",
  "subtitle": "...",
  "footer": "...",
  "slides": [...]
}
|||OUTLINE_END|||
```

### Layout-Katalog (Kurzfassung)

| Layout | Name | Verwendung |
|--------|------|-----------|
| 0 | Titel | Nur Titelfolie (wird automatisch erstellt) |
| 2 | Ergebnis + Text | Hero-Content mit Bullets |
| 3 | Text + 1/3 Bild | Text-dominant mit Bildakzent |
| 4 | Text + 1/2 Bild | 50/50 Text und Bild |
| 5 | Text + 2/3 Bild | Bild-dominant |
| 6 | 4 Kacheln + 1/3 Bild | 4 gleichwertige Items |
| 7 | Text + 1/3 Bild groß | High-Impact (max 2x pro Deck) |

### Wichtige Constraints:
- Layout 6: **Exakt 4 Kacheln** — nicht 3, nicht 5
- Layout 7: Max 2x pro Deck, Titel max ~45 Zeichen
- Titel immer **1 Zeile**, max ~55 Zeichen (Layouts 2-6)
- Fazit-Zeilen beginnen mit "→ "
- Content-Slides im `slides`-Array verwenden Layouts 2-7. Die Titelfolie (Layout 0) wird automatisch erstellt.

### JSON-Format für Slides:

Für Layout 2, 3, 4, 5, 7:
```json
{
  "layout": 2,
  "title": "Einzelne Aussage als Titel",
  "body": ["Punkt 1", "Punkt 2", "→ Fazit: Kernaussage"]
}
```

Für Layout 6 (Kacheln):
```json
{
  "layout": 6,
  "title": "Vier gleichwertige Faktoren",
  "kacheln": [
    {"title": "Faktor 1", "body": "Kurzbeschreibung"},
    {"title": "Faktor 2", "body": "Kurzbeschreibung"},
    {"title": "Faktor 3", "body": "Kurzbeschreibung"},
    {"title": "Faktor 4", "body": "Kurzbeschreibung"}
  ]
}
```

### Kommunikationsstil:
- Professionell aber zugänglich
- Auf Deutsch antworten
- Kurz und präzise — kein Filler-Text
- Bei Rückfragen maximal 3 Punkte gleichzeitig
- Wenn der Berater "ja", "passt", "bauen" o.ä. sagt → Phase 3 auslösen
"""


# ── Models ──────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    history: list = []  # [{"role": "user"|"assistant", "content": "..."}]


class ChatResponse(BaseModel):
    reply: str
    download_url: Optional[str] = None


class BuildRequest(BaseModel):
    outline: dict


class BuildResponse(BaseModel):
    download_url: str
    filename: str


# ── Supabase Upload ────────────────────────────────────────────────────
async def upload_to_supabase(filepath: str, filename: str) -> str:
    """Upload .pptx to Supabase Storage and return public URL."""
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"

    with open(filepath, "rb") as f:
        file_bytes = f.read()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "x-upsert": "true",
            },
            content=file_bytes,
        )

    if resp.status_code not in (200, 201):
        log.error(f"Supabase upload failed: {resp.status_code} {resp.text}")
        raise HTTPException(500, f"Supabase upload failed: {resp.text}")

    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
    return public_url


# ── Log to Supabase ────────────────────────────────────────────────────
async def log_to_supabase(data: dict):
    """Log chat interaction to Supabase chat_logs table."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/chat_logs",
                headers={
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "apikey": SUPABASE_KEY,
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=data,
            )
    except Exception as e:
        log.warning(f"Logging failed (non-critical): {e}")


# ── Build PPTX ─────────────────────────────────────────────────────────
async def build_and_upload(outline: dict) -> tuple[str, str]:
    """Build .pptx from outline and upload to Supabase. Returns (url, filename)."""
    filename = f"deck_{uuid.uuid4().hex[:8]}.pptx"

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, filename)
        build(TEMPLATE_PATH, outline, output_path)
        url = await upload_to_supabase(output_path, filename)

    return url, filename


# ── Extract outline.json from Claude response ─────────────────────────
def extract_outline(text: str) -> Optional[dict]:
    """Try to extract outline JSON from Claude's response."""
    pattern = r'\|\|\|OUTLINE_START\|\|\|\s*(\{.*?\})\s*\|\|\|OUTLINE_END\|\|\|'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            log.warning(f"Found outline markers but invalid JSON: {e}")
            return None
    return None


def clean_reply(text: str) -> str:
    """Remove the outline JSON block from the reply text."""
    pattern = r'```json\s*\|\|\|OUTLINE_START\|\|\|.*?\|\|\|OUTLINE_END\|\|\|\s*```'
    cleaned = re.sub(pattern, '', text, flags=re.DOTALL)
    pattern2 = r'\|\|\|OUTLINE_START\|\|\|.*?\|\|\|OUTLINE_END\|\|\|'
    cleaned = re.sub(pattern2, '', cleaned, flags=re.DOTALL)
    return cleaned.strip()


# ── Endpoints ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "bet-deck-agent",
        "version": "2.0.0",
        "has_anthropic_key": bool(ANTHROPIC_API_KEY),
        "has_supabase": bool(SUPABASE_URL and SUPABASE_KEY),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Multi-turn chat with the BET Deck Builder Agent."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured")

    # Build messages array
    messages = []
    for msg in req.history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": req.message})

    # Call Claude API with timing
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    start_time = time.time()

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
    except anthropic.APIError as e:
        log.error(f"Claude API error: {e}")
        raise HTTPException(502, f"Claude API error: {str(e)}")

    latency_ms = int((time.time() - start_time) * 1000)
    reply_text = response.content[0].text

    # Check if the response contains an outline
    outline = extract_outline(reply_text)
    download_url = None
    deck_built = False

    if outline:
        try:
            url, filename = await build_and_upload(outline)
            download_url = url
            deck_built = True
            reply_text = clean_reply(reply_text)
            if not reply_text:
                reply_text = "Ihre Präsentation ist fertig!"
            reply_text += f"\n\n📥 **Download:** {url}"
            log.info(f"Built and uploaded: {filename}")
        except Exception as e:
            log.error(f"Build/upload failed: {e}")
            reply_text = clean_reply(reply_text)
            reply_text += f"\n\n⚠️ Fehler beim Erstellen der Präsentation: {str(e)}"

    # Log to Supabase AFTER everything else succeeded
    try:
        await log_to_supabase({
            "user_message": req.message,
            "assistant_reply": reply_text[:5000],
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "latency_ms": latency_ms,
            "deck_built": deck_built,
            "download_url": download_url,
            "conversation_length": len(req.history) + 1,
        })
    except Exception:
        pass  # never let logging break the response

    return ChatResponse(reply=reply_text, download_url=download_url)


@app.post("/build", response_model=BuildResponse)
async def build_endpoint(req: BuildRequest, request: Request):
    """Direct build endpoint (legacy/backup). Requires BUILD_SECRET."""
    if BUILD_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {BUILD_SECRET}":
            raise HTTPException(401, "Unauthorized")

    try:
        url, filename = await build_and_upload(req.outline)
        return BuildResponse(download_url=url, filename=filename)
    except Exception as e:
        log.error(f"Build failed: {e}")
        raise HTTPException(500, str(e))


# ── Serve static frontend ──────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    static_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(static_path):
        with open(static_path) as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>BET Deck Builder Agent</h1><p>Frontend not found.</p>")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
