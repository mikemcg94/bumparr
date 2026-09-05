"""Shared path validation and recoverable file-removal primitives."""
import os
import re
import uuid
from pathlib import Path

from bumparr import config


def _contained(candidate, root):
    """Normalized lexical candidate when its resolved target is contained.

    Resolution is used only for the security check. Returning the lexical path
    preserves the filesystem entry itself, so removing an in-tree symlink does
    not accidentally remove its in-tree target.
    """
    try:
        resolved_root = Path(root).resolve()
        candidate_path = Path(os.path.abspath(os.fspath(candidate)))
        resolved = candidate_path.resolve()
    except Exception:
        return None
    try:
        inside = resolved.is_relative_to(resolved_root)
    except AttributeError:
        try:
            resolved.relative_to(resolved_root)
            inside = True
        except ValueError:
            inside = False
    return candidate_path if inside else None


def resolve_media(uri):
    """Registry uri -> file on disk, across the source and output trees."""
    if not uri or str(uri).lower().startswith(("http://", "https://")):
        return None
    if uri.startswith("bumpers/"):
        root = Path(config.OUTPUT_DIR)
        candidate = root / uri[len("bumpers/"):]
    else:
        root = Path(config.ASSET_ROOT)
        candidate = root / uri
    return _contained(candidate, root)


def resolve_kind_dir(root, kind):
    """Resolved <root>/<kind> dir contained in root, or None.

    For category-directory removal only: never the root itself, never
    anything outside it (traversal, absolute, or symlink escape).
    """
    try:
        value = str(kind or "").strip()
        # Categories are registry/YAML values, not relative paths.
        if (not value or value.startswith(".") or "/" in value or "\\" in value
                or not re.fullmatch(r"[\w.-]+", value)):
            return None
        resolved_root = Path(root).resolve()
        d = (resolved_root / value).resolve()
    except Exception:
        return None
    if d == resolved_root:
        return None
    return _contained(d, resolved_root)


def safe_filename(name, default="clip.mp4", max_bytes=180):
    """Flatten and bound an untrusted filename, with a sanitized fallback."""
    fallback = os.path.basename(str(default).replace("\\", "/"))
    fallback = re.sub(r"[^\w.\-]", "_", fallback).strip("._")
    raw = os.path.basename(str(name or fallback).replace("\\", "/"))
    cleaned = re.sub(r"[^\w.\-]", "_", raw).strip("._")
    cleaned = cleaned or fallback
    # Linux components are normally limited to 255 bytes. Callers can reserve
    # space for their own prefixes/suffixes by choosing a smaller byte limit.
    encoded = cleaned.encode("utf-8")
    limit = max(1, min(255, int(max_bytes)))
    if len(encoded) > limit:
        suffix = Path(cleaned).suffix
        if len(suffix.encode("utf-8")) > min(20, limit - 1):
            suffix = ""
        budget = limit - len(suffix.encode("utf-8"))
        stem = cleaned[:-len(suffix)] if suffix else cleaned
        stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
        cleaned = stem.rstrip("._") + suffix
    return cleaned or fallback


def stage_delete(path):
    """Atomically quarantine one file beside itself; return its staged path.

    A missing file needs no staging and returns ``None``. Other failures are
    intentionally raised so callers can leave the associated DB row intact.
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file() and not path.is_symlink():
        raise OSError("target is not a regular file")
    staged = path.with_name(".bumparr-delete-%s" % uuid.uuid4().hex)
    os.replace(path, staged)
    return staged


def restore_delete(original, staged):
    if staged is not None and (Path(staged).exists() or Path(staged).is_symlink()):
        os.replace(staged, original)


def finish_delete(staged):
    if staged is not None:
        Path(staged).unlink()
