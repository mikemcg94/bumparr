"""M9 + Phase 4.10: footage-vs-weather routing and ambiguous-count handling."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bumparr import ingest


class Routing(unittest.TestCase):
    """Footage requests containing 'weather' pull footage, not a weather card."""

    def setUp(self):
        """Stub the network-backed endpoints to record routing only."""
        self.calls = {}
        self._orig_theme = ingest._theme_search
        self._orig_weather = ingest._weather_card

        def fake_theme(text, count=3):
            self.calls.setdefault("theme", []).append((text, count))
            return "theme-ok"

        def fake_weather(place):
            self.calls.setdefault("weather", []).append(place)
            return "weather-ok"

        ingest._theme_search = fake_theme
        ingest._weather_card = fake_weather
        self.addCleanup(self._restore)

    def _restore(self):
        ingest._theme_search = self._orig_theme
        ingest._weather_card = self._orig_weather

    def test_storm_footage_pull_routes_to_theme(self):
        """'pull 5 storm weather clips' is a footage pull, not a weather card."""
        self.assertEqual(ingest.handle("pull 5 storm weather clips"), "theme-ok")
        self.assertNotIn("weather", self.calls)

    def test_weather_clips_with_place_routes_to_theme(self):
        """'pull 5 weather clips in Seattle' is footage despite the preposition."""
        self.assertEqual(ingest.handle("pull 5 weather clips in Seattle"), "theme-ok")
        self.assertNotIn("weather", self.calls)

    def test_storm_weather_videos_routes_to_theme(self):
        self.assertEqual(ingest.handle(
            "5 storm-weather videos for my channel"), "theme-ok")
        self.assertNotIn("weather", self.calls)

    def test_weather_in_tokyo_routes_to_card(self):
        """'weather in Tokyo' is still a weather-data card request."""
        self.assertEqual(ingest.handle("weather in Tokyo"), "weather-ok")
        self.assertEqual(self.calls.get("weather"), ["tokyo"])

    def test_generic_get_and_fetch_verbs_still_route_to_weather_card(self):
        """Generic acquisition verbs are not by themselves footage cues."""
        self.assertEqual(ingest.handle("get weather for Boston"), "weather-ok")
        self.assertEqual(ingest.handle("fetch weather in Tokyo"), "weather-ok")
        self.assertEqual(self.calls.get("weather"), ["boston", "tokyo"])

    def test_explicit_count_without_media_noun_routes_to_theme(self):
        """A requested count is sufficient to distinguish footage from data."""
        self.assertEqual(ingest.handle("pull 5 weather in Boston"), "theme-ok")
        self.assertNotIn("weather", self.calls)

    def test_current_weather_and_weather_at_home_route_to_card(self):
        self.assertEqual(ingest.handle("current weather"), "weather-ok")
        self.assertEqual(ingest.handle("weather at home"), "weather-ok")
        self.assertEqual(self.calls.get("weather"), [None, None])


class ExtractCount(unittest.TestCase):
    """Years/decades are theme, not counts; explicit counts still work."""

    def test_year_with_suffix_takes_default(self):
        """'1940s cartoons' specifies no count, so the default applies."""
        self.assertIsNone(ingest._extract_count("1940s cartoons"))

    def test_standalone_year_ignored(self):
        """A 4-digit year that is not a count is ignored."""
        self.assertIsNone(ingest._extract_count("cartoons from 1940"))
        self.assertIsNone(ingest._extract_count("cartoons from 40"))

    def test_explicit_counts_preserved(self):
        """'pull 8 rain' and '5 golf clips' are still explicit counts."""
        self.assertEqual(ingest._extract_count("pull 8 rain"), 8)
        self.assertEqual(ingest._extract_count("5 golf clips"), 5)


class StableErrors(unittest.TestCase):
    def test_request_results_do_not_reflect_exception_details(self):
        secret = "secret-token-and-private-path"
        with mock.patch.object(ingest, "_gj", side_effect=RuntimeError(secret)):
            archive = ingest._fetch_archive("item", "safe")
            search = ingest._theme_search("rain clips", 1)
        with mock.patch("bumparr.generators.weather.generate",
                        side_effect=RuntimeError(secret)):
            weather = ingest._weather_card("home")
        for result in (archive, search, weather):
            self.assertNotIn(secret, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
