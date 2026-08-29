"""Grounded card generators — real facts from real sources, NOT model invention.

The local model fabricates confidently on factual prompts, which is fine for the
absurd card kinds (PSAs etc.) but wrong for factual ones (trivia, facts). These
generators pull verified content from public APIs so the channel never states a
confident falsehood. The model is not in the loop here at all.

Usage:
    python -m backend.generators.grounded --kind trivia    --n 30
    python -m backend.generators.grounded --kind fun_facts --n 25
"""
import argparse
import hashlib
import html
import json
import os
import re
import time
import urllib.request

from bumparr import config, db
from bumparr.card_validation import validate_card, looks_truncated

UA = {"User-Agent": "bumparr/1.0"}


def _get_json(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25))


def _insert(c, kind, payload, title, weight=0.9):
    clean, reason = validate_card(kind, payload)
    if clean is None:
        print("  reject %s: %s" % (kind, reason))
        return False
    payload = clean
    pj = json.dumps(payload, sort_keys=True)
    pid = "card:%s:%s" % (kind, hashlib.md5(pj.encode()).hexdigest()[:12])
    before = c.total_changes
    c.execute(
        """INSERT OR IGNORE INTO playables
           (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)""",
        (pid, "card", kind, "grounded", None, config.CARD_DEFAULT_DURATION,
         title[:80], pj, "grounded", weight, time.time()),
    )
    return c.total_changes > before


def gen_trivia(n):
    """Verified multiple-choice trivia from the Open Trivia DB (no key)."""
    added = 0
    with db.conn() as c:
        while added < n:
            batch = min(30, n - added)
            try:
                d = _get_json("https://opentdb.com/api.php?amount=%d&type=multiple" % batch)
            except Exception as e:
                print("  opentdb error:", e); break
            for r in d.get("results", []):
                q = html.unescape(r["question"])
                correct = html.unescape(r["correct_answer"])
                opts = [html.unescape(x) for x in r["incorrect_answers"]] + [correct]
                # shuffle deterministically by hashing so the answer isn't always last
                opts.sort(key=lambda s: hashlib.md5((q + s).encode()).hexdigest())
                letters = ["A", "B", "C", "D"][:len(opts)]
                lines = [q] + ["%s  %s" % (l, o) for l, o in zip(letters, opts)]
                ans_letter = letters[opts.index(correct)]
                payload = {"lines": lines, "answer": "%s  %s" % (ans_letter, correct),
                           "reveal_after": 9, "source": "Open Trivia DB"}
                if _insert(c, "trivia", payload, q):
                    added += 1
            c.commit()
            time.sleep(5)  # opentdb rate limit
    return added


_SENT = re.compile(r"[^.!?]*[.!?]")


def _first_sentence(extract):
    """First clean sentence, accumulating fragments so a period inside an
    abbreviation or initial ("Judith C.", "U.S.") does not cut it short."""
    acc = ""
    for frag in _SENT.findall(extract):
        acc = (acc + frag).strip()
        if len(acc) >= 40 and not looks_truncated(acc):
            return acc
    return acc


def gen_fun_facts(n):
    """Real fun facts from Wikipedia random-article summaries (sourced, not invented)."""
    added = 0
    tries = 0
    with db.conn() as c:
        while added < n and tries < n * 4:
            tries += 1
            try:
                d = _get_json("https://en.wikipedia.org/api/rest_v1/page/random/summary")
            except Exception as e:
                print("  wiki error:", e); time.sleep(2); continue
            extract = (d.get("extract") or "").strip()
            title = d.get("title", "")
            # take the first substantial sentence as the fact; skip disambig/list stubs
            fact = _first_sentence(extract)
            if len(fact) < 40 or len(fact) > 240 or "may refer to" in fact.lower():
                continue
            payload = {"lines": [fact], "source": "Wikipedia: " + title}
            if _insert(c, "fun_facts", payload, title):
                added += 1
            time.sleep(1)
    return added


_NUMBER_DATA = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "config_files", "number_facts.json"))


def gen_number(n):
    """True number facts from a vendored, verified dataset. No model, no
    fabrication -- the model-invented number kind produced physically absurd
    values, so numbers are grounded like trivia and fun_facts."""
    import random
    try:
        facts = json.load(open(_NUMBER_DATA, encoding="utf-8"))["facts"]
    except Exception as e:
        print("  number facts unavailable:", e)
        return 0
    random.shuffle(facts)
    added = 0
    with db.conn() as c:
        for f in facts:
            if added >= n:
                break
            payload = {"number": str(f["number"]), "meaning": str(f["meaning"]),
                       "reveal_after": 5}
            if _insert(c, "number", payload, str(f["number"])):
                added += 1
    return added


GENERATORS = {"trivia": gen_trivia, "fun_facts": gen_fun_facts, "number": gen_number}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=list(GENERATORS))
    ap.add_argument("--n", type=int, default=25)
    args = ap.parse_args()
    db.init_db()
    got = GENERATORS[args.kind](args.n)
    print("[grounded] added %d verified '%s' card(s)" % (got, args.kind))
