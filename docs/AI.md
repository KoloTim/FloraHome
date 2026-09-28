# AI: plant chat + photo → plant

FloraHome has an optional AI layer. It is **provider-agnostic** (any
OpenAI-compatible endpoint) and fails soft — with no key configured, the feature
simply reports itself unavailable and the rest of the dashboard is unchanged.

## What it does

1. **Chat with your plants.** Ask "Wie geht es dir?" and the answer is grounded
   in that node's *live* telemetry and its species' care range — so it is about
   *this* plant, not generic chatbot fluff.
2. **Photo → new plant.** Take a photo on your phone (the browser opens the
   camera directly); the vision model identifies the species, matches it to the
   built-in catalog, and creates the plant profile. You can then flash a free
   ESP to that node.

## Configuration (free)

Any of these work — put the values in `.env` and recreate the API:

```env
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
AI_API_KEY=<your key>
AI_MODEL=gemini-3.8-flash
AI_VISION_MODEL=gemini-3.8-flash
AI_FALLBACKS=gemini-3.8-flash,gemini-flash-latest,gemini-3.6-flash,gemma-4-31b-it
```

| Provider | `AI_BASE_URL` | Notes |
|---|---|---|
| **Google AI Studio (Gemini)** | `https://generativelanguage.googleapis.com/v1beta/openai` | free key, no card; vision-capable |
| **OpenRouter** | `https://openrouter.ai/api/v1` | use `:free` models (e.g. `google/gemma-4-31b-it:free`), ~200 req/day |
| **Groq** | `https://api.groq.com/openai/v1` | very fast; free tier |
| **ModelScope** | `https://api-inference.modelscope.cn/v1` | Qwen/DeepSeek, 2000 req/day |

The code tries `AI_MODEL`, then each entry in `AI_FALLBACKS`, skipping
busy (503), rate-limited (429) and missing (404) models. So a transient
"high demand" on one model doesn't break the feature.

Restart with env changes applied:

```bash
cd ~/smartplanter && docker compose up -d --force-recreate api
curl -s localhost:8097/api/ai/status
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ai/status` | configured? which models? |
| POST | `/api/ai/chat` | `{"node":"plant-a","message":"…"}` → `{"reply":"…"}` (login required) |
| POST | `/api/ai/identify` | `{"image":"data:image/…;base64,…"}` → species guess + catalog match |

## Privacy & cost

- Only the plant's **numbers** are sent for chat (no photos, no personal data).
- A photo is sent **only** when you tap "Neue Pflanze" and pick a file.
- Free tiers are rate-limited (Gemini/Groq vary; OpenRouter ~200/day). Fine for
  a family of plants.
- If you want zero third-party calls, point `AI_BASE_URL` at a local
  OpenAI-compatible server (e.g. Ollama) — the code doesn't care.

## Roadmap / ideas

- Weekly AI "plant diary" summary per node.
- Diagnose leaf problems from a photo (pests/overwatering) and attach the advice
  to the plant's notes.
- Voice: Gemini's TTS models are listed for this key — a plant that *speaks* its
  mood is a fun demo.
