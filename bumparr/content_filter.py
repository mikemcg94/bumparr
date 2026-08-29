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
GRIM_WEIGHT = 0.08

GRIM_TERMS = (
    "war ", "wars", "kill", "died", " death", "dead", "bomb", "attack", "massacre",
    "assassin", "shot", "shoot", "murder", "terror", "genocide", "execut", "invasion",
    "battle", "siege", "crash", "disaster", "earthquake", "hurricane", "flood",
    "famine", "riot", "hostage", "suicide", "slaughter", "atrocit", "casualt",
    "wound", "victim", "nazi", "holocaust", "lynch", "rape", "plague", "epidemic",
    "tsunami", "collapse", "explos", "wreck", "drown", "fatal", "tragedy",
    "coup", "hijack", "sank", "sink", "gunman", "shell", "airstrike", "raid",
    "shooting", "bombing", "stabbing", "hanged", "beheaded", "poison",
)


def is_grim(text: str) -> bool:
    t = " " + (text or "").lower() + " "
    return any(term in t for term in GRIM_TERMS)


def weight_for(base_weight: float, text: str) -> float:
    """Normal weight, or the heavily-reduced grim weight for grim text."""
    return GRIM_WEIGHT if is_grim(text) else base_weight


def allow_image(text: str) -> bool:
    """Grim content is never given a background image."""
    return not is_grim(text)
