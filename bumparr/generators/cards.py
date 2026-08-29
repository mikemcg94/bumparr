"""Text-card bumper generator.

Bumparr ships the card CONCEPTS and prompts; the USER supplies the model that
writes them, via any OpenAI-compatible endpoint they choose — self-hosted or
cloud. No model, no endpoint, and no generated content ships with Bumparr.

Results are cached into the playable registry so playback stays cheap and
deterministic. Run on demand or on a schedule, never in the playback path.

Usage:
    python -m bumparr.generators.cards --kind psa    --n 20
    python -m bumparr.generators.cards --kind number --n 20
    python -m bumparr.generators.cards --kind trivia --n 20
"""
import argparse
import json
import os
import re
import time
import urllib.request

from bumparr import config, db
from bumparr.content_filter import weight_for
from bumparr.card_validation import validate_card, verify_fact

PROMPTS = {
    "psa": (
        "You write bumpers for a strange, dry, deadpan TV channel called " + config.BRAND + ". "
        "Generate {n} surreal fake public-service announcements. Each is 1 to 3 very short lines, "
        "understated and a little unsettling, never a joke with a punchline. "
        'Return ONLY a JSON array of objects: [{{"lines": ["line one", "line two"]}}]. No prose.'
    ),
    "trivia": (
        "Generate {n} short trivia questions for a TV bumper (television, film, music, science, geography, "
        "history, technology, weird internet history). "
        'Return ONLY a JSON array: [{{"lines": ["Question text", "A ...", "B ...", "C ..."], "answer": "B ..."}}]. '
        "Keep each option short. No prose."
    ),
    "corrections": (
        "You write bumpers for a dry, deadpan TV channel called " + config.BRAND + ". "
        "Generate {n} fake on-air corrections to things never actually stated: retractions "
        "of claims nobody made, clarifications that clarify nothing. Understated, 1 to 3 "
        "very short lines, never a punchline. "
        'Return ONLY a JSON array: [{{"lines": ["We regret the error.", "..."]}}]. No prose.'
    ),
    "achievements": (
        "Generate {n} mock achievement unlocks for watching television, in the style of a "
        "game notification but wry and slightly sad. Two lines: a title, then a one-line "
        "description of the trivial feat. "
        'Return ONLY a JSON array: [{{"lines": ["Still Awake", "You outlasted the last commercial."]}}]. No prose.'
    ),
    "coming_up": (
        "Generate {n} fake 'coming up later' teasers for programmes that will never air on a "
        "channel called " + config.BRAND + ". Plausible-sounding but quietly absurd, 1 to 2 "
        "short lines, delivered straight. "
        'Return ONLY a JSON array: [{{"lines": ["Coming up: a man reads a map.", "Later: he folds it."]}}]. No prose.'
    ),
    "tiny_games": (
        "Generate {n} tiny two-option guessing games for a TV bumper: a short question "
        "followed by exactly two candidate answers, then the answer. Light, odd, quick. "
        'Return ONLY a JSON array: [{{"lines": ["Which came first?", "An egg", "A chicken"], "answer": "An egg"}}]. No prose.'
    ),
}

DEFAULT_WEIGHT = {"psa": 0.7, "trivia": 0.8, "corrections": 0.6,
                  "achievements": 0.7, "coming_up": 0.7,
                  "tiny_games": 0.8}
REVEAL_AFTER = {"number": 5, "trivia": 9, "tiny_games": 8}


class NoModelConfigured(RuntimeError):
    """Raised when card generation is attempted with no inference endpoint set.

    Card generation is OPTIONAL: Bumparr ships the card CONCEPTS, and the user
    supplies the model that writes them — local or cloud, their choice. Failing
    with a clear message beats emitting a malformed request to an empty URL.
    """


def _require_model():
    if not config.LOCAL_LLM_BASE:
        raise NoModelConfigured(
            "No inference endpoint configured. Set LLM_BASE to your own "
            "OpenAI-compatible endpoint (e.g. http://localhost:8080/v1) and "
            "LLM_MODEL to the model name. Bumparr does not provide a model."
        )


def _call_model(prompt: str, timeout: int = 180,
                temperature: float = 0.8, max_tokens: int = 2048) -> str:
    _require_model()
    payload = {
        "model": config.LOCAL_LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # Qwen reasoning models spend the whole token budget on hidden thinking and
    # return empty content unless this is off. It is provider-specific, so it is
    # opt-in rather than sent to every endpoint the user might point us at —
    # some providers reject unknown fields outright.
    if config.env("LLM_DISABLE_THINKING", "").lower() in ("1", "true", "yes"):
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(config.LOCAL_LLM_BASE.rstrip("/") + "/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"]


def _repair_json(blob: str) -> str:
    """Patch the JSON mistakes small local models actually make.

    Observed from a real run: `{"lines": ["a", "b"}` — the inner array is opened
    and never closed. Strict parsing threw the whole batch away even though every
    item was usable, which is the worst possible failure for a generator whose
    output cost real inference time.
    """
    # Close an inner array that runs straight into the object's closing brace.
    blob = re.sub(r'("\s*)\}', r'\1]}', blob) if '"]}' not in blob else blob
    # Trailing commas before a closing bracket or brace.
    blob = re.sub(r",\s*([\]}])", r"\1", blob)
    return blob


def _split_lines(obj):
    """Normalise a `lines` value into separate on-screen lines.

    Models often return one string with embedded newlines instead of an array,
    which renders as a single run-on line. Splitting keeps the card's intended
    shape without rejecting the item.
    """
    v = obj.get("lines")
    if isinstance(v, str):
        return [x.strip() for x in v.split("\n") if x.strip()]
    if isinstance(v, list):
        out = []
        for x in v:
            out += [y.strip() for y in str(x).split("\n") if y.strip()]
        return out
    return []


def _extract_array(text: str):
    """Pull the first JSON array out of a possibly-chatty model response.

    Tries strict parsing first, then a repair pass, then per-object salvage —
    one malformed entry should cost that entry, not the whole generation.
    """
    m = re.search(r"\[.*\]", text, re.DOTALL)
    blob = m.group(0) if m else text
    for candidate in (blob, _repair_json(blob)):
        try:
            arr = json.loads(candidate)
            if isinstance(arr, list) and arr:
                return arr
        except Exception:
            pass
    # Last resort: salvage whatever individual objects still parse.
    out = []
    for chunk in re.findall(r"\{[^{}]*\}?", blob, re.DOTALL):
        for cand in (chunk, _repair_json(chunk), chunk.rstrip().rstrip(",") + "]}"):
            try:
                o = json.loads(cand)
                if isinstance(o, dict) and o:
                    out.append(o)
                    break
            except Exception:
                continue
    return out


def _verifier(prompt: str) -> str:
    return _call_model(prompt, timeout=60, temperature=0.0, max_tokens=8)


def generate(kind: str, n: int, verify: bool = None) -> tuple:
    if kind not in PROMPTS:
        raise SystemExit(f"unknown kind: {kind} (choose from {list(PROMPTS)})")
    # A model cannot reliably INVENT true obscure numbers, so those get a cold
    # fact re-check by default. Purely additive: no model, no verification.
    if verify is None:
        verify = kind == "number"
    raw = _call_model(PROMPTS[kind].format(n=n))
    items = _extract_array(raw)
    added = rejected = 0
    with db.conn() as c:
        for i, obj in enumerate(items):
            if not isinstance(obj, dict):
                continue
            if obj.get("lines") is not None:
                obj["lines"] = _split_lines(obj)
            clean, reason = validate_card(kind, obj)
            if clean is None:
                rejected += 1
                continue
            obj = clean
            if verify and kind in ("number", "trivia"):
                ok, _r = verify_fact(kind, obj, _verifier)
                if not ok:
                    rejected += 1
                    continue
            payload = {}
            if kind == "number":
                if not obj.get("number") or not obj.get("meaning"):
                    continue
                payload = {"number": str(obj["number"]), "meaning": str(obj["meaning"]),
                           "reveal_after": REVEAL_AFTER["number"]}
                title = str(obj["number"])
            elif kind in ("trivia", "tiny_games"):
                lines = obj["lines"]
                payload = {"lines": lines,
                           "answer": str(obj.get("answer", "")),
                           "reveal_after": REVEAL_AFTER.get(kind, 8)}
                title = str(lines[0])[:80]
            else:  # every remaining kind is plain lines on screen
                lines = obj["lines"]
                payload = {"lines": lines}
                title = str(lines[0])[:80]
            # Tone policy: grim cards are kept but heavily down-weighted
            # (rare), never banned.
            card_text = (" ".join(str(v) for v in payload.values() if isinstance(v, str)) +
                         " " + " ".join(payload.get("lines", [])))
            weight = weight_for(DEFAULT_WEIGHT.get(kind, 0.7), card_text)
            pid = f"card:{kind}:{int(time.time())}:{i}"
            row = {
                "id": pid, "type": "card", "kind": kind, "source": "generated",
                "uri": None, "duration": config.CARD_DEFAULT_DURATION, "title": title,
                "payload": json.dumps(payload), "weight": weight,
            }
            # upsert_playable ignores duplicate ids; card ids are time-unique so all insert.
            c.execute(
                """INSERT OR IGNORE INTO playables (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,created_at)
                   VALUES (:id,:type,:kind,:source,:uri,:duration,:title,:payload,'',:weight,1,'ok',:created_at)""",
                {**row, "created_at": time.time()},
            )
            added += 1
        c.commit()
    return added, rejected


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=list(PROMPTS))
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--verify", dest="verify", action="store_true", default=None,
                    help="cold model fact-check each card (default: on for number)")
    ap.add_argument("--no-verify", dest="verify", action="store_false",
                    help="skip the fact-check even for number")
    args = ap.parse_args()
    db.init_db()
    got, dropped = generate(args.kind, args.n, verify=args.verify)
    print(f"[cards] cached {got} '{args.kind}' card(s), rejected {dropped} "
          f"via {config.LOCAL_LLM_MODEL}")
