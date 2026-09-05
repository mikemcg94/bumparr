"""XMLTV for the station's two channels.

Dispatcharr, Tunarr and most players show an empty grid for a channel with
no guide, and an empty grid reads as broken. Five-second items would be
noise in a grid, so the live channel's programmes are the daypart blocks:
the same windows that steer the rotation, with their viewer-facing
descriptions. Standby is one rolling "please stand by" programme, because
that is honestly all it is.
"""
import datetime
import xml.etree.ElementTree as ET

from bumparr import config, dayparts

LIVE_ID, STANDBY_ID = "bumparr.live", "bumparr.standby"


def _fmt(dt):
    return dt.strftime("%Y%m%d%H%M%S %z")


def xmltv(now=None, brand=None, back_hours=6, ahead_hours=24):
    """The guide as a string, covering now-back_hours .. now+ahead_hours."""
    brand = brand or config.BRAND
    t0 = dayparts.now_local(now)
    start, end = t0 - datetime.timedelta(hours=back_hours), t0 + datetime.timedelta(hours=ahead_hours)
    tv = ET.Element("tv", {"generator-info-name": "bumparr"})
    for cid, label in ((LIVE_ID, brand), (STANDBY_ID, brand + " standby")):
        ch = ET.SubElement(tv, "channel", id=cid)
        ET.SubElement(ch, "display-name").text = label
    for s, e, title, desc in dayparts.blocks(start, end, brand):
        p = ET.SubElement(tv, "programme", start=_fmt(s), stop=_fmt(e), channel=LIVE_ID)
        ET.SubElement(p, "title").text = title
        if desc:
            ET.SubElement(p, "desc").text = desc
    t = start.replace(minute=0, second=0, microsecond=0)
    t -= datetime.timedelta(hours=t.hour % 6)
    while t < end:
        e = t + datetime.timedelta(hours=6)
        p = ET.SubElement(tv, "programme", start=_fmt(t), stop=_fmt(e), channel=STANDBY_ID)
        ET.SubElement(p, "title").text = "%s — Please stand by" % brand
        ET.SubElement(p, "desc").text = "Standby loop: station IDs, test cards, and live windows."
        t = e
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="unicode") + "\n"
