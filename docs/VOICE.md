# Talk to your plants (voice on the Pi)

The Pi panel gets a **🎤 Sprich mit mir** button: speak, the plant answers out
loud. Speech-to-text and text-to-speech use the same AI key as the chat
(`docs/AI.md`); audio capture/playback happen *on the Pi* (via the host helper),
so the kiosk needs no browser mic permission.

## Where to plug the microphone and speaker

The Pi 4 has **no microphone**. Audio jacks:

| Signal | Where | Notes |
|---|---|---|
| **Speaker (output)** | 3.5 mm jack next to the HDMI ports, **or** HDMI audio | `aplay -l` shows `card 2: bcm2835 Headphones` (jack) and the two HDMI cards |
| **Microphone (input)** | **USB microphone** (or a USB webcam with a mic, or a USB sound card + 3.5 mm mic) | the 3.5 mm jack is **output only** — there is no mic input on a Pi 4 |

Recommended parts:

- **Speaker:** any powered speaker / small USB speaker. A cheap USB speaker is
  easiest (it becomes its own card and works without touching the 3.5 mm jack).
- **Mic:** a **USB mic** (e.g. a small conference mic) or a USB webcam with a mic.
  Plug it into either USB port; `arecord -l` should then list a capture device.

After plugging in a USB mic, check it:

```bash
arecord -l               # should now list a "card N: … , device 0" under CAPTURE
arecord -q -f S16_LE -r 16000 -c 1 -d 4 /tmp/test.wav && aplay /tmp/test.wav
```

If it records silence, set the default capture device (`~/.asoundrc`) or pick the
USB card explicitly.

## How it works

```
🎤 button → arecord (Pi mic) → /api/voice/ask
          → STT (AI provider) → grounded reply (AI chat)
          → TTS (AI provider) → aplay (Pi speaker) → you hear the plant
```

Endpoints (login required):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/voice/devices` | list playback/capture devices |
| POST | `/api/voice/record` | record N seconds from the Pi mic (host helper) |
| POST | `/api/voice/ask` | `{audio}` or `{text}` → `{question, reply, played}` |
| POST | `/api/voice/tts` | speak arbitrary text on the Pi speaker |

## Configuration

```env
AI_TTS_MODEL=gemini-3.8-flash-tts      # or gemini-2.5-flash-preview-tts
AI_TTS_VOICE=Kore
AI_STT_MODEL=gemini-3.8-flash
```

If TTS is unavailable, install a local fallback on the Pi:

```bash
sudo apt-get install -y espeak-ng        # then the helper can speak without cloud
```

## Notes & limits

- Recording is push-to-talk (fixed 4 s by default). Wake-word ("Hey Flora") is a
  possible later step (needs an always-on, low-power listener).
- Voice replies are kept short ("1–3 sentences") for the speaker.
- Everything is optional: with no mic, the button reports a clear error and the
  typed chat still works.
