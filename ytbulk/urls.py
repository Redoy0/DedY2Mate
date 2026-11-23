"""Parse, validate and de-duplicate YouTube video URLs from a text file."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_WATCH_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PATH_PREFIXES = {"shorts", "embed", "live", "v", "e"}


@dataclass
class Entry:
    line: int
    raw: str
    video_id: str

    @property
    def url(self) -> str:
        # Canonical form: the video ID is all YouTube needs (unlisted included).
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class ParsedInput:
    entries: list[Entry] = field(default_factory=list)        # valid + unique, in file order
    invalid: list[tuple[int, str]] = field(default_factory=list)
    duplicates: list[Entry] = field(default_factory=list)
    total_found: int = 0                                      # non-empty, non-comment lines


def extract_video_id(text: str) -> str | None:
    """Return the 11-char video ID for a single-video YouTube URL, else None."""
    url = text.strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
    except ValueError:
        return None

    vid = ""
    if host in _SHORT_HOSTS:
        vid = p.path.lstrip("/").split("/")[0]
    elif host in _WATCH_HOSTS:
        if p.path.rstrip("/") == "/watch":
            vid = parse_qs(p.query).get("v", [""])[0]
        else:
            parts = [x for x in p.path.split("/") if x]
            if len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
                vid = parts[1]
    return vid if _ID_RE.match(vid) else None


def parse_url_file(path: str | Path) -> ParsedInput:
    return parse_url_text(Path(path).read_text(encoding="utf-8-sig", errors="replace"))


def parse_url_text(text: str) -> ParsedInput:
    """One URL per line. Blank lines and lines starting with '#' are ignored.
    Anything after the first whitespace on a line is ignored (allows notes)."""
    result = ParsedInput()
    seen: set[str] = set()
    text = text.lstrip("\ufeff")
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        result.total_found += 1
        token = stripped.split()[0]
        vid = extract_video_id(token)
        if vid is None:
            result.invalid.append((lineno, stripped))
            continue
        entry = Entry(line=lineno, raw=token, video_id=vid)
        if vid in seen:
            result.duplicates.append(entry)
        else:
            seen.add(vid)
            result.entries.append(entry)
    return result
