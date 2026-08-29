"""Weather cards — real current conditions from Open-Meteo (free, no key).

A grounded card: geocodes a place name, fetches live weather, renders a station-
style weather card. Home location comes from HOME_LOCATION; a request can
name any place ("weather in Tokyo").

Usage:
    python -m bumparr.generators.weather --location "Your City, Region"
    python -m bumparr.generators.weather   # uses HOME_LOCATION
"""
import argparse
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

from bumparr import config, db

UA = {"User-Agent": "bumparr/1.0"}

# WMO weather codes -> (label, emoji)
WMO = {
    0: ("Clear", "☀"), 1: ("Mainly clear", "🌤"), 2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁"), 45: ("Fog", "🌫"), 48: ("Rime fog", "🌫"),
    51: ("Light drizzle", "🌦"), 53: ("Drizzle", "🌦"), 55: ("Heavy drizzle", "🌧"),
    61: ("Light rain", "🌦"), 63: ("Rain", "🌧"), 65: ("Heavy rain", "🌧"),
    66: ("Freezing rain", "🌧"), 67: ("Freezing rain", "🌧"),
    71: ("Light snow", "🌨"), 73: ("Snow", "🌨"), 75: ("Heavy snow", "❄"),
    77: ("Snow grains", "🌨"), 80: ("Showers", "🌦"), 81: ("Showers", "🌧"),
    82: ("Violent showers", "⛈"), 85: ("Snow showers", "🌨"), 86: ("Snow showers", "❄"),
    95: ("Thunderstorm", "⛈"), 96: ("Thunderstorm + hail", "⛈"), 99: ("Thunderstorm + hail", "⛈"),
}


def _gj(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20))


def _geocode(name):
    d = _gj("https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(name))
    res = (d.get("results") or [])
    if not res:
        return None
    r = res[0]
    label = r["name"]
    if r.get("admin1"):
        label += ", " + r["admin1"]
    return {"label": label, "lat": r["latitude"], "lon": r["longitude"]}


def generate(location):
    loc = _geocode(location)
    if not loc:
        return "couldn't find location: %s" % location
    w = _gj("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
            "&current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m"
            "&temperature_unit=fahrenheit&wind_speed_unit=mph" % (loc["lat"], loc["lon"]))
    cur = w.get("current", {})
    code = cur.get("weather_code", 0)
    label, emoji = WMO.get(code, ("—", "•"))
    temp = round(cur.get("temperature_2m", 0))
    payload = {
        "city": loc["label"].upper(),
        "temp": "%d°" % temp,
        "conditions": label,
        "emoji": emoji,
        "wind": "%d mph" % round(cur.get("wind_speed_10m", 0)),
        "humidity": "%d%%" % round(cur.get("relative_humidity_2m", 0)),
        "source": "Open-Meteo",
    }
    pid = "card:weather:" + hashlib.md5(loc["label"].encode()).hexdigest()[:12]
    with db.conn() as c:
        # weather is time-sensitive: refresh the card for the same place. OR REPLACE
        # is atomic (a DELETE+INSERT can race the shared DB and throw UNIQUE errors).
        c.execute("INSERT OR REPLACE INTO playables (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)",
                  (pid, "card", "weather", "grounded", None, 10.0,
                   loc["label"], json.dumps(payload), "grounded,weather", 0.8, time.time()))
        c.commit()
    return "weather card for %s: %d° %s" % (loc["label"], temp, label)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", default=config.env("HOME_LOCATION", ""),
                    help="e.g. 'Portland, Oregon'. Required: there is no sensible default location.")
    ap.add_argument("--n", type=int, default=1)  # accepted for API symmetry; weather is one card/location
    args = ap.parse_args()
    db.init_db()
    print("[weather]", generate(args.location))
