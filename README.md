# Voice Presentation Agent

An AI agent that presents a slide deck out loud, answers questions about it in real time, and navigates to whichever slide holds the answer. Upload a PDF or PowerPoint file and it becomes a presentation you can interrupt and talk to.

**[Watch the demo](https://www.tella.tv/video/voice-activated-presentation-agent-demo-1hqc)** &nbsp;·&nbsp; **[Try it live](https://98-80-186-155.sslip.io)**

---

## What it does

1. **Upload** a PDF or PowerPoint file and watch it parse. Pages render one at a time and appear as thumbnails while an AI writes spoken narration for each slide.
2. **Review** the parsed deck in a gallery before presenting, so a bad parse is caught early rather than mid-presentation.
3. **Pick a presenter.** Six voices, each with its own delivery style, from a precise analyst to a story led narrator.
4. **Listen.** The agent presents each slide with word synchronised subtitles, then stops and waits for you.
5. **Interrupt any time.** Ask a question mid sentence. It stops, works out which slide answers you, navigates there, then answers from that slide.
6. **Review the session.** After ending the call you get an analysis of every question asked: what the audience wanted, how well each answer scored, and which topics the deck failed to cover.

The agent answers only from the deck. Ask it something the deck does not cover and it says so plainly instead of improvising.

---

## Quick start with Docker

One command, once you have the four API keys below:

```bash
cp .env.example .env && $EDITOR .env && docker compose up --build
```

Then open **http://localhost:7860**.

That builds the frontend, installs the backend, bundles LibreOffice for PowerPoint conversion, and serves everything from a single container on one port.

### API keys you need

Fill these four in `.env`. Everything else has a working default.

| Variable | Where to get it | Used for |
|---|---|---|
| `DEEPGRAM_API_KEY` | [console.deepgram.com](https://console.deepgram.com) | Speech to text and turn detection |
| `CARTESIA_API_KEY` | [play.cartesia.ai](https://play.cartesia.ai) | Text to speech |
| `DEEPSEEK_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com) | The conversation and deck ingestion |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Optional. Reads charts and diagrams from uploaded slides |

Without a Gemini key the app still works. Uploaded decks are narrated from each page's text layer alone, which is fine for text heavy slides and thin for charts.

Check that everything is wired up:

```bash
curl http://localhost:7860/api/health
```

---

## Running without Docker

Two processes. Backend first.

**Backend**

```bash
cd server
python -m venv .venv && .venv/Scripts/activate     # Windows
# python3 -m venv .venv && source .venv/bin/activate  # macOS and Linux
pip install -r requirements.txt
python main.py
```

**Frontend**

```bash
cd web
npm install
npm run dev
```

Open http://localhost:5173. Vite proxies API calls through to the backend on port 7860.

For PowerPoint support outside Docker, install [LibreOffice](https://www.libreoffice.org/download/). The app finds it on `PATH` automatically, and `/api/health` reports `pptx_supported` so the upload page can say up front whether `.pptx` will be accepted.

---

## How it works

```
PDF or PPTX
    |
    |  LibreOffice converts PPTX to PDF
    |  PyMuPDF renders each page to PNG and extracts its text
    v
Narration model (Gemini vision, falling back to DeepSeek text)
    |  per slide: title, bullets, spoken narration, routing keywords
    |  per deck:  intro, transitions, outro, presenter persona
    v
Deck stored on disk
    |
    v
Live session
    Deepgram Flux  ->  DeepSeek  ->  Cartesia  ->  browser
    speech in         decides         speech out
                      and navigates
```

The voice pipeline runs on [Pipecat](https://github.com/pipecat-ai/pipecat) 1.7.0 over WebRTC.

**Deepgram Flux** does transcription and turn detection in one model, using acoustic and semantic cues together. There is no separate voice activity detector and no separate end of turn model to disagree with it, which is what makes interruption feel immediate rather than delayed.

**DeepSeek** holds the whole deck in its system prompt and decides what to say and where to navigate. Navigation happens through tool calls, not keyword matching.

**Cartesia** speaks, and returns per word timestamps. Pipecat releases one text frame per word in playback order, so the subtitles are genuinely in step with the audio rather than estimated from a words per minute guess.

### Project layout

```
server/
  main.py         FastAPI app, WebRTC signalling, REST API
  bot.py          the Pipecat pipeline
  prompts.py      the presenter's system prompt and its guardrails
  presenters.py   the six selectable voices and delivery styles
  ingest.py       PDF and PPTX to deck
  convert.py      PowerPoint to PDF via headless LibreOffice
  narration.py    slide narration, vision first with text fallback
  insights.py     post session transcript analysis
  tools.py        the tools the agent can call
  slides.py       deck and slide models, plus the built in deck
  store.py        deck persistence
  net.py          outbound HTTP, pinned to IPv4
  latency.py      timing instrumentation

web/src/
  pages/          Home, Deck (parse and review), Present
  components/     slide stage, subtitles, presenter picker, insights

deploy/           EC2 provisioning and deployment
scripts/          headless smoke test and voice catalogue check
```

---

## Design decisions

### Navigation is a tool call, not keyword matching

The model calls `goto_slide(number, reason)` and receives that slide's full content back, which is then what it answers from. The tool result is the grounding.

Keyword matching fails on exactly the questions people actually ask. "The one with the big number" and "what happens right before that" share no words with the slide that answers them. The keywords in the prompt are labelled as hints for the model's judgement, explicitly not a lookup table.

### The whole deck goes in the prompt. There is no RAG

A fourteen slide deck is roughly 4,800 tokens, which is cheap to hold in context. Retrieval would actively hurt here for three reasons.

Navigation needs global knowledge, because working out which slide someone means requires knowing the shape of the whole deck. Retrieval adds a round trip before the first spoken word, and in a voice application latency is the product. Grounding is stronger with the full deck, because refusing an off deck question means knowing what the deck does not contain, which you cannot prove from top k chunks.

The practical ceiling is `MAX_SLIDES`, which defaults to 40.

### The slide changes before the answer is spoken

`goto_slide` pushes the slide change to the browser and returns the slide content in the same call. The screen updates while the model is still forming the sentence, so the audience is looking at the right slide before they hear the answer instead of a beat after it.

### The viewer shows the real slide

Every page is rendered to PNG at 150 DPI and displayed as the actual page, charts and layout included, rather than reconstructed from extracted text. The generated bullets sit underneath as talking points.

Rendering is local and never depends on any API, so page images work even when a narration model is unavailable.

### Vision first narration

A slide's text layer is not a script. It is fragments, orphaned headings, page numbers, and bullet glyphs, and it misses charts entirely. Each page is sent to Gemini as an image alongside its text, so a bar chart becomes "Groq Llama three point three at eight hundred" rather than nothing at all.

Every vision path degrades to text only narration on failure, so one bad page or a provider hiccup produces a thinner deck rather than a failed upload.

### Two layers of persona

The deck's own persona is inferred during ingestion and describes who would plausibly give this particular talk, which sets the subject matter register. The chosen presenter describes how that speaker delivers anything at all, which sets temperament. The deck persona is listed first so subject framing wins wherever the two pull against each other.

### Ingestion publishes progress as it goes

Rendering is fast and local, roughly 46 milliseconds per page. Narration is slow and network bound, several seconds per slide. Showing one combined percentage across both would crawl and then leap, so the parsing view shows them as separate steps and the frontend polls faster during rendering than during narration.

Page images land on disk one at a time, so thumbnails appear progressively instead of all at once at the end.

### Counted facts and judged scores are kept apart

The session analysis computes question counts, word counts and turn counts in Python. Only the assessments come from a model, and every score is shown next to the model's own reasoning. A model asked to count will occasionally be wrong for no reason, and there is no need to accept that when the transcript is right there.

### Outbound HTTP is pinned to IPv4

Some networks run DNS64 and NAT64, answering with a synthetic `64:ff9b::/96` address ahead of the real IPv4 one. Where the NAT64 gateway is not actually reachable, every request hangs on that address until it times out. httpx has no Happy Eyeballs fallback, so the OpenAI SDK burns its entire retry budget getting nowhere and the agent connects successfully but never speaks.

Binding the local socket to `0.0.0.0` constrains connections to IPv4 and skips the dead address. On the network this was built on, that took DeepSeek calls from 16.6 second timeouts to roughly 1.1 seconds. Set `FORCE_IPV4=0` on a genuinely IPv6 only network.

---

## Security and guardrails

Deck content is untrusted input. It arrives in a file anyone can upload, and although it passes through a narration model first, that model faithfully reproduces what it reads.

**Prompt injection through deck content.** A crafted PDF containing `</deck>` followed by new instructions would close the prompt's own markup early, and everything after it would read as instruction rather than data. Every deck derived field is stripped of angle bracket tags before it reaches the prompt. The words survive as inert text, because deleting them would silently change a deck's meaning, but the structure an injection depends on does not.

**Explicit instruction hierarchy.** The prompt states that everything inside `<deck>` is content to present and never instructions to follow, that the audience can steer the presentation but cannot change the rules or grant a new role, and that the system prompt, deck markup and tool list are never to be revealed or paraphrased under any framing.

**Grounding.** The agent answers only from slide content, never invents figures or claims, and refuses off deck questions by name rather than improvising. A refusal is correct behaviour, not a failure, and the session analysis scores it that way.

**Secrets.** API keys travel in headers, never in query strings. httpx puts the full URL into the exception it raises on failure, and that exception is logged, so a key in a query string ends up in plaintext in container logs. A test asserts no key is ever interpolated into a URL. `.env` is listed in `.dockerignore` so it is never baked into an image layer, and is copied to the server over SSH with mode 600.

**Uploads.** Size capped by `MAX_UPLOAD_MB`, page count capped by `MAX_SLIDES`, extensions restricted to PDF and PowerPoint, and deck identifiers validated against path traversal before touching the filesystem.

**CORS.** Restricted to the local Vite dev origin by default rather than `*`. In production the API and the frontend share one origin, so no cross origin access is needed at all.

**Transport.** The deployed instance serves HTTPS only, with a real certificate. This is not cosmetic: browsers refuse microphone access on an insecure origin, so a plain HTTP deployment loads the page and then silently fails to hear anything.

---

## Testing

```bash
cd server && python -m pytest -q          # 142 tests
cd web && npm run build                   # typecheck and bundle
```

Two scripts exercise the running system:

```bash
python scripts/smoke_session.py                        # full pipeline, no browser
python scripts/smoke_session.py --presenter briefer     # as a specific presenter
python scripts/check_voices.py                          # voice ids still resolve
```

`smoke_session.py` connects over WebRTC exactly as the browser would, streams silence, and reports what comes back. It fails when audio is silent or the agent never spoke, not merely when no frames arrive. Frames keep flowing when text to speech is broken, so counting them alone reports success on a completely mute agent.

---

## Deployment

The app runs on a single EC2 instance behind Caddy, which terminates TLS with a Let's Encrypt certificate.

```bash
python deploy/provision.py     # key pair, security group, instance
python deploy/deploy.py        # sync, build, start, verify
```

See [deploy/README.md](deploy/README.md) for the full runbook and the reasoning behind each choice, including why WebRTC needs the UDP range open, why the instance uses host networking, and why a 1 GB instance needs swap to build the image at all.

---

## Known limitations

**Cost and quota.** Every model is a paid API. Cartesia returns HTTP 402 when credits run out, and the symptom is a session that connects normally and produces silence.

**Gemini free tier.** Capped per day per model. Vision narration and any other Gemini use share that budget, so heavy upload testing can exhaust it. Uploads then fall back to text only narration.

**Transient provider errors are not retried.** A single HTTP 503 from the narration model drops that one slide to text only. Retrying with backoff would be the obvious improvement.

**Scanned PDFs.** A PDF with no text layer needs a vision model configured. Without one the upload is rejected with an explanation rather than producing an empty deck.

**Single instance.** Deck storage is the local filesystem and sessions are held in process, so this does not scale horizontally as written.

**Speculative inference is not wired up.** Deepgram Flux emits an eager end of turn event before it confirms the turn. Starting the language model call at that point and discarding it if the user resumes would remove real latency. The event is logged today but not acted on.

---

## What I would do next

Retry transient provider failures before falling back. Act on the eager end of turn event to cut response latency. Move deck storage to object storage and sessions to a shared store so more than one instance can run. Add per IP rate limiting on upload and session creation. Cache the deck prompt across turns, since it is identical every time and currently re sent in full.
