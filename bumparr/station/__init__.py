"""The station: the bumper pool run as live HLS channels.

conform  — one-time transcode of each playable into splice-safe segments
playout  — a virtual clock per channel that picks, publishes, and reports
guide    — XMLTV for the channels
routes   — the HTTP surface

Design: docs/superpowers/specs/2026-09-05-station-playout-design.md.
"""
