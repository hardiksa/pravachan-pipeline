#!/usr/bin/env python3
"""
OpenRouter client for the pravachan build stage + cross-model consensus verification.

The build stage generates the typeset HTML fragment with an OpenRouter model (default:
deepseek/deepseek-v4.1-flash — 1M context, ~30x cheaper than Claude Opus) and, when
consensus is enabled, has the fragment verified INDEPENDENTLY by two models from two
other vendors (default: z-ai/glm-5.3 and qwen/qwen3.8-flash). Deterministic, model-free
contract checks run alongside. A bounded fix loop applies the findings; anything still
failing is written to a .review.json next to the output and logged loudly, so a human
knows exactly where to look.

Verifier reasoning is capped per model (GLM defaults to effort "max" — without a cap it
can burn the whole token budget thinking and return no verdict; Qwen supports a
max_tokens thinking budget instead of effort levels).

Env:
    OPENROUTER_API_KEY          required (sk-or-...)
    PRAVACHAN_BUILD_MODEL       build/generation model  (default: deepseek/deepseek-v4.1-flash)
    PRAVACHAN_VERIFY_MODELS     comma list, other vendors (default: z-ai/glm-5.3,qwen/qwen3.8-flash)
    PRAVACHAN_CONSENSUS_ROUNDS  max fix rounds          (default: 2)
    PRAVACHAN_VERIFY_REASONING  JSON: per-model reasoning config override, e.g.
                                '{"qwen/qwen3.8-flash": {"max_tokens": 8000}, "*": {"effort": "low"}}'
    PRAVACHAN_VERIFY_MAX_TOKENS verifier max output tokens (default: 24000)
    OPENROUTER_BASE_URL         default https://openrouter.ai/api/v1

CLI (smoke test):
    python openrouter.py --model deepseek/deepseek-v4.1-flash "reply with OK"
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")

DEFAULT_BUILD_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_VERIFY_MODELS = "z-ai/glm-5.3,qwen/qwen3.8-flash"
DEFAULT_CONSENSUS_ROUNDS = 2
DEFAULT_VERIFY_MAX_TOKENS = 24000
# per-model reasoning caps (OpenRouter `reasoning` param); "*" = fallback for other models
DEFAULT_VERIFY_REASONING: dict = {
    "z-ai/glm-5.3": {"effort": "low"},           # supported_efforts: max|high|low (default: max!)
    "qwen/qwen3.8-flash": {"max_tokens": 8000},  # supports_max_tokens, ignores effort
    "*": {"effort": "low"},
}
MAX_TOKENS = 32000
REQUIRED_CHECKS = 6

ALLOWED_TAGS = {"h1", "h2", "p", "div", "br", "b", "strong", "i", "em"}


class OpenRouterError(RuntimeError):
    pass


class OpenRouterLengthError(OpenRouterError):
    """Completion hit max_tokens before producing content — deterministic, do not retry."""


# ── config ────────────────────────────────────────────────────────────────────
def build_model() -> str:
    return os.environ.get("PRAVACHAN_BUILD_MODEL", DEFAULT_BUILD_MODEL)


def verify_models() -> list[str]:
    raw = os.environ.get("PRAVACHAN_VERIFY_MODELS", DEFAULT_VERIFY_MODELS)
    return [m.strip() for m in raw.split(",") if m.strip()]


def consensus_rounds() -> int:
    try:
        return max(0, int(os.environ.get("PRAVACHAN_CONSENSUS_ROUNDS", DEFAULT_CONSENSUS_ROUNDS)))
    except ValueError:
        return DEFAULT_CONSENSUS_ROUNDS


def verify_reasoning(model: str) -> dict | None:
    """Reasoning config for a verifier model: env JSON override, else built-in defaults."""
    raw = os.environ.get("PRAVACHAN_VERIFY_REASONING", "").strip()
    cfg = None
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                cfg = parsed.get(model, parsed.get("*"))
        except json.JSONDecodeError:
            cfg = None
    if cfg is None:
        cfg = DEFAULT_VERIFY_REASONING.get(model, DEFAULT_VERIFY_REASONING.get("*"))
    return dict(cfg) if cfg else None


def verify_max_tokens() -> int:
    try:
        return max(1000, int(os.environ.get("PRAVACHAN_VERIFY_MAX_TOKENS", DEFAULT_VERIFY_MAX_TOKENS)))
    except ValueError:
        return DEFAULT_VERIFY_MAX_TOKENS


def api_key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not k:
        raise OpenRouterError("OPENROUTER_API_KEY is not set — export your OpenRouter key (sk-or-...)")
    return k


# ── client ────────────────────────────────────────────────────────────────────
def chat(model: str, messages: list[dict], max_tokens: int = MAX_TOKENS,
         temperature: float = 0.2, timeout: int = 1800, retries: int = 2,
         reasoning: dict | None = None) -> tuple[str, dict]:
    """One streaming chat completion via OpenRouter. Returns (text, usage).

    `reasoning` is the OpenRouter reasoning config, e.g. {"effort": "low"} or
    {"max_tokens": 8000}. Dropped automatically if the provider rejects it.
    """
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/hardiksa/pravachan-pipeline",
        "X-Title": "pravachan-pipeline",
    }
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "temperature": temperature, "stream": True, "usage": {"include": True}}
    if reasoning:
        payload["reasoning"] = dict(reasoning)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(BASE_URL + "/chat/completions", json=payload, headers=headers,
                              stream=True, timeout=(30, timeout))
            if r.status_code == 400:
                # some models pin temperature and/or reject the reasoning param — drop & retry
                body_l = (r.text or "").lower()
                dropped = []
                for key in ("temperature", "reasoning"):
                    if key in body_l and key in payload:
                        payload.pop(key, None)
                        dropped.append(key)
                if dropped:
                    r.close()
                    r = requests.post(BASE_URL + "/chat/completions", json=payload, headers=headers,
                                      stream=True, timeout=(30, timeout))
            if r.status_code >= 400:
                body = (r.text or "")[:500]
                r.close()
                raise OpenRouterError(f"HTTP {r.status_code} from OpenRouter ({model}): {body}")
            text: list[str] = []
            usage: dict = {}
            finish = None
            raw_acc: list[str] = []
            for raw_line in r.iter_lines():
                if not raw_line:
                    continue
                # SSE is always UTF-8; requests would default to ISO-8859-1 for a
                # text/event-stream response without charset (corrupts Gujarati text)
                line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, bytes) else raw_line
                raw_acc.append(line)
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        text.append(piece)
            r.close()
            if not text:
                # some providers ignore stream:true and return one JSON body — accept it
                try:
                    body = json.loads("".join(raw_acc).strip() or "{}")
                    choice = (body.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    if msg.get("content"):
                        text.append(msg["content"])
                    usage = body.get("usage") or usage
                    finish = choice.get("finish_reason") or finish
                except json.JSONDecodeError:
                    pass
            out = "".join(text)
            if not out.strip():
                if finish == "length":
                    raise OpenRouterLengthError(
                        f"{model}: no content — completion hit max_tokens={max_tokens} "
                        f"(finish_reason=length; reasoning likely consumed the budget). "
                        f"Cap reasoning (PRAVACHAN_VERIFY_REASONING) or raise max_tokens.")
                raise OpenRouterError(
                    f"empty completion from {model} (finish_reason={finish}; first bytes: "
                    f"{''.join(raw_acc)[:200]!r})")
            return out, usage
        except OpenRouterLengthError:
            raise
        except (requests.RequestException, OpenRouterError) as e:
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise OpenRouterError(f"OpenRouter call failed for {model}: {last}")


def _acc(stats: dict, usage: dict) -> None:
    stats["calls"] = stats.get("calls", 0) + 1
    stats["prompt_tokens"] = stats.get("prompt_tokens", 0) + int(usage.get("prompt_tokens") or 0)
    stats["completion_tokens"] = stats.get("completion_tokens", 0) + int(usage.get("completion_tokens") or 0)
    stats["reasoning_tokens"] = stats.get("reasoning_tokens", 0) + int(
        (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)


# ── fragment hygiene ──────────────────────────────────────────────────────────
def strip_to_fragment(s: str) -> str:
    """Drop markdown fences / preamble; keep from the first <h1> on."""
    s = (s or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\n", "", s)
    s = re.sub(r"\n```$", "", s)
    i = s.find("<h1")
    return s[i:] if i != -1 else s


def _parse_json(s: str) -> dict | None:
    s = (s or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\n", "", s)
    s = re.sub(r"\n```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None


# ── deterministic (model-free) contract checks ────────────────────────────────
_TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>|<p[^>]*class=\"sub\"[^>]*>(.*?)</p>", re.S | re.I)


def contract_issues(fragment: str, transcript: str = "") -> list[dict]:
    """Model-free checks on a candidate fragment. Issue = {severity, check, detail}."""
    issues: list[dict] = []
    f = (fragment or "").strip()
    if not f:
        return [dict(severity="error", check="contract.empty", detail="empty fragment")]
    if not f.startswith("<h1"):
        issues.append(dict(severity="error", check="contract.h1", detail="fragment must start with <h1>"))
    if "```" in f:
        issues.append(dict(severity="error", check="contract.fences", detail="markdown code fences present"))
    for m in _TITLE_RE.finditer(f):
        seg = (m.group(1) or m.group(2) or "").strip()
        if re.search(r"[0-9]", seg):
            issues.append(dict(severity="error", check="numerals.title",
                               detail=f"Latin digit in title/subtitle: {seg[:90]!r}"))
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(f, "html.parser")
        bad = sorted({t.name for t in soup.find_all(True)} - ALLOWED_TAGS)
        if bad:
            issues.append(dict(severity="error", check="contract.tags",
                               detail=f"non-whitelisted tags: {bad}"))
        mm = soup.find("div", class_="mindmap")
        if mm is None:
            issues.append(dict(severity="error", check="structure.mindmap",
                               detail="missing વિચારનકશો mindmap block"))
        else:
            branches = mm.find_all("div", class_="branch", recursive=False)
            if len(branches) != 5:
                issues.append(dict(severity="error", check="structure.mindmap",
                                   detail=f"mindmap has {len(branches)} branches (need exactly 5)"))
            for bi, b in enumerate(branches, 1):
                leaves = b.find_all("div", class_="leaf", recursive=False)
                if len(leaves) != 3:
                    issues.append(dict(severity="error", check="structure.mindmap",
                                       detail=f"mindmap branch {bi} has {len(leaves)} leaves (need exactly 3)"))
        sm = soup.find("div", class_="summary")
        if sm is None:
            issues.append(dict(severity="error", check="structure.summary",
                               detail="missing સારાંશ summary block"))
        else:
            if not sm.find("div", class_="stats"):
                issues.append(dict(severity="warning", check="structure.stats",
                                   detail="summary has no stats box (એક નજરે મુખ્ય આંકડા)"))
            n_paras = len(sm.find_all("p", recursive=True))
            if n_paras < 3:
                issues.append(dict(severity="warning", check="structure.summary",
                                   detail=f"summary has only {n_paras} paragraph(s) (want ~5)"))
        for h2 in soup.find_all("h2"):
            if re.search(r"[0-9]", h2.get_text()):
                issues.append(dict(severity="warning", check="numerals.h2",
                                   detail=f"Latin digit in section heading: {h2.get_text(strip=True)[:60]!r}"))
        body_text = soup.get_text(" ", strip=True)
    except Exception as e:  # noqa: BLE001 — any parse failure is a contract issue
        issues.append(dict(severity="error", check="contract.parse", detail=f"fragment not parseable: {e!r}"))
        return issues
    if not re.search(r"</(div|p|h2|h1)>\s*$", f):
        issues.append(dict(severity="error", check="contract.truncated",
                           detail="fragment does not end with a closing tag (truncated?)"))
    if transcript:
        tr = len(re.sub(r"\s+", "", transcript))
        ft = len(re.sub(r"\s+", "", body_text))
        if tr and ft < 0.35 * tr:
            issues.append(dict(severity="error", check="fidelity.length",
                               detail=f"fragment text {ft} chars vs transcript {tr} — over 65% of content missing"))
        elif tr and ft < 0.55 * tr:
            issues.append(dict(severity="warning", check="fidelity.length",
                               detail=f"fragment text {ft} chars vs transcript {tr} — possible content loss"))
    return issues


# ── LLM verification ──────────────────────────────────────────────────────────
VERIFIER_SYSTEM = """You are an independent verifier for a Jain pravachan typesetting pipeline. \
A cheaper model has converted a raw Gujarati ASR transcript into a structured HTML fragment for a \
print-ready A5 book. You receive the RAW TRANSCRIPT and the CANDIDATE FRAGMENT. Your job is to catch \
anything that would embarrass the publisher. You are adversarial but precise: report only issues you \
can point to with evidence from the two texts.

Perform EXACTLY these {required} checks and report how many you actually performed:

1. contract — fragment starts with <h1>; uses only allowed elements (h1 h2 p div br b strong i em); \
no markdown fences; not truncated mid-tag.
2. numerals — every number and date in the title/subtitle is in Gujarati numerals (૦૧૨૩૪૫૬૭૮૯), \
never Latin digits.
3. gatha_fidelity — every verse block (div.verse) matches something actually said in the transcript \
(gathas, sutra fragments, stavans). Flag any verse that looks invented, embellished, or untraceable.
4. corrections — garbled Jain technical terms that clearly occur in the transcript (see the corrections \
glossary below) have been corrected in the fragment. Flag prominent glossary terms left uncorrected.
5. fidelity — no invented content (claims, names, numbers, sections the transcript does not support), \
and no substantial talk content silently dropped; summary must reflect the actual talk.
6. structure — the fragment contains a સારાંશ summary (~5 paragraphs + stats box), a વિચારનકશો mindmap \
(exactly 5 branches × 3 leaves covering the numbered sections), numbered sections in Gujarati numerals, \
and verse/box/q-line elements used per the format contract.

Rules:
- Do NOT flag: English loanwords, colloquialisms, audience names, the speaker's asides, or his arithmetic \
as spoken — the pipeline deliberately preserves those.
- The title/subtitle (h1, and p.sub such as "વ્યાખ્યાન ૧ · ૨૧-૮-૨૫") is filled in from the SOURCE \
FILENAME per the pipeline's title convention — do NOT flag filename-derived title/subtitle metadata as \
invented; only check its numerals.
- The સારાંશ and વિચારનકશો section headings are rendered by the layout script — do not require heading \
elements for them in the fragment.
- Every issue must cite evidence: quote the fragment text (and transcript text where applicable).
- severity "error" = must fix before print; "warning" = a human should glance at it.

Reply with ONLY this JSON (no fences, no commentary):
{{"checks_performed": <int, how many of the {required} checks you ACTUALLY performed>, \
"pass": <true iff there are no error-level issues>, \
"issues": [{{"severity": "error"|"warning", "check": "<one of the six check names>", \
"detail": "<what is wrong, with quoted evidence>", "fix": "<concrete instruction to fix>"}}]}}"""

FIX_SYSTEM = """You repair an HTML fragment for a Jain pravachan A5 book. You receive the raw transcript, \
the current fragment, and issues found by independent verifiers. Return the FULL corrected fragment — \
same contract as before: starts with <h1>, only allowed elements (h1 h2 p div br b strong i em), no \
markdown fences, no commentary before or after. Keep everything already correct; change only what the \
issues require. Never invent content that is not in the transcript. Never remove, shorten or reword the \
talk text. Gujarati numerals only in the title/subtitle. The title/subtitle metadata derives from the \
source filename — keep it as is unless the issues say it is malformed. Never insert placeholders like \
[તારીખ અહીં ઉમેરો] — if a value is uncertain, keep the original filename-derived value."""


def verifier_references(skill_dir: Path) -> str:
    """Corrections + structure references for the verifier's system prompt."""
    parts = []
    for name in ("corrections.md", "structure.md"):
        p = Path(skill_dir) / "references" / name
        if p.exists():
            parts.append(f"# references/{name}\n" + p.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def _verdict(model: str, raw: str) -> dict | None:
    v = _parse_json(raw)
    if v is None:
        return None
    v["model"] = model
    try:
        v["checks_performed"] = int(v.get("checks_performed", 0))
    except (TypeError, ValueError):
        v["checks_performed"] = 0
    if not isinstance(v.get("issues"), list):
        v["issues"] = []
    v["pass"] = bool(v.get("pass")) and not any(
        i.get("severity") == "error" for i in v["issues"] if isinstance(i, dict))
    return v


def verify_fragment(model: str, transcript: str, fragment: str, references: str,
                    stats: dict | None = None) -> dict:
    """Independent verification by `model`. Invalid/failed verifications return
    pass=False with invalid=True (fail-closed)."""
    system = VERIFIER_SYSTEM.format(required=REQUIRED_CHECKS) + "\n\n" + references
    user = "TRANSCRIPT (raw ASR):\n" + transcript + "\n\nCANDIDATE FRAGMENT:\n" + fragment

    def invalid(why: str) -> dict:
        v = dict(checks_performed=0, invalid=True, model=model,
                 issues=[dict(severity="error", check="verify.invalid", detail=why)])
        v["pass"] = False
        return v

    for attempt in range(2):
        try:
            raw, usage = chat(model, [{"role": "system", "content": system},
                                      {"role": "user", "content": user}],
                              max_tokens=verify_max_tokens(), temperature=0.0,
                              reasoning=verify_reasoning(model))
            if stats is not None:
                _acc(stats, usage)
        except OpenRouterError as e:
            return invalid(f"verifier call failed: {e}")
        v = _verdict(model, raw)
        if v is None:
            user += "\n\nNOTE: your previous reply was not valid JSON. Reply with ONLY the JSON object."
            continue
        if v["checks_performed"] >= REQUIRED_CHECKS:
            return v
        user += (f"\n\nNOTE: you reported checks_performed={v['checks_performed']} but all "
                 f"{REQUIRED_CHECKS} required checks must actually be performed. Re-verify and reply "
                 "with the full JSON.")
    return invalid("verifier did not return a valid full verdict (JSON/checks) after 2 attempts")


def fix_fragment(model: str, transcript: str, fragment: str, issues: list[dict],
                 stats: dict | None = None) -> str:
    user = ("TRANSCRIPT (raw ASR):\n" + transcript + "\n\nCURRENT FRAGMENT:\n" + fragment +
            "\n\nISSUES TO FIX (JSON):\n" + json.dumps(issues, ensure_ascii=False, indent=1))
    raw, usage = chat(model, [{"role": "system", "content": FIX_SYSTEM},
                              {"role": "user", "content": user}], max_tokens=MAX_TOKENS, temperature=0.2)
    if stats is not None:
        _acc(stats, usage)
    return strip_to_fragment(raw)


# ── consensus orchestration ───────────────────────────────────────────────────
def consensus_build(transcript: str, fragment: str, references: str, gen_model: str,
                    verify_model_list: list[str], rounds: int, log=print) -> tuple[str, dict]:
    """Verify `fragment` with every verifier model (in parallel); apply up to `rounds`
    fix rounds.

    Consensus PASS requires: every verifier passes AND performed all required checks,
    and the deterministic contract checks report no errors.
    Returns (final_fragment, report) — report includes full history + token stats.
    """
    stats: dict = {}
    history: list[dict] = []
    verdicts: list[dict] = []
    det: list[dict] = []
    passed = False
    for rnd in range(rounds + 1):
        local_stats = [{} for _ in verify_model_list]
        with ThreadPoolExecutor(max_workers=max(1, len(verify_model_list))) as ex:
            verdicts = list(ex.map(
                lambda pair: verify_fragment(pair[0], transcript, fragment, references, pair[1]),
                list(zip(verify_model_list, local_stats))))
        for st in local_stats:
            for k, v in st.items():
                stats[k] = stats.get(k, 0) + int(v)
        det = contract_issues(fragment, transcript)
        errs = [i for v in verdicts for i in (v.get("issues") or []) if i.get("severity") == "error"]
        derrs = [i for i in det if i["severity"] == "error"]
        passed = not errs and not derrs and all(not v.get("invalid") for v in verdicts) \
            and all(v.get("pass") for v in verdicts)
        history.append(dict(round=rnd, verdicts=verdicts, deterministic=det, passed=passed))
        summary = ", ".join(
            f"{v.get('model', '?')}={'PASS' if (v.get('pass') and not v.get('invalid')) else 'FAIL'}"
            f"({v.get('checks_performed', 0)}/{REQUIRED_CHECKS} checks)"
            for v in verdicts)
        log(f"  consensus r{rnd}: {summary}" + (f" · det: {len(derrs)} error(s)" if derrs else ""))
        if passed or rnd == rounds:
            break
        issues = derrs + errs
        warns = [i for v in verdicts for i in (v.get("issues") or []) if i.get("severity") == "warning"]
        log(f"  fixing ({len(issues)} error(s), {len(warns)} warning(s)) via {gen_model} …")
        fragment = fix_fragment(gen_model, transcript, fragment, issues + warns, stats)
    report = dict(passed=passed, rounds=len(history), verify_models=verify_model_list,
                  gen_model=gen_model, stats=stats, history=history)
    return fragment, report


# ── smoke-test CLI ────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="OpenRouter smoke test for pravachan-pipeline")
    ap.add_argument("prompt", nargs="?", default="Reply with exactly: OK")
    ap.add_argument("--model", default=build_model())
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--effort", default=None, help="reasoning effort: low|medium|high|max")
    ap.add_argument("--reasoning-max-tokens", type=int, default=None, help="reasoning token budget")
    args = ap.parse_args()
    reasoning = None
    if args.effort:
        reasoning = {"effort": args.effort}
    if args.reasoning_max_tokens:
        reasoning = {**(reasoning or {}), "max_tokens": args.reasoning_max_tokens}
    text, usage = chat(args.model, [{"role": "user", "content": args.prompt}],
                       max_tokens=args.max_tokens, reasoning=reasoning)
    print(f"model: {args.model}")
    print(f"usage: {usage}")
    print("---- response ----")
    print(text[:2000])


if __name__ == "__main__":
    main()
