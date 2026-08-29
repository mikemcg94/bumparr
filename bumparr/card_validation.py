"""Generation-time card validation.

Every generator (model-invented in `generators/cards.py`, grounded in
`generators/grounded.py`, and any future one) runs each candidate card through
`validate_card` before it is inserted. The channel would rather air fewer cards
than air broken ones, so this rejects freely.

It catches the real defect classes seen in the pool:

  - trivia with unlabelled options and a bare-letter answer ("Apple"/"IBM" then
    answer "A") -> repaired to labelled form, or rejected if the answer can't be
    mapped to an option.
  - trivia that is self-answering (the answer text sits in the question).
  - number cards with an empty/placeholder meaning ("... = ...") or a non-number.
  - either/or preference prompts ("Pizza or Tacos?") carrying a definitive
    answer -> the answer is stripped so they air as open prompts.
  - factual prose (fun_facts) truncated mid-sentence ("...Judith C.", "the U.").

It deliberately does NOT judge whether a *well-formed* factual claim is true —
a model cannot invent "genuinely true obscure numeric facts" reliably, so that
is handled separately by `verify_fact` (a cold model re-check) and, better, by
grounding factual kinds against real sources. Absurd/surreal content in the
comedy kinds (psa, corrections, ...) is intended and never flagged here.
"""
import re

LETTERS = ["A", "B", "C", "D", "E", "F"]

# An option that already carries a label: "A  text", "A) text", "A. text".
_LABELLED = re.compile(r"^\s*([A-Fa-f])[\).:]?\s+\S")
# An answer that is just a letter: "A", "b ".
_BARE_LETTER = re.compile(r"^\s*([A-Fa-f])\s*$")
# A preference either/or question: "X or Y?".
_EITHER_OR = re.compile(r"\bor\b.*\?", re.I)
# Strip a leading label off an option to get its bare text.
_STRIP_LABEL = re.compile(r"^\s*[A-Fa-f][\).:]?\s+")

# Comedy kinds where surreal/absurd/unfinished-sounding text is intentional and
# must never be rejected for "reading oddly".
COMEDIC_KINDS = {
    "psa", "corrections", "technical_difficulties", "coming_up",
    "achievements",
}


def looks_truncated(text: str) -> bool:
    """True if prose appears cut off mid-sentence.

    Conservative: only fires on the shapes that actually occur when a source
    extract is sliced at a bad boundary, to avoid rejecting legitimate prose.
    """
    t = (text or "").strip()
    if not t:
        return True
    if t[-1] not in '.!?"”)':          # no terminal punctuation at all
        return True
    if re.search(r"(?:^|\s)[A-Za-z]\.$", t):  # dangling initial: "Judith C." / "the U."
        return True
    if re.search(r"\[[^\]]*\.$", t):          # bracket cut: "... [Cap."
        return True
    if re.search(r"\b(?:is|are|was|were|of|the|and|to|in|by|for)\s+\d+\.$", t):
        return True                            # "...which is 69." dangling number
    return False


def _option_text(opt: str) -> str:
    return _STRIP_LABEL.sub("", opt).strip()


def _validate_trivia(obj):
    lines = [str(x).strip() for x in (obj.get("lines") or []) if str(x).strip()]
    answer = str(obj.get("answer", "")).strip()
    if len(lines) < 3:
        return None, "trivia needs a question and >=2 options"
    question, options = lines[0], lines[1:]
    if len(options) < 2:
        return None, "fewer than 2 options"

    labelled = [bool(_LABELLED.match(o)) for o in options]
    if all(labelled):
        pass
    elif not any(labelled):
        options = ["%s  %s" % (LETTERS[i], o) for i, o in enumerate(options)]
    else:
        return None, "options inconsistently labelled"

    # Resolve the answer to an option letter, however it was expressed.
    letter = None
    bm, lm = _BARE_LETTER.match(answer), _LABELLED.match(answer)
    if bm:
        letter = bm.group(1).upper()
    elif lm:
        letter = lm.group(1).upper()
    else:
        for i, o in enumerate(options):
            if answer.lower() == _option_text(o).lower():
                letter = LETTERS[i]
                break
    if letter is None or LETTERS.index(letter) >= len(options):
        return None, "answer does not map to an option"

    idx = LETTERS.index(letter)
    otext = _option_text(options[idx])
    if len(otext) > 3 and otext.lower() in question.lower():
        return None, "self-answering (answer appears in the question)"

    cleaned = dict(obj)
    cleaned["lines"] = [question] + options
    cleaned["answer"] = "%s  %s" % (letter, otext)
    return cleaned, "ok"


def _validate_number(obj):
    number = str(obj.get("number", "")).strip()
    meaning = str(obj.get("meaning", "")).strip()
    if not re.search(r"\d", number):
        return None, "number field has no digit"
    # A number's meaning is a short label ("Height of Everest"), not a sentence,
    # so it is not truncation-checked -- only rejected when empty/placeholder.
    if meaning in ("", ".", "...", "…"):
        return None, "empty/placeholder meaning"
    cleaned = dict(obj)
    cleaned["number"], cleaned["meaning"] = number, meaning
    return cleaned, "ok"


def _validate_tiny_games(obj):
    lines = [str(x).strip() for x in (obj.get("lines") or []) if str(x).strip()]
    if len(lines) < 2:
        return None, "tiny_games needs a prompt and options"
    question = lines[0]
    answer = str(obj.get("answer", "")).strip()
    cleaned = dict(obj)
    cleaned["lines"] = lines
    # "Which came first?" is factual and keeps its answer. Any other either/or
    # is a preference with no correct answer, so it must not assert one.
    if answer and _EITHER_OR.search(question) and not question.lower().startswith("which came first"):
        cleaned["answer"] = ""
    else:
        cleaned["answer"] = answer
    return cleaned, "ok"


def validate_card(kind, obj):
    """Return (cleaned_obj, reason) or (None, reason) to reject.

    The cleaned obj carries normalized/repaired fields; the caller builds its
    own payload from it exactly as before.
    """
    if not isinstance(obj, dict):
        return None, "not an object"
    if kind == "trivia":
        return _validate_trivia(obj)
    if kind == "number":
        return _validate_number(obj)
    if kind == "tiny_games":
        return _validate_tiny_games(obj)

    # Prose kinds. Reject only genuine defects; absurd comedy is intended.
    lines = [str(x).strip() for x in (obj.get("lines") or []) if str(x).strip()]
    text = str(obj.get("text", "")).strip()
    body = " ".join(lines) if lines else text
    if not body:
        return None, "empty card"
    if kind == "fun_facts" and looks_truncated(body):
        return None, "truncated fact"
    return dict(obj), "ok"


def verify_fact(kind, obj, call_model):
    """Cold model re-check for model-INVENTED factual kinds (number, trivia).

    `call_model(prompt) -> str` is injected so this is testable and provider-
    agnostic. Returns (ok: bool, reason). Fails closed: an unparseable or error
    response rejects the card, because for invented facts a false positive
    (airing a wrong number) is worse than dropping a good one.
    """
    if kind == "number":
        q = ("Is this numeric fact correct, to within an order of magnitude? "
             "Answer ONLY 'YES' or 'NO'.\n\n%s = %s"
             % (obj.get("number", ""), obj.get("meaning", "")))
    elif kind == "trivia":
        opts = "\n".join(obj.get("lines", [])[1:])
        q = ("Is the marked answer to this question factually correct? "
             "Answer ONLY 'YES' or 'NO'.\n\nQ: %s\n%s\nMarked answer: %s"
             % ((obj.get("lines") or [""])[0], opts, obj.get("answer", "")))
    else:
        return True, "no verification for this kind"
    try:
        out = (call_model(q) or "").strip().upper()
    except Exception as e:  # pragma: no cover - network
        return False, "verify call failed: %s" % e
    if out.startswith("YES"):
        return True, "verified"
    if out.startswith("NO"):
        return False, "model says the fact is wrong"
    return False, "unclear verification response: %r" % out[:40]
