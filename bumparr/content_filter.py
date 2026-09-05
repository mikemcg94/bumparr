"""Shared tone policy for generated content.

The default voice is cozy, dry and weird. Wikipedia's "on this day" feed and open
image search both skew heavily toward war, disaster, and violence.

Policy: grim content is NOT banned — it may appear *very
occasionally*, but weighted heavily against, and NEVER with a background image.
A real photo of an atrocity behind a bumper is the thing to prevent.

  is_grim(text)          -> True if the text reads as violence/disaster/tragedy.
  weight_for(base, text) -> base weight normally; GRIM_WEIGHT if grim (rare).
  allow_image(text)      -> False for grim text (never illustrate it).
"""

# A grim card keeps ~1/10th the pull of a normal one, so it surfaces rarely
# rather than never.
import re

GRIM_WEIGHT = 0.08

GRIM_TERMS = (
    "war ", "wars", "kill", "died", " death", "dead", "bomb", "attack", "massacre",
    "assassin", "murder", "terror", "genocide", "invasion",
    "battle", "siege", "disaster", "earthquake", "hurricane", "flood",
    "famine", "hostage", "suicide", "slaughter", "atrocit", "casualt",
    "wound", "victim", "nazi", "holocaust", "lynch", "plague", "epidemic",
    "tsunami", "collapse", "explos", "wreck", "drown", "fatal", "tragedy",
    "hijack", "gunman", "airstrike",
    "shooting", "bombing", "stabbing", "hanged", "beheaded", "poison",
)

_WORD_RES = [re.compile(p) for p in (
    r"\bshots?\b",
    r"\bshoots?\b",
    r"\bexecut(?:e|es|ed|ing|ion|ions)\b",
    r"\bcoups?\b",
    r"\braid(?:s|ed|ing)?\b",
    r"\bshell(?:s|ed|ing)?\b",
    r"\bsink(?:s|ing)?\b|\bsank\b|\bsunk(?:en)?\b",
    r"\brape(?:s|d)?\b",
    r"\briot(?:s|ed|ing)?\b",
    r"\bcrash(?:es|ed|ing)?\b",
)]


def is_grim(text: str) -> bool:
    """True if the text reads as violence/disaster/tragedy (see GRIM_TERMS).

    Deliberately a dumb keyword check: it is the shared gate for every
    generated source, must run offline, and false positives only cost a card
    its weight and its background image — never its place in the pool.
    """
    t = " " + (text or "").lower() + " "
    return any(term in t for term in GRIM_TERMS) or any(rx.search(t) for rx in _WORD_RES)


def weight_for(base_weight: float, text: str) -> float:
    """Normal weight, or the heavily-reduced grim weight for grim text."""
    return GRIM_WEIGHT if is_grim(text) else base_weight


def allow_image(text: str) -> bool:
    """Grim content is never given a background image."""
    return not is_grim(text)
