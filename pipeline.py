#!/usr/bin/env python3
"""
pravachan-pipeline — Drive folder ➜ audio ➜ transcript ➜ typeset PDF/DOCX.

Give it a Google Drive folder link, a YouTube video/playlist URL, or a local audio
dir. It downloads the audio, transcodes/compresses as needed, transcribes each file
via NotebookLM (Selenium + a dedicated Firefox profile; Google Cloud STT is an optional
fallback), runs the bundled jain-pravachan-transcript skill via an OpenRouter model —
consensus-verified by independent models from other vendors — to clean + structure the
transcript, and typesets the result as PDF + DOCX. Resumable: anything already produced
is skipped.

Transcription requires a one-time NotebookLM login on this machine: `pravachan-nlm-login`.

Usage:
    python pipeline.py <drive_folder_url | local_audio_dir> --out OUT [options]

Stages run in order; use --only to run a subset:
    --only download,transcribe,build

Env:
    OPENROUTER_API_KEY          required for the 'build' stage (OpenRouter key, sk-or-...)
    PRAVACHAN_BUILD_MODEL       build model id       (default: deepseek/deepseek-v4.1-flash)
    PRAVACHAN_VERIFY_MODELS     consensus verifiers  (default: z-ai/glm-5.3,qwen/qwen3.8-flash)
    PRAVACHAN_CONSENSUS_ROUNDS  max fix rounds       (default: 2)
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, tempfile, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import openrouter

HERE = Path(__file__).resolve().parent
SKILL = HERE / "skill"
BUILD_DOC = SKILL / "scripts" / "build_doc.py"

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma", ""}
# "" covers Drive files saved without an extension (common); we sniff those below.

def log(*a): print("[pipeline]", *a, flush=True)

# ── audio detection ───────────────────────────────────────────────────────────
def is_audio(p: Path) -> bool:
    if p.suffix.lower() in AUDIO_EXTS - {""}:
        return True
    if p.suffix == "":  # sniff extension-less files
        try:
            head = p.open("rb").read(16)
        except OSError:
            return False
        return (head[4:8] == b"ftyp" or head[:3] == b"ID3" or head[:4] == b"RIFF"
                or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") or head[:4] == b"OggS")
    return False

def find_audio(root: Path):
    return sorted(p for p in root.rglob("*") if p.is_file() and is_audio(p))

# ── stage 1: download ─────────────────────────────────────────────────────────
def download(url: str, audio_dir: Path):
    audio_dir.mkdir(parents=True, exist_ok=True)
    log(f"downloading from Google Drive → {audio_dir}")
    # try a folder first (gdown --folder recurses into nested subfolders and mirrors the tree)
    subprocess.run([sys.executable, "-m", "gdown", "--folder", url, "-O", str(audio_dir),
                    "--remaining-ok"], check=False)
    if not find_audio(audio_dir):
        # then a single-file link — gdown --folder refuses those; plain gdown <url> -O dir/ fetches it
        log("no folder download — trying single-file Drive link")
        subprocess.run([sys.executable, "-m", "gdown", url, "-O", str(audio_dir) + "/"], check=False)
    if not find_audio(audio_dir):
        raise SystemExit("download produced no audio files — check the Drive link's sharing settings")
    log(f"downloaded {len(find_audio(audio_dir))} audio files")

def download_youtube(url: str, audio_dir: Path):
    """Download one video or a whole playlist as MP3 (192k) via yt-dlp."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    log(f"yt-dlp → mp3 → {audio_dir}")
    subprocess.run([sys.executable, "-m", "yt_dlp",
                    "-x", "--audio-format", "mp3", "--audio-quality", "192K",
                    "--embed-metadata", "-o", str(audio_dir / "%(title)s.%(ext)s"), url],
                   check=True)
    log(f"downloaded {len(find_audio(audio_dir))} audio files")

# ── compression: keep every file under NotebookLM/upload caps (compress, don't split)
def compress_oversized(audio_dir: Path, target_mb: int = 190):
    cap = target_mb * 1024 * 1024
    for a in find_audio(audio_dir):
        if a.stat().st_size <= cap:
            continue
        dur = 1.0
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "csv=p=0", str(a)], capture_output=True, text=True)
            dur = max(1.0, float(out.stdout.strip() or 1))
        except Exception:
            pass
        br = int((target_mb * 8 * 1024 * 1024) / (dur * 1000) * 0.92)
        br = max(32, min(128, br))
        keep = audio_dir / "_oversized"; keep.mkdir(exist_ok=True)
        if not (keep / a.name).exists():
            shutil.copy2(a, keep / a.name)
        tmp = a.with_suffix(".cmp.mp3")
        log(f"compressing {a.name} ({a.stat().st_size//1048576}MB) → mono {br}k mp3")
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(a), "-vn",
                            "-ac", "1", "-acodec", "libmp3lame", "-b:a", f"{br}k", str(tmp)])
        if r.returncode == 0:
            # replace in place, keeping an .mp3 name so downstream naming is stable
            target = a.with_suffix(".mp3")
            tmp.replace(target)
            if target != a and a.exists():
                a.replace(keep / (a.name + ".orig"))
            log(f"  → {target.name} {target.stat().st_size//1048576}MB")
        else:
            tmp.unlink(missing_ok=True); log(f"  compress failed for {a.name}")

# ── stage 2: transcribe (NotebookLM primary; Google STT optional fallback) ─────
def transcribe_all(audio_dir: Path, trans_dir: Path, transcriber: str, lang: str,
                   nlm_profile: str, nlm_chunk: int):
    trans_dir.mkdir(parents=True, exist_ok=True)
    if transcriber == "notebooklm":
        import nlm                                   # Selenium + dedicated Firefox profile
        nlm.transcribe_dir(audio_dir, trans_dir, nlm_profile, nlm_chunk)
    elif transcriber == "google":
        import gstt                                  # Google Cloud Speech-to-Text (gu-IN)
        gstt.transcribe_dir(audio_dir, trans_dir, lang)
    else:
        raise SystemExit(f"unknown transcriber {transcriber!r} (use notebooklm|google)")

# ── stage 3: skill (OpenRouter + consensus) → HTML → PDF/DOCX ─────────────────
# Title conventions per set type. `{N}` and `{date}` are derived by the model from the
# transcript FILENAME (leading number, trailing date), rendered in GUJARATI NUMERALS.
TITLE_SPECS = {
    "topic":
        "TITLE: `<h1>` = a faithful Gujarati topic title drawn from THIS talk's own content. "
        "NO speaker name, NO 'Day N'/'વ્યાખ્યાન N'. `<p class=\"sub\">` = the DATE ONLY, in "
        "Gujarati numerals (e.g. ૨૧-૮-૨૫). If no date is in the filename, omit the subtitle.",
    "numbered":
        "TITLE: `<h1>` = the discourse/series' Gujarati name or its core subject. "
        "`<p class=\"sub\">` = `વ્યાખ્યાન <N> · <date>` where <N> is the leading number from the "
        "filename and <date> the filename date — BOTH in Gujarati numerals (with '-' between date parts).",
    "granth":
        "TITLE: `<h1>` = the granth's proper Gujarati name (translate/transliterate the English "
        "name in the filename). `<p class=\"sub\">` = `ગ્રંથ પરિચય · વ્યાખ્યાન <N> · <date>` with "
        "<N> (leading filename number) and <date> (filename date) BOTH in Gujarati numerals.",
    "day":
        "TITLE: `<h1>` = the fixed set title (e.g. `ધ્યાન શિબિર`). `<p class=\"sub\">` = `દિવસ <N>` "
        "where <N> is the day number from the filename, in Gujarati numerals. Add ` · <date>` "
        "(Gujarati numerals) only if the filename carries a date.",
    "auto":
        "TITLE: choose the most faithful `<h1>` from the talk's content and a `<p class=\"sub\">` "
        "subtitle appropriate to it; render any number/date in Gujarati numerals.",
}

def skill_system_prompt(title_style: str = "auto") -> str:
    title = TITLE_SPECS.get(title_style, TITLE_SPECS["auto"])
    parts = [
        (SKILL / "SKILL.md").read_text(encoding="utf-8"),
        "\n\n# references/corrections.md\n" + (SKILL / "references/corrections.md").read_text(encoding="utf-8"),
        "\n\n# references/structure.md\n" + (SKILL / "references/structure.md").read_text(encoding="utf-8"),
        "\n\n# TITLE CONVENTION\n" + title +
        "\n⚠️ Never use Latin digits in the title/subtitle — Gujarati numerals (૦૧૨૩૪૫૬૭૮૯) only.",
        "\n\n# OUTPUT CONTRACT\n"
        "You are running Stage 1 (clean) + Stage 2 (structure) of the skill on the transcript "
        "in the user message. Return ONLY the HTML fragment described in the skill — starting "
        "with <h1> and using only the allowed elements. No markdown, no ``` fences, no commentary "
        "before or after. Drop any trailing 'Create an Audio Overview…' UI line and any spillover "
        "into a different discourse.",
    ]
    return "".join(parts)

def build_one(txt: Path, trans_dir: Path, pdf_dir: Path, model: str, fmt: str, columns: int,
              size: int, portrait: bool, title_style: str = "auto",
              verify_model_list: list[str] | None = None, rounds: int = 0):
    rel = txt.relative_to(trans_dir)
    base = rel.with_suffix("")            # strip the ".txt"
    if base.suffix == "":                 # original had no ext (e.g. "1. …")
        base = rel.parent / rel.stem
    out_base = pdf_dir / base
    if (out_base.parent / (out_base.name + ".pdf")).exists():   # exactly what build_doc writes
        return "skip"
    out_base.parent.mkdir(parents=True, exist_ok=True)

    transcript = txt.read_text(encoding="utf-8")
    messages = [
        {"role": "system", "content": skill_system_prompt(title_style)},
        {"role": "user", "content": f"Transcript filename: {txt.name}\n\nTRANSCRIPT:\n{transcript}"},
    ]
    html, usage = openrouter.chat(model, messages, max_tokens=32000)
    html = openrouter.strip_to_fragment(html)
    log(f"  generated ← {txt.name} ({model}, {usage.get('completion_tokens', '?')} out tok)")

    report = None
    if verify_model_list:
        html, report = openrouter.consensus_build(
            transcript, html, openrouter.verifier_references(SKILL),
            model, verify_model_list, rounds, log=log)
        st = report.get("stats", {})
        log(f"  consensus ← {txt.name}: {'PASS' if report['passed'] else 'FLAGGED'}"
            f" ({report['rounds']} round(s), {st.get('prompt_tokens', 0)} in / "
            f"{st.get('completion_tokens', 0)} out tok)")
        if not report["passed"]:
            flag = out_base.parent / (out_base.name + ".review.json")
            flag.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
            log(f"  ⚠ consensus not passed for {txt.name} — wrote {flag.name} for human review")

    if os.environ.get("PRAVACHAN_KEEP_FRAGMENT"):
        keep = out_base.parent / (out_base.name + ".fragment.html")
        keep.write_text(html, encoding="utf-8")
        log(f"  kept fragment ← {keep.name}")

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fp:
        fp.write(html); frag = fp.name
    try:
        cmd = [sys.executable, str(BUILD_DOC), frag, "--out", str(out_base),
               "--format", fmt, "--columns", str(columns), "--size", str(size),
               "--src-name", txt.name]
        if portrait:
            cmd.append("--portrait")
        subprocess.run(cmd, check=True)
    finally:
        os.unlink(frag)
    return "done" if (report is None or report["passed"]) else "done ⚠flagged"

def build_all(trans_dir: Path, pdf_dir: Path, model: str, fmt: str, columns: int, size: int,
              portrait: bool, workers: int, title_style: str = "auto",
              verify_model_list: list[str] | None = None, rounds: int = 0):
    jobs = sorted(trans_dir.rglob("*.txt"))
    consensus = f", consensus via {','.join(verify_model_list)}" if verify_model_list else ""
    log(f"building {len(jobs)} document(s) via {model} (workers={workers}, title={title_style}{consensus})")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(build_one, t, trans_dir, pdf_dir, model, fmt, columns, size, portrait,
                          title_style, verify_model_list, rounds): t
                for t in jobs}
        for f in as_completed(futs):
            t = futs[f]
            try:
                log(f"  {f.result()} ← {t.name}")
            except Exception as e:
                log(f"  build ✗ {t.name}: {e!r}")

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="Google Drive folder URL, a YouTube video/playlist URL, or a local audio dir")
    ap.add_argument("--out", required=True, help="output root (creates audio/ transcripts/ pdfs/)")
    ap.add_argument("--source-type", default="auto", choices=["auto", "drive", "youtube", "local"],
                    help="how to fetch the source (default: auto-detect from the URL)")
    ap.add_argument("--title-style", default="auto", choices=list(TITLE_SPECS),
                    help="title convention: topic | numbered | granth | day | auto")
    ap.add_argument("--target-mb", type=int, default=190,
                    help="compress any audio larger than this (MB) instead of splitting")
    ap.add_argument("--only", default="download,transcribe,build",
                    help="comma list of stages to run (default: all)")
    ap.add_argument("--workers", type=int, default=4, help="parallel build workers (typeset stage)")
    ap.add_argument("--model", default=None,
                    help="OpenRouter model for the build stage "
                         "(default: $PRAVACHAN_BUILD_MODEL or deepseek/deepseek-v4.1-flash)")
    ap.add_argument("--verify-models", default="",
                    help="comma list of OpenRouter models for consensus verification, from vendors "
                         "other than the build model (default: $PRAVACHAN_VERIFY_MODELS or "
                         "z-ai/glm-5.3,qwen/qwen3.8-flash)")
    ap.add_argument("--no-consensus", action="store_true",
                    help="skip consensus verification (single-model generation only)")
    ap.add_argument("--consensus-rounds", type=int, default=openrouter.consensus_rounds(),
                    help="max fix rounds when verification fails (default 2)")
    ap.add_argument("--transcriber", default="notebooklm", choices=["notebooklm", "google"],
                    help="transcription engine: notebooklm (default) | google (Cloud STT fallback)")
    ap.add_argument("--nlm-profile", default=os.environ.get("PRAVACHAN_NLM_PROFILE",
                    os.path.expanduser("~/.pravachan-notebooklm-firefox")),
                    help="dedicated Firefox automation profile for NotebookLM (log in once via pravachan-nlm-login)")
    ap.add_argument("--nlm-chunk", type=int, default=5, help="files per NotebookLM notebook before rotating")
    ap.add_argument("--lang", default="gu", help="language code for the google transcriber (gu=Gujarati)")
    ap.add_argument("--format", default="both", choices=["pdf", "docx", "both"])
    ap.add_argument("--columns", type=int, default=2, choices=[1, 2, 3])
    ap.add_argument("--size", type=int, default=18)
    ap.add_argument("--portrait", action="store_true")
    args = ap.parse_args()

    model = args.model or openrouter.build_model()
    if args.no_consensus:
        verify_model_list = []
    else:
        verify_model_list = ([m.strip() for m in args.verify_models.split(",") if m.strip()]
                             or openrouter.verify_models())

    out = Path(args.out).expanduser().resolve()
    audio_dir, trans_dir, pdf_dir = out / "audio", out / "transcripts", out / "pdfs"
    stages = {s.strip() for s in args.only.split(",")}

    stype = args.source_type
    if stype == "auto":
        if re.search(r"(youtube\.com|youtu\.be)", args.source):
            stype = "youtube"
        elif re.match(r"https?://", args.source):
            stype = "drive"
        else:
            stype = "local"

    if "download" in stages:
        if stype == "youtube":
            download_youtube(args.source, audio_dir)
        elif stype == "drive":
            download(args.source, audio_dir)
        else:                       # local dir: mirror it into audio/
            src = Path(args.source).expanduser().resolve()
            if src != audio_dir:
                audio_dir.mkdir(parents=True, exist_ok=True)
                for p in find_audio(src):
                    dst = audio_dir / p.relative_to(src)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        shutil.copy2(p, dst)
            log(f"using local audio: {len(find_audio(audio_dir))} files")
        compress_oversized(audio_dir, args.target_mb)

    if "transcribe" in stages:
        transcribe_all(audio_dir, trans_dir, args.transcriber, args.lang,
                       args.nlm_profile, args.nlm_chunk)
    if "build" in stages:
        build_all(trans_dir, pdf_dir, model, args.format, args.columns, args.size,
                  args.portrait, args.workers, args.title_style,
                  verify_model_list, args.consensus_rounds)

    log("done. outputs:")
    log(f"  audio       {audio_dir}")
    log(f"  transcripts {trans_dir}")
    log(f"  pdfs        {pdf_dir}")

if __name__ == "__main__":
    main()
