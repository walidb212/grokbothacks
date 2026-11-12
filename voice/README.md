# Voice analyst (ElevenLabs)

The voice analyst runs inside the UI (`ui/`). It talks about the report on screen and drives the page through five client tools:

| Tool | What it does in the UI |
|---|---|
| `get_deal_report` | Returns every figure with value, source, page and status |
| `highlight_figure(metric)` | Scrolls to the figure, highlights it, shows its source chain |
| `open_source(metric)` | Shows where the figure came from |
| `run_what_if(...)` | Recomputes gross income, NOI, yield on cost and verdict; changes stack |
| `reset_what_if` | Back to the base case |

The API key stays on the server. The browser gets a short-lived WebRTC token from `GET /api/voice/token` (fallback: signed WebSocket URL from `/api/voice/signed-url`).

## Set up the agent (once, about 1 minute)

```bash
cd ~/grokbothacks
python3 voice/setup_agent.py --dry-run                       # check the payload
ELEVENLABS_API_KEY=sk_... python3 voice/setup_agent.py       # creates 5 tools + agent, prints ELEVENLABS_AGENT_ID
```

Then on the server add both lines to `/opt/demo3/app/.env` and restart the API:

```
ELEVENLABS_API_KEY=sk_...
ELEVENLABS_AGENT_ID=agent_...
```
```bash
cd /opt/demo3/app && docker compose up -d --build
```

Options (environment variables for `setup_agent.py`):

- `AGENT_LLM` default `gemini-2.5-flash`. `grok-beta` is available too if you want Grok to drive the voice agent; test tool calling before the demo.
- `AGENT_VOICE` an ElevenLabs voice id (default voice if empty).
- `AGENT_HOSTS` allowed hostnames, default `demo3.ai-vance.de,localhost`.

Change the prompt: edit `voice/system_prompt.md`, then `python3 voice/setup_agent.py --update agent_...`.

## Local test

```bash
cd ~/grokbothacks/api && ELEVENLABS_API_KEY=... ELEVENLABS_AGENT_ID=... uvicorn main:app --port 8000
cd ~/grokbothacks/ui && python3 -m http.server 3000
open "http://localhost:3000/?api=http://localhost:8000"
```

## Demo checklist

- Use Chrome, allow the microphone, headphones or a quiet corner (echo).
- Typed questions work during a session (input under the transcript) as a fallback in a loud room.
- Good demo questions: "Why is this a GO?", "Where does the GIA come from?", "Which figures are assumptions?", "What if occupancy drops to 90 percent?", "And if studios only get 300 pounds?".
