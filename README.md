# pravachan-pipeline

Turn **Jain pravachan audio** — a Google Drive folder, a YouTube link, or local files —
into clean, typeset Gujarati **PDF + DOCX** documents, end to end.

Each talk is fetched, transcribed via **NotebookLM**, cleaned + structured + typeset by the
bundled `jain-pravachan-transcript` skill through a low-cost **OpenRouter** model (ASR terms
fixed, sections, gathas restored, Gujarati numerals, A5 book layout), and every document is
**consensus-verified** by independent models from other vendors before it is typeset.
Everything runs **on your own machine, on your own OpenRouter key and your own NotebookLM
login** — no data leaves, no per-seat bill.

## Three ways to run

### 1. Claude Code superskill (self-contained — Claude Code; unaffected by the OpenRouter change below)
Drop `claude-skill/pravachan` into `~/.claude/skills/` and paste a link or path:
```bash
claude-skill/install.sh
```
The `pravachan` superskill then runs the whole pipeline in **multi-agent mode** (a typeset
subagent per transcript + a verifier subagent per doc): bootstrap deps → fetch → transcribe
→ typeset → **scripture-verify** → PDF/DOCX. It asks for configuration (title style,
transcriber, concurrency, verifier passes) and installs anything missing.

### 2. CLI
```bash
pravachan "https://drive.google.com/drive/folders/XXXX" --out ./out/aagam --title-style numbered
pravachan "https://youtube.com/playlist?list=YYYY"        --out ./out/talks --title-style topic
pravachan ./local_audio_dir --out ./out/granth --title-style granth
```

### 3. Web app
```bash
pravachan-web            # → http://127.0.0.1:8765
```
Paste a Drive/YouTube link or upload audio, pick a set name + title style, queue the job,
and download PDF/DOCX/zip from the dashboard.

## Install
```bash
brew install ffmpeg pango geckodriver
brew install --cask firefox
brew install --cask font-noto-serif-gujarati font-noto-sans-gujarati font-noto-serif-devanagari font-noto-sans-devanagari
pip install -e .                          # installs `pravachan`, `pravachan-web`, `pravachan-nlm-login`
export OPENROUTER_API_KEY=sk-or-...       # https://openrouter.ai/keys — used by the build stage
pravachan-nlm-login                       # one-time: sign in to NotebookLM in a dedicated profile
```

## OpenRouter build stage + consensus (this fork)

The build stage runs on **OpenRouter** instead of the Anthropic API. Default build model:
`deepseek/deepseek-v4.1-flash` (~30x cheaper than Claude Opus at comparable quality for this
task). To keep quality honest, every document is **consensus-verified** before typesetting:

1. **Generate** — the build model writes the HTML fragment (skill contract unchanged).
2. **Verify ×2** — two independent models from *different vendors* (default `z-ai/glm-5.3`
   and `qwen/qwen3.8-flash`) each re-read the transcript + fragment and return a structured
   verdict against 6 required checks (contract, numerals, gatha fidelity, corrections,
   fidelity, structure). Deterministic model-free checks run alongside (tag whitelist,
   Gujarati numerals, mindmap 5×3, truncation, content-loss ratio).
3. **Fix loop** — if any verifier or check fails, the build model applies the findings and
   re-verification runs (max `--consensus-rounds`, default 2).
4. **Fail-closed flag** — anything still failing gets a `.review.json` next to the PDF/DOCX
   plus a loud log line; the document is still produced, flagged for a human.

Env: `OPENROUTER_API_KEY` (required) · `PRAVACHAN_BUILD_MODEL` · `PRAVACHAN_VERIFY_MODELS` ·
`PRAVACHAN_CONSENSUS_ROUNDS` · `PRAVACHAN_VERIFY_REASONING` (JSON per-model reasoning caps,
defaults: glm-5.3 `{"effort":"low"}`, qwen3.8-flash `{"max_tokens":8000}`).
Claude models remain usable via OpenRouter ids (e.g. `--model anthropic/claude-opus-5`) for
A/B comparisons.

## Transcription = NotebookLM (Selenium)

The same approach used to build the original sets. Sign-in is a **two-phase** flow:

- **`pravachan-nlm-login`** opens a **normal Firefox window** on a dedicated automation
  profile (never your real profile) — you sign into NotebookLM there. Google blocks sign-in
  inside an automated browser, which is why login is a separate manual step. The session is
  saved to the profile.
- Transcription then drives that already-logged-in profile with Selenium (upload → read the
  transcript from the viewer that auto-opens). One reliable session; a fresh Google account
  has a fresh notebook quota.

Google Cloud Speech-to-Text (`--transcriber google`, model `chirp_2`) is an optional
fallback. There is no Whisper.

## Options
```
--source-type auto|drive|youtube|local    # default auto-detects from the URL
--title-style topic|numbered|granth|day|auto
--target-mb 190                           # compress (not split) audio larger than this
--transcriber notebooklm|google
--format both|pdf|docx   --columns 2  --size 18  --portrait
--workers 4              # parallel build workers
--model deepseek/deepseek-v4.1-flash      # build model (OpenRouter id; e.g. qwen/qwen3.8-max-0902 for max quality)
--verify-models z-ai/glm-5.3,qwen/qwen3.8-flash   # consensus verifiers (other vendors)
--no-consensus           # single-model generation only
--only download,transcribe,build          # run a subset of stages
```

## Title styles
- **topic** — H1 from the talk's content; date-only subtitle. No speaker, no day number.
- **numbered** — `વ્યાખ્યાન N · date`.
- **granth** — `ગ્રંથ પરિચય · વ્યાખ્યાન N · date`.
- **day** — `ધ્યાન શિબિર` / `દિવસ N`.

All numbers/dates render in **Gujarati numerals**, derived from the filename.

## Layout
```
pravachan-pipeline/
  pipeline.py          # engine: fetch → transcribe → OpenRouter typeset + consensus → PDF/DOCX (CLI: `pravachan`)
  openrouter.py        # OpenRouter client + consensus verification (build stage)
  nlm.py               # NotebookLM transcriber (Selenium + Firefox profile); `pravachan-nlm-login`
  gstt.py              # optional Google Cloud STT (chirp_2) fallback
  webapp/              # FastAPI web app + dashboard (`pravachan-web`)
  skill/               # the jain-pravachan-transcript skill: SKILL.md, corrections, structure, build_doc.py
  skills/              # per-step Claude Code skills: pravachan-run / -fetch / -transcribe / -typeset
  claude-skill/        # self-contained `pravachan` superskill (fetch→transcribe→typeset→verify) + install.sh
```

## Notes
- Transcripts are machine ASR; the skill fixes common misrecognitions and the consensus
  verifiers check canon (gathas, Tirthankara/agam names, standard numbers), but a human
  proofread is wise before authoritative use.
- Audio over ~200 MB is **compressed** (mono, lower bitrate — inaudible for speech), not
  split; the original is kept in `audio/_oversized/`. Splitting is a last resort.
- Public Drive folders work directly; private folders need a service-account Drive API fetch.

## License
MIT — see [LICENSE](LICENSE).
