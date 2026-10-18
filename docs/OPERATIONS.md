# Pravachan Pipeline — Operations SOP (this fork)

Fork of [`pur4v/pravachan-pipeline`](https://github.com/pur4v/pravachan-pipeline) maintained by
@hardiksa. `main` on this fork carries the **OpenRouter build stage with cross-vendor consensus
verification**, mixed-script (Gujarati + Devanagari) typesetting support, and a set of upstream
fixes. This is the operational SOP: setup, running, verification, and the gotchas that cost real
time to find.

---

## 1. What this fork adds vs upstream

| Commit | Change |
| --- | --- |
| `86d2027` | **fix(build_doc)** — container-div content (`box`/`verse`/`stats`) rendered twice; corrected the parser skip-list |
| `8a423cc` | **fix(webapp)** — starlette ≥1.0 `TemplateResponse(request, …)` signature; added `run-web.sh` launcher |
| `37028a7` | **feat: OpenRouter build stage** — replaces the Anthropic dependency; cross-vendor consensus (2 independent verifiers + deterministic checks), bounded fix loop, fail-closed `.review.json` |
| `dd7f8df` | **fix(download)** — single-file Google Drive links (not just folders) |
| `dbb11c0` | **build_doc/det** — Noto Devanagari font stacks + summary/leaf markup normalisation; det-checker accepts the same variants; README font casks |

Upstream (`pur4v`) is untouched; ports of any of these may be opened upstream separately.

## 2. Setup

```bash
brew install ffmpeg pango geckodriver
brew install --cask firefox
brew install --cask font-noto-serif-gujarati font-noto-sans-gujarati font-noto-serif-devanagari font-noto-sans-devanagari
pip install -e .
export OPENROUTER_API_KEY=sk-or-...      # build stage; never commit the value anywhere
pravachan-nlm-login                      # one-time: NotebookLM sign-in on a dedicated profile
```

- The key is **environment-only** (read once by `openrouter.py`); the build stage fails closed without it.
- Transcription needs no key; the web dashboard warns until `OPENROUTER_API_KEY` is set in the server's environment.

## 3. Running

- **Web**: `bash run-web.sh` → http://127.0.0.1:8765. Always launch through this script — it activates the
  venv (jobs spawn `python3` from PATH) and exports the WeasyPrint dylib path.
- **CLI**: `pravachan <drive-folder|drive-file|youtube|local-dir> --out DIR [--only download,transcribe,build]`
- Stages are resumable — anything already produced is skipped (`skip ← <file>`).

## 4. Build stage + consensus (OpenRouter)

Flow: **generate → verify → (bounded fix loop) → PASS or flag.**

- Generator (default `deepseek/deepseek-v4.1-flash`) drafts the clean+structured HTML fragment from
  the transcript per the `skill/` contract.
- **Consensus**: two independent verifiers from vendors *other than* the generator
  (`z-ai/glm-5.3`, `qwen/qwen3.8-flash`), each running 6 required checks, **plus model-free
  deterministic checks** (tag whitelist, numerals, mindmap 5×3, truncation, content loss).
- Fail-closed: unresolved docs still produce PDF/DOCX but write `<doc>.review.json` and log loudly.
- Env knobs:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | — | required for the build stage |
| `PRAVACHAN_BUILD_MODEL` | `deepseek/deepseek-v4.1-flash` | generator |
| `PRAVACHAN_VERIFY_MODELS` | `z-ai/glm-5.3,qwen/qwen3.8-flash` | verifier models (other vendors) |
| `PRAVACHAN_CONSENSUS_ROUNDS` | `2` | max fix rounds |
| `PRAVACHAN_VERIFY_REASONING` | per-model caps | JSON reasoning config overrides |
| `PRAVACHAN_KEEP_FRAGMENT` | unset | keep `<doc>.fragment.html` beside outputs |
| `PRAVACHAN_OUT` / `PRAVACHAN_PORT` | `~/pravachan-output` / `8765` | web app root / port |

- Typical cost: **~cents/doc** (a clean round-0 pass ≈ $0.02; a run with two heavy fix rounds ≈ several
  tenths of a dollar). Verifier reasoning caps exist precisely to keep fix rounds affordable.

## 5. macOS environment gotchas

- **WeasyPrint** needs `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` (else `import weasyprint` fails,
  `libgobject-2.0-0`). Baked into `.venv/bin/activate` and `run-web.sh`.
- **Never rely on system font fallback.** Apple's `.SF Devanagari` is subset by WeasyPrint into an
  *invalid CFF embed*: `pdffonts` reports `Syntax Error: Embedded font file may be invalid`,
  PDFKit renders garbage glyphs, while the pypdf text layer still extracts plausible text (so
  text-based checks do not catch it). All font stacks pin the Noto families explicitly.
- **Verify renders with two engines**: `qlmanage -t -s 1600 -o DIR file.pdf` (PDFKit) and
  `pdftoppm -png -r 120` (poppler). `pdffonts` must show zero syntax errors and only TrueType CID fonts.
- PDF preview: prefer `qlmanage` — `sips` renders PDF backgrounds transparent (comes out dark).

## 6. Devanagari / mixed-script documents

- `build_doc` normalises common model markup variants deterministically: `<p class="leaf">` leaves and
  bare `<h2>સારાંશ</h2> + <p>…` summaries are folded into the canonical `div` form; `<b>`-wrapped
  branch themes are accepted. Symptom if unhandled: mindmap page renders as 5 empty colored bars.
- The deterministic checker accepts the same variants, so harmless markup variance no longer burns
  fix rounds or produces false flags.

## 7. Reasoning & encoding pitfalls (all fixed in `openrouter.py`)

- SSE must be decoded as UTF-8 explicitly (`requests.iter_lines(decode_unicode=True)` defaults to
  ISO-8859-1 for `text/event-stream` without charset → mojibake).
- Reasoning models can burn the whole budget thinking and return no content
  (`finish_reason=length`). Caps: glm-5.3 → `reasoning.effort=low` (its default is `max`);
  qwen3.8-flash → `reasoning.max_tokens` (it ignores effort). Length-truncated empty completions
  are deterministic — never retry them.
- Verifier output budget should stay ≥ 2× the expected reasoning cap (default 24 000 tokens).

## 8. Transcription when Firefox/Selenium can't run (CDP fallback)

If `pravachan-nlm-login` or the Selenium transcriber hangs, first check whether Firefox itself can
launch: on some managed macOS machines every Firefox launch stalls pre-main at `_dyld_start`
(consent/trust-evaluation daemon spin) regardless of launch context. Remedy is host-side (reboot,
or restart the security daemon). Until then, NotebookLM can be driven in the user's *signed-in
browser* over CDP:

1. Open the notebook, click **Add sources → Upload files** — the click must be a **real input event**
   (CDP mouse at the button's coordinates); a JS `.click()` is untrusted and silently ignored.
2. Once clicked, `input[type=file]` appears; attach the file with `DOM.setFileInputFiles`.
   Keep `Page.setInterceptFileChooserDialog(enabled=True)` so no native picker blocks an
   unmanned machine.
3. Confirm the source row (`1 source · <name>`) before extracting; processing takes ~a minute even
   for short videos.
4. Extraction: pick the largest text blob counting **both** Gujarati (U+0A80–U+0AFF) and Devanagari
   (U+0900–097F) characters; strip UI lines (`Create an Audio Overview…`). The transcript can arrive
   as 1–2 very long lines — the build model splits it fine.

## 9. Patch ledger (surviving upstream pulls)

Re-apply after an upstream merge if files conflict:

1. `webapp/server.py` — modern `TemplateResponse` signature.
2. `skill/scripts/build_doc.py` — box/verse/stats duplication fix; Devanagari font stacks; variant normalisation.
3. `pipeline.py` — `--src-name` passthrough (A5 title numbering) + exact skip-check.
4. `openrouter.py` (new) + `pipeline.py`/`webapp`/`pyproject`/`requirements` wiring.
5. `run-web.sh` (new).

## 10. Quick verification (no credentials needed)

- Typeset smoke: write a small fragment (h1/h2/p/p.q/div.verse/div.box/p.end), then
  `python skill/scripts/build_doc.py frag.html --out OUT --format both --number 1`;
  expect rc=0, `text-integrity (DOCX): PASS`, box string exactly once in `word/document.xml`.
- Consensus dry-run on a kept fragment: `openrouter.contract_issues(fragment, transcript)` must
  return issues with `severity != "error"` only.
- Dashboard: `curl -s http://127.0.0.1:8765/` returns 200 and shows any env warnings; `/api/jobs` returns JSON.
