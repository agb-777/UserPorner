# userporner.py
# Export the video list of an Eporner profile to TXT, CSV, JSON, Markdown, HTML, or PDF.
# Author: agb-777
# Homepage: https://github.com/agb-777/userporner

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit
from xml.sax.saxutils import escape

from colorama import Fore, Style, init

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTHOR = "agb-777"
HOMEPAGE = "https://github.com/agb-777/userporner"

# Version of the checkpoint layout. Older checkpoints lack thumbnail and views, so they cannot be resumed.
CHECKPOINT_FORMAT = 2
DEFAULT_FORMAT = "txt"
DEFAULT_FIELDS = "all"
DEFAULT_WORKERS = 3
DEFAULT_RETRIES = 3
# An empty or missing page must be seen at least twice before the listing is treated as ended.
MIN_RETRIES = 2
DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 500
FORMATS = ("txt", "csv", "json", "md", "html", "pdf")
FIELD_CHOICES = ("all", "url")
# Column order used by every format: title, thumbnail, views, URL.
ALL_FIELDS = ("title", "thumbnail", "views", "url")
FIELD_LABELS = {"title": "Title", "thumbnail": "Thumbnail", "views": "Views", "url": "URL"}
# CSS class of each column in the HTML table.
_HTML_CLASS = {"title": "title", "thumbnail": "thumb", "views": "views", "url": "url"}

# PDF column sizes. Fixed columns have a set width in points, flexible columns share the rest.
THUMB_BOX = (120, 68)  # Largest thumbnail drawn in the PDF, in points.
PDF_FIXED_WIDTHS = {"thumbnail": THUMB_BOX[0] + 12, "views": 60}
PDF_FLEX_WEIGHTS = {"title": 0.6, "url": 0.4}
MAX_IMAGE_BYTES = 1024 * 1024  # Thumbnails larger than this are skipped in the PDF.

# A username: letters, digits, underscore, dash, with single dots between parts.
USERNAME = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"
USERNAME_PATTERN = re.compile(USERNAME)
# A full profile URL, with optional scheme and www. Captures the username.
PROFILE_PATTERN = re.compile(
    rf"^(?:https?://)?(?:www\.)?eporner\.com/profile/({USERNAME})(?:[/?#]|$)",
    re.IGNORECASE,
)
# Matches a listing page number such as /uploaded-videos/3/.
PAGE_PATTERN = re.compile(r"/uploaded-videos/(\d+)(?:[/?#]|$)")
# Every video link contains this text.
VIDEO_MARKER = "/video-"
# Text that appears in the markup of every real listing page, even when it has no videos.
LISTING_MARKER = "/uploaded-videos/"
# Only links on this site are counted as videos.
SITE_HOST = "eporner.com"
# CSS class of the div that holds one video on a listing page.
_CONTAINER_CLASS = "mb"
# CSS class of the span that holds the view count.
_VIEWS_CLASS = "mbvie"
# A view count written as plain digits, with optional thousands separators.
_PLAIN_NUMBER = re.compile(r"\d[\d,\s]*")

# Network settings, in seconds unless noted.
REQUEST_TIMEOUT = 20
REQUEST_DELAY = 0.5  # Pause before each request, per worker, to keep the load on the site low.
RETRY_DELAY = 2  # Multiplied by the attempt number, so retries back off.
MAX_PAGE_BYTES = 10 * 1024 * 1024  # Larger responses are treated as failures.
SAFETY_PAGE_LIMIT = 100_000  # Hard stop so the scraper cannot loop forever.
MAX_WORKERS = 10
MAX_RETRIES = 10

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html",
    "Accept-Encoding": "identity",  # urllib does not decompress gzip, so ask for plain HTML.
    "Accept-Language": "en-US,en;q=0.9",
}

# Characters left alone when a URL is percent-encoded for Markdown.
URL_SAFE_CHARS = ":/?=&%#+@,;!$'()*"
# Characters that have a meaning in Markdown and must be escaped inside table cells.
_MD_SPECIAL = str.maketrans({char: "\\" + char for char in "\\`*_[]<>|&"})
# Markdown column alignment: centered thumbnails, right-aligned views, left-aligned text.
_MD_ALIGN = {"thumbnail": ":--:", "views": "--:"}
# Control characters that are not whitespace. They can break PDF generation.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0e-\x1b\x7f]")
# A CSV cell starting with one of these may be run as a formula by spreadsheet programs.
_CSV_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

# Reasons used in log messages when a page marks the end of the listing.
END_REASONS = {"missing": "does not exist", "end": "is empty"}

_HTML_STYLE = """
:root { color-scheme: light dark; --bg: #f6f7f9; --fg: #1b1f24; --muted: #5b6577; --line: #d9dee7;
        --head: #1f2a44; --stripe: #eef2f7; --accent: #2f6db5; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #14171c; --fg: #e6e9ef; --muted: #97a0b0; --line: #2a303b;
          --head: #0d1220; --stripe: #1a1f27; --accent: #7fb2ff; }
}
[hidden] { display: none !important; }
body { margin: 0; padding: 32px 24px; background: var(--bg); color: var(--fg);
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size: 22px; margin: 0 0 6px; }
.meta { color: var(--muted); margin: 0 0 20px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; }
th { background: var(--head); color: #ffffff; font-size: 12px;
     text-transform: uppercase; letter-spacing: 0.04em; }
tbody tr:nth-child(even) td { background: var(--stripe); }
td.num, th.num { text-align: right; color: var(--muted); }
th.thumb, td.thumb { text-align: center; }
td.thumb img { display: block; width: 160px; height: auto; margin: 0 auto; border-radius: 4px; }
td.views, th.views { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.url { word-break: break-all; font-size: 12px; }
a { color: var(--accent); }
.pager { display: flex; align-items: center; justify-content: center; gap: 12px; margin: 20px 0; }
.pager button { font: inherit; padding: 6px 14px; border: 1px solid var(--line); border-radius: 6px;
                background: var(--head); color: #ffffff; cursor: pointer; }
.pager button:disabled { opacity: 0.4; cursor: default; }
.pager span { color: var(--muted); min-width: 220px; text-align: center; }
footer { margin-top: 24px; color: var(--muted); font-size: 12px; }
"""

# Script for the HTML pager. __PER_PAGE__ is replaced with the chosen number of rows per page.
_PAGER_SCRIPT = """
(function () {
  var perPage = __PER_PAGE__;
  var rows = Array.prototype.slice.call(document.querySelectorAll("tbody tr"));
  var total = Math.max(1, Math.ceil(rows.length / perPage));
  var current = 1;
  var nav = document.getElementById("pager");
  var info = document.getElementById("pager-info");
  var prev = document.getElementById("prev");
  var next = document.getElementById("next");

  function show() {
    rows.forEach(function (row, index) {
      row.hidden = Math.floor(index / perPage) + 1 !== current;
    });
    info.textContent = "Page " + current + " of " + total + " (" + perPage + " per page)";
    prev.disabled = current <= 1;
    next.disabled = current >= total;
  }

  prev.addEventListener("click", function () {
    if (current > 1) { current -= 1; show(); window.scrollTo({ top: 0, behavior: "smooth" }); }
  });
  next.addEventListener("click", function () {
    if (current < total) { current += 1; show(); window.scrollTo({ top: 0, behavior: "smooth" }); }
  });
  nav.hidden = false;
  show();
})();
"""


class ValidationError(Exception):
    # A problem with the settings or with a file the user named. The run stops before scraping.
    pass


# ---------------------------------------------------------------------------
# Logging and stop signal
# ---------------------------------------------------------------------------

# Lock so messages from several threads do not mix on one line.
_LOG_LOCK = threading.Lock()
# Set when the program is stopping, so worker threads quit without finishing every retry.
_STOP = threading.Event()
_LOG_STYLES = {
    "info": (Fore.CYAN, "*"),
    "success": (Fore.GREEN, "+"),
    "warn": (Fore.YELLOW, "!"),
    "error": (Fore.RED, "x"),
    "step": (Fore.MAGENTA, ">"),
    "dim": (Fore.WHITE, "-"),
}


def log(message, level="info"):
    # Print a timestamped, colored message.
    color, symbol = _LOG_STYLES.get(level, (Fore.WHITE, "-"))
    stamp = datetime.now().strftime("%H:%M:%S")
    with _LOG_LOCK:
        print(f"{color}{Style.DIM}[{stamp}]{Style.NORMAL} {symbol} {message}{Style.RESET_ALL}", flush=True)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def normalize_profile_url(raw):
    # Accept a full profile URL or a bare username. Return the listing URL, or None if invalid.
    text = (raw or "").strip()
    match = PROFILE_PATTERN.match(text)
    if match is not None:
        username = match.group(1)
    elif USERNAME_PATTERN.fullmatch(text):
        username = text
    else:
        return None
    return f"https://www.eporner.com/profile/{username}/uploaded-videos/"


def page_url(base, number):
    # Page 1 is the base URL. Other pages are base + number + "/".
    return base if number == 1 else f"{base}{number}/"


def page_number(url):
    # Read the page number from a URL. Default to 1 when there is none.
    match = PAGE_PATTERN.search(url or "")
    return int(match.group(1)) if match else 1


def _on_site(url):
    # True if the URL points to eporner.com or one of its subdomains.
    host = (urlsplit(url).hostname or "").lower()
    return host == SITE_HOST or host.endswith("." + SITE_HOST)


def _safe_image_url(url):
    # Only http and https image links are kept. Anything else becomes an empty value.
    return url if urlsplit(url).scheme in ("http", "https") else ""


def _is_listing(final_url, number):
    # A response only counts as page N if it is a listing URL for that same page number.
    # A redirect to the home page or another section is not a listing.
    return LISTING_MARKER in final_url and page_number(final_url) == number


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _clean(value):
    # Remove control characters and collapse whitespace and newlines into single spaces.
    return " ".join(_CONTROL_CHARS.sub(" ", str(value)).split())


def _classes(attr):
    # The list of CSS classes on an element.
    return (attr.get("class") or "").split()


def _to_int(text):
    # Turn "11,137" into 11137. Return None for anything that is not a plain number, such as "1.2K".
    text = (text or "").strip()
    if not _PLAIN_NUMBER.fullmatch(text):
        return None
    return int(re.sub(r"\D", "", text))


def _urls(videos):
    # The set of video URLs in a list of video dictionaries.
    return {video["url"] for video in videos}


class _VideoParser(HTMLParser):
    # Reads one listing page into cards. A card is one div with class "mb" that holds a video
    # link, a thumbnail, and a view count. Video links outside any card are kept as a fallback
    # for pages that use a different layout.

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self._stack = []  # One flag per open div: True if that div starts a card.
        self._card = None  # The card being read, or None.
        self._link = None  # Absolute URL of the video link being read, or None.
        self._link_title = ""  # Title attribute of the current link.
        self._link_alt = ""  # Alt text of the image inside the current link.
        self._link_text = []  # Visible text of the current link.
        self._views_open = False  # True while reading inside the views span.
        self.cards = []  # Finished cards, in page order.
        self.loose = []  # Video links outside any card, in page order.

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "div":
            starts_card = self._card is None and _CONTAINER_CLASS in _classes(attr)
            if starts_card:
                self._card = {"url": "", "title": "", "thumbnail": "", "views_text": ""}
            self._stack.append(starts_card)
        elif tag == "a":
            # A new link starts before the old one was closed. Browsers close the old one first.
            self._close_link()
            href = attr.get("href") or ""
            if VIDEO_MARKER in href:
                # Relative links are resolved against the page URL, like a browser does.
                absolute = urljoin(self._base, href)
                # Ignore video-looking links that point to other sites, such as ads.
                if _on_site(absolute):
                    self._link = absolute
                    self._link_title = (attr.get("title") or "").strip()
                    self._link_alt = ""
                    self._link_text = []
        elif tag == "img" and self._link is not None:
            if not self._link_alt:
                self._link_alt = (attr.get("alt") or "").strip()
            if self._card is not None and not self._card["thumbnail"]:
                src = (attr.get("src") or "").strip()
                if src:
                    self._card["thumbnail"] = _safe_image_url(urljoin(self._base, src))
        elif tag == "span" and self._card is not None and _VIEWS_CLASS in _classes(attr):
            self._views_open = True

    def handle_endtag(self, tag):
        if tag == "a":
            self._close_link()
        elif tag == "span":
            self._views_open = False
        elif tag == "div" and self._stack:
            self._close_link()
            if self._stack.pop():
                self._finish_card()

    def handle_data(self, data):
        if self._views_open and self._card is not None:
            self._card["views_text"] += data
        elif self._link is not None:
            self._link_text.append(data)

    def _close_link(self):
        # Store the current video link in the card, or in the fallback list if it has no card.
        if self._link is None:
            return
        # Title order: title attribute, then image alt text, then visible text.
        title = _clean(self._link_title or self._link_alt or "".join(self._link_text))
        if self._card is None:
            self.loose.append({"url": self._link, "title": title, "thumbnail": "", "views_text": ""})
        else:
            if not self._card["url"]:
                self._card["url"] = self._link
            if not self._card["title"]:
                self._card["title"] = title
        self._link = None

    def _finish_card(self):
        card, self._card = self._card, None
        if card is not None and card["url"]:
            self.cards.append(card)

    def finish(self):
        # Close anything still open when the page ends, such as a card with a missing closing tag.
        self._close_link()
        if self._card is not None:
            self._finish_card()


def parse_videos(html, page_url_value):
    # Return a list of {title, url, thumbnail, views} dictionaries from one listing page.
    parser = _VideoParser(page_url_value)
    parser.feed(html)
    parser.close()
    parser.finish()
    # Prefer listing cards. Fall back to loose video links if the page has no cards.
    source = parser.cards or parser.loose
    found = {}
    for item in source:
        entry = found.setdefault(
            item["url"],
            {"title": "", "url": item["url"], "thumbnail": "", "views": None},
        )
        # Keep the first non-empty value seen for each field.
        if not entry["title"]:
            entry["title"] = _clean(item["title"])
        if not entry["thumbnail"]:
            entry["thumbnail"] = item["thumbnail"]
        if entry["views"] is None:
            entry["views"] = _to_int(item["views_text"])
    return list(found.values())


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PageResult:
    # kind is one of:
    #   "ok"       the page has videos
    #   "end"      the page is a real listing with no videos, confirmed over every attempt
    #   "missing"  the page does not exist (404 or redirect), confirmed over every attempt
    #   "fail"     the page could not be read after all attempts, or the program is stopping
    kind: str
    videos: list | None = None
    reason: str = ""


def _download(url):
    # Download one page. Return (final URL after redirects, raw bytes, declared charset).
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read(MAX_PAGE_BYTES + 1)
        if len(raw) > MAX_PAGE_BYTES:
            raise OSError(f"page is larger than {MAX_PAGE_BYTES // (1024 * 1024)} MB")
        return response.geturl(), raw, response.headers.get_content_charset()


def _decode(raw, charset):
    # Decode the body. Fall back to UTF-8 if the server declares a charset Python does not know.
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def fetch_page(base, number, attempts):
    # Fetch one listing page with up to `attempts` tries. Return a PageResult.
    # "Missing" and "empty" are only trusted when every attempt agrees, so a single
    # transient error cannot end the listing early.
    url = page_url(base, number)
    reason = "unknown"
    missing_votes = 0
    empty_votes = 0
    for attempt in range(1, attempts + 1):
        # Waiting on the stop event instead of sleeping lets Ctrl+C end workers quickly.
        if _STOP.wait(REQUEST_DELAY):
            return PageResult("fail", reason="stopped")
        vote = None
        try:
            final_url, raw, charset = _download(url)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404:
                vote = "missing"
            else:
                reason = f"HTTP {code}"
        except Exception as exc:
            # Covers timeouts, connection resets, incomplete reads, and oversized pages.
            reason = type(exc).__name__
        else:
            try:
                page_html = _decode(raw, charset)
                if not _is_listing(final_url, number):
                    vote = "missing"
                else:
                    videos = parse_videos(page_html, final_url)
                    if videos:
                        return PageResult("ok", videos)
                    if LISTING_MARKER in page_html:
                        vote = "empty"
                    else:
                        # Anything else, such as a challenge or error page, is a failure, never the end.
                        reason = "unexpected page content"
            except Exception as exc:
                # A parser error on one page must not stop the whole run.
                reason = f"parse error: {type(exc).__name__}"

        if vote == "missing":
            missing_votes += 1
        elif vote == "empty":
            empty_votes += 1

        if _STOP.is_set():
            return PageResult("fail", reason="stopped")

        # Only real problems are logged. A normal end of the listing is confirmed quietly.
        if attempt < attempts:
            if vote is None:
                log(f"Attempt {attempt}/{attempts} failed ({reason}): {url}", "warn")
            if _STOP.wait(RETRY_DELAY * attempt):
                return PageResult("fail", reason="stopped")

    if missing_votes == attempts:
        return PageResult("missing")
    if empty_votes == attempts:
        return PageResult("end")
    log(f"All {attempts} attempts failed ({reason}): {url}", "error")
    return PageResult("fail", reason=reason)


def _fetch_batch(job, batch):
    # Fetch several pages at once. Results come back in the same order as the batch.
    executor = ThreadPoolExecutor(max_workers=job.workers)
    try:
        return list(executor.map(lambda number: fetch_page(job.url, number, job.attempts), batch))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _is_jpeg_or_png(data):
    # The PDF writer can only draw JPEG and PNG images without extra libraries.
    return data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")


def _fetch_image(url):
    # Download one thumbnail for the PDF. Return the bytes, or None if it is missing or not supported.
    # Any failure is treated as a missing picture, so one bad image cannot stop the run.
    if _STOP.wait(REQUEST_DELAY):
        return None
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg,image/png,*/*;q=0.5"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            if not response.headers.get_content_type().startswith("image/"):
                return None
            data = response.read(MAX_IMAGE_BYTES + 1)
    except Exception:
        return None
    if len(data) > MAX_IMAGE_BYTES or not _is_jpeg_or_png(data):
        return None
    return data


def _download_thumbnails(job, records):
    # Pictures for the PDF are downloaded in parallel, with the same worker count as the pages.
    urls = sorted({record["thumbnail"] for record in records if record.get("thumbnail")})
    if not urls:
        return {}
    log(f"Downloading {len(urls)} thumbnails for the PDF...", "step")
    images = {}
    executor = ThreadPoolExecutor(max_workers=job.workers)
    try:
        futures = {executor.submit(_fetch_image, url): url for url in urls}
        done = 0
        for future in as_completed(futures):
            data = future.result()
            if data is not None:
                images[futures[future]] = data
            done += 1
            if done % 100 == 0 or done == len(urls):
                log(f"Thumbnails: {done}/{len(urls)}", "dim")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    failed = len(urls) - len(images)
    if failed:
        log(f"{failed} thumbnails could not be downloaded and will show as '-' in the PDF.", "warn")
    return images


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------

def _is_video(item):
    # Check that a stored video has the right shape and points to this site before using it.
    # A checkpoint can be edited by hand, so nothing from it is trusted without these checks.
    if not isinstance(item, dict):
        return False
    url = item.get("url")
    thumbnail = item.get("thumbnail")
    views = item.get("views")
    return (
        isinstance(url, str)
        and bool(url)
        and _on_site(url)
        and isinstance(item.get("title"), str)
        and isinstance(thumbnail, str)
        and (thumbnail == "" or _safe_image_url(thumbnail) == thumbnail)
        and (views is None or (isinstance(views, int) and not isinstance(views, bool)))
    )


def _is_page_key(key):
    # Page keys are stored as strings like "3". Check that they are digits within the valid range.
    return isinstance(key, str) and key.isascii() and key.isdigit() and 1 <= int(key) <= SAFETY_PAGE_LIMIT


class Checkpoint:
    # Keeps progress for one profile. Progress is only written to disk when a save path is given,
    # and it is only read from disk when a resume path is given. Nothing is implicit.

    def __init__(self, save_path, source_url):
        self.save_path = Path(save_path) if save_path else None
        self.source_url = source_url  # The checkpoint only applies to this URL.
        self.pages = {}  # {page number: [videos]}
        self.last_page = None  # Set once the end of the listing is confirmed.

    @property
    def videos(self):
        # All videos from pages up to the last page, with duplicates removed by URL.
        unique = {}
        for number in sorted(self.pages):
            if self.last_page is not None and number > self.last_page:
                continue
            for video in self.pages[number]:
                unique.setdefault(video["url"], video)
        return list(unique.values())

    def known_urls(self):
        # Every video URL stored so far, within the listing.
        return {
            video["url"]
            for number, page in self.pages.items()
            if self.last_page is None or number <= self.last_page
            for video in page
        }

    def covers_listing(self):
        # True when the end is known and every page up to it is stored.
        return self.last_page is not None and all(n in self.pages for n in range(1, self.last_page + 1))

    def load(self, path):
        # Read progress from a file the user named with -R. Every problem is an error.
        # A run that asked to resume must never quietly start from the beginning instead.
        path = Path(path)
        if not path.exists():
            raise ValidationError(f"resume file not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError(f"resume file is unreadable ({exc}): {path}") from exc
        if not isinstance(data, dict):
            raise ValidationError(f"resume file is not a checkpoint: {path}")

        source = data.get("source_url")
        if not isinstance(source, str) or not source:
            raise ValidationError(f"resume file has no source URL: {path}")
        # Compared without case, because file names are not case sensitive on every system.
        if source.lower() != self.source_url.lower():
            raise ValidationError(f"resume file belongs to another profile ({source}): {path}")
        if data.get("format") != CHECKPOINT_FORMAT:
            raise ValidationError(f"resume file was made by an older version and cannot be used: {path}")

        last = data.get("last_page")
        if isinstance(last, int) and not isinstance(last, bool) and 1 <= last <= SAFETY_PAGE_LIMIT:
            self.last_page = last

        stored = data.get("pages")
        if isinstance(stored, dict):
            for key, videos in stored.items():
                if not (_is_page_key(key) and isinstance(videos, list)):
                    continue
                valid = [video for video in videos if _is_video(video)]
                # A damaged page is dropped so it is fetched again, instead of losing videos silently.
                if valid and len(valid) == len(videos):
                    self.pages[int(key)] = valid

        # Pages after the known end are not part of the listing, so they are dropped.
        if self.last_page is not None:
            self.pages = {n: page for n, page in self.pages.items() if n <= self.last_page}

        log(f"Resuming from {path}: {len(self.pages)} pages, {len(self.videos)} videos", "success")

    def record(self, number, videos):
        # Store one page and save progress if a save path was given.
        self.pages[number] = videos
        self._save()

    def set_last_page(self, number):
        # Mark the end of the listing and save progress if a save path was given.
        self.last_page = number
        self._save()

    def _save(self):
        # Do nothing unless the user asked for a checkpoint file with -s.
        if self.save_path is None:
            return
        payload = {
            "format": CHECKPOINT_FORMAT,
            "source_url": self.source_url,
            "timestamp": datetime.now().isoformat(),
            "last_page": self.last_page,
            "pages": {str(number): videos for number, videos in sorted(self.pages.items())},
        }
        # Write to a .tmp file first, then rename it. This prevents a half-written checkpoint.
        tmp = self.save_path.with_name(self.save_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.save_path)
        except OSError as exc:
            raise OSError(f"checkpoint could not be saved to {self.save_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Crawling
# ---------------------------------------------------------------------------

def _already_seen(checkpoint, videos):
    # Some sites repeat earlier content for out-of-range page numbers.
    # A page whose videos were all collected before is a repeat, not new content.
    return bool(videos) and _urls(videos) <= checkpoint.known_urls()


def _next_batch(checkpoint, workers):
    # The next pages to fetch, in order. Pages already stored are skipped, so a gap in a
    # checkpoint is filled without refetching pages that are already known.
    batch = []
    number = 1
    while len(batch) < workers and number <= SAFETY_PAGE_LIMIT:
        if checkpoint.last_page is not None and number > checkpoint.last_page:
            break
        if number not in checkpoint.pages:
            batch.append(number)
        number += 1
    return batch


def _later_pages_stored(checkpoint, end_page):
    # True if any page after end_page is already stored. That would contradict an end at end_page.
    return any(number > end_page for number in checkpoint.pages)


def _failure_message(job, number, reason):
    # Explain how to continue, depending on whether progress is being saved.
    if job.save_path is not None:
        hint = f"run again with -R {job.save_path} to continue"
    else:
        hint = "no progress was saved, use -s FILE next time to be able to continue"
    return f"page {number} failed ({reason}); {hint}"


def crawl(job, checkpoint):
    # Fetch pages in parallel batches until the end of the listing is confirmed.
    # Return (complete, error_message).
    while True:
        batch = _next_batch(checkpoint, job.workers)
        if not batch:
            if checkpoint.last_page is not None:
                return True, ""
            return False, "reached the safety page limit"

        began = time.monotonic()
        log(f"Fetching pages {', '.join(map(str, batch))}...", "step")
        results = _fetch_batch(job, batch)
        if job.verbose:
            log(f"Batch finished in {time.monotonic() - began:.1f}s", "dim")

        # Process results in page order, so the end of the listing is found correctly.
        for number, result in zip(batch, results):
            if result.kind == "fail":
                return False, _failure_message(job, number, result.reason)

            ends_listing = result.kind in END_REASONS or (
                result.kind == "ok" and _already_seen(checkpoint, result.videos)
            )
            if ends_listing:
                # A resumed checkpoint already knows where the listing ends. If the site now disagrees,
                # stop instead of shrinking the listing silently, because that would drop videos.
                if checkpoint.last_page is not None:
                    return False, (
                        f"page {number} no longer matches the resumed checkpoint, "
                        "start a new run without -R"
                    )
                # An empty first page is never trusted as a finished listing. It usually means a wrong
                # username or a changed page layout.
                if number == 1:
                    return False, "page 1 has no videos or does not exist, check the username or the page layout"
                end_page = number - 1
                if _later_pages_stored(checkpoint, end_page):
                    return False, (
                        f"page {number} ends the listing, but later pages are already stored. "
                        "The listing changed, so start a new run without -R"
                    )
                reason = END_REASONS.get(result.kind, "repeats earlier pages")
                checkpoint.set_last_page(end_page)
                log(f"Page {number} {reason}, last page is {end_page}.", "success")
                break

            checkpoint.record(number, result.videos)
            log(f"Page {number}: {len(result.videos)} videos", "success")


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------
# Every writer takes the same arguments, so save_records can call any of them.
# images holds PDF pictures keyed by URL, and per_page sets the rows per page in HTML.
# metadata["status"] is "complete" or "incomplete", so a partial file cannot pass for a full one.

def _plain_value(value):
    # Text for the plain text format. Empty values show a dash.
    if value is None or value == "":
        return "-"
    return _clean(value)


def _write_txt(path, records, fieldnames, metadata, images=None, per_page=None):
    # Plain text: one block per video, so it reads well in any editor and needs no viewer.
    lines = [
        "Eporner Video Listing",
        "",
        f"Source: {metadata['source_url']}",
        f"Total videos: {metadata['total_videos']}",
        f"Status: {metadata['status']}",
        f"Scraped at: {metadata['scraped_at']}",
        f"Author: {metadata['author']}",
        f"Homepage: {metadata['homepage']}",
        "",
    ]
    for index, record in enumerate(records, start=1):
        # The first line of each block is the title, or the URL when the title is not included.
        if "title" in fieldnames:
            heading = _clean(record["title"]) or "(no title)"
            rest = [name for name in fieldnames if name != "title"]
        else:
            heading = _clean(record["url"])
            rest = [name for name in fieldnames if name != "url"]
        lines.append(f"{index}. {heading}")
        lines.extend(f"   {FIELD_LABELS[name]}: {_plain_value(record[name])}" for name in rest)
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _csv_value(value):
    # Stop spreadsheet programs from running a title as a formula. Empty values become empty cells.
    if value is None:
        return ""
    if isinstance(value, str):
        return "'" + value if value.startswith(_CSV_TRIGGERS) else value
    return value


def _write_csv(path, records, fieldnames, metadata, images=None, per_page=None):
    # CSV has no room for metadata without breaking the table, so only records are written.
    # The status is therefore not stored in CSV. Use JSON or TXT if you need it.
    # utf-8-sig adds a BOM so Excel reads non-Latin characters correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({key: _csv_value(value) for key, value in record.items()})


def _write_json(path, records, fieldnames, metadata, images=None, per_page=None):
    # Metadata (including status, author, and homepage) and records together in one JSON object.
    payload = {**metadata, "records": records}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _md_text(value):
    # Make text safe for a Markdown table cell.
    return _clean(value).translate(_MD_SPECIAL)


def _md_thumbnail(url):
    # A picture in a Markdown table. The width keeps the table readable.
    if not url:
        return "-"
    src = html_escape(quote(url, safe=URL_SAFE_CHARS), quote=True)
    return f'<img src="{src}" width="160" alt="">'


def _md_cell(name, value):
    # One Markdown table cell for one field.
    if name == "thumbnail":
        return _md_thumbnail(value)
    if name == "views":
        return "" if value is None else str(value)
    if value is None or value == "":
        return ""
    if name == "url":
        # Encoding removes spaces and <> so the link stays valid and the pipe cannot break the table.
        return "<" + quote(_clean(value), safe=URL_SAFE_CHARS) + ">"
    return _md_text(value)


def _write_md(path, records, fieldnames, metadata, images=None, per_page=None):
    # Markdown table with centered pictures for thumbnails and clickable URLs.
    headers = ["No", *(FIELD_LABELS[name] for name in fieldnames)]
    alignment = [":--:", *(_MD_ALIGN.get(name, ":--") for name in fieldnames)]
    lines = [
        "# Eporner Video Listing",
        "",
        f"> **Source:** {metadata['source_url']}  ",
        f"> **Total videos:** {metadata['total_videos']}  ",
        f"> **Status:** {metadata['status']}  ",
        f"> **Scraped at:** {metadata['scraped_at']}  ",
        f"> **Author:** {metadata['author']}  ",
        f"> **Homepage:** {metadata['homepage']}",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(alignment) + " |",
    ]
    for index, record in enumerate(records, start=1):
        cells = [str(index), *(_md_cell(name, record[name]) for name in fieldnames)]
        lines.append("| " + " | ".join(cells) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _html_cell(name, value):
    # One HTML table cell. Thumbnails are pictures, URLs are links, and all text is escaped.
    if name == "thumbnail":
        if not value:
            return "-"
        src = html_escape(value, quote=True)
        return f'<img src="{src}" alt="" loading="lazy" referrerpolicy="no-referrer">'
    if value is None or value == "":
        return ""
    if name == "url":
        href = html_escape(value, quote=True)
        return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{html_escape(value)}</a>'
    return html_escape(_clean(value))


def _write_html(path, records, fieldnames, metadata, images=None, per_page=DEFAULT_PER_PAGE):
    # A single HTML file with inline styling. Rows are split into pages, so a long list does not
    # run down the whole screen. Without JavaScript, every row is shown.
    per_page = per_page or DEFAULT_PER_PAGE
    head = '<th class="num">No</th>' + "".join(
        f'<th class="{_HTML_CLASS[name]}">{html_escape(FIELD_LABELS[name])}</th>' for name in fieldnames
    )
    rows = []
    for index, record in enumerate(records, start=1):
        cells = [f'<td class="num">{index}</td>']
        cells.extend(
            f'<td class="{_HTML_CLASS[name]}">{_html_cell(name, record[name])}</td>' for name in fieldnames
        )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    homepage = html_escape(metadata["homepage"], quote=True)
    pager = "\n".join([
        '<nav class="pager" id="pager" hidden>',
        '<button type="button" id="prev">Previous</button>',
        '<span id="pager-info"></span>',
        '<button type="button" id="next">Next</button>',
        "</nav>",
    ])
    script = _PAGER_SCRIPT.replace("__PER_PAGE__", str(int(per_page)))
    page = "\n".join([
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Eporner Video Listing</title>",
        f"<style>{_HTML_STYLE}</style>",
        "</head>",
        "<body>",
        "<h1>Eporner Video Listing</h1>",
        f'<p class="meta">Source: {html_escape(metadata["source_url"])} &middot; '
        f'Total videos: {metadata["total_videos"]} &middot; '
        f'Status: {html_escape(metadata["status"])} &middot; '
        f'Scraped at: {html_escape(metadata["scraped_at"])}</p>',
        '<div class="table-wrap"><table>',
        f"<thead><tr>{head}</tr></thead>",
        "<tbody>",
        *rows,
        "</tbody>",
        "</table></div>",
        pager,
        f"<script>{script}</script>",
        f'<footer>Author: {html_escape(metadata["author"])} &middot; '
        f'<a href="{homepage}" rel="noopener noreferrer">{html_escape(metadata["homepage"])}</a></footer>',
        "</body>",
        "</html>",
    ])
    path.write_text(page + "\n", encoding="utf-8")


def _write_pdf(path, records, fieldnames, metadata, images=None, per_page=None):
    # reportlab is imported here so it is only needed when PDF output is used.
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    images = images or {}

    # Colors
    navy = colors.HexColor("#1F2A44")
    accent = colors.HexColor("#2F6DB5")
    stripe = colors.HexColor("#F2F5FA")
    rule = colors.HexColor("#C9D3E3")
    muted = colors.HexColor("#5B6577")

    # Page layout: landscape A4. Thumbnail and views have fixed widths, title and URL share the rest.
    page_size = landscape(A4)
    margin = 28
    number_width = 40
    text_width = page_size[0] - 2 * margin - number_width
    fixed_total = sum(PDF_FIXED_WIDTHS[name] for name in fieldnames if name in PDF_FIXED_WIDTHS)
    flex_total = sum(PDF_FLEX_WEIGHTS[name] for name in fieldnames if name in PDF_FLEX_WEIGHTS)
    widths = [number_width]
    for name in fieldnames:
        if name in PDF_FIXED_WIDTHS:
            widths.append(PDF_FIXED_WIDTHS[name])
        else:
            widths.append((text_width - fixed_total) * PDF_FLEX_WEIGHTS[name] / flex_total)

    # Text styles
    base = getSampleStyleSheet()["BodyText"]
    title_style = ParagraphStyle(
        "title", parent=base, fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=navy, spaceAfter=4
    )
    meta_style = ParagraphStyle("meta", parent=base, fontSize=8.5, leading=12, textColor=muted)
    head_style = ParagraphStyle(
        "head", parent=base, fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white
    )
    head_center_style = ParagraphStyle("headc", parent=head_style, alignment=TA_CENTER)
    cell_style = ParagraphStyle("cell", parent=base, fontSize=7.5, leading=9.5, textColor=colors.black)
    # URLs have no spaces, so they wrap by character to stay inside their column.
    link_style = ParagraphStyle("link", parent=cell_style, textColor=accent, wordWrap="CJK")
    views_style = ParagraphStyle("views", parent=cell_style, alignment=TA_RIGHT)
    index_style = ParagraphStyle("index", parent=cell_style, textColor=muted, alignment=TA_RIGHT)
    placeholder_style = ParagraphStyle("placeholder", parent=cell_style, textColor=muted, alignment=TA_CENTER)
    field_styles = {"title": cell_style, "url": link_style, "views": views_style}

    def thumbnail_cell(url):
        # Fit the downloaded picture inside the thumbnail box. Missing or unreadable pictures show a dash.
        data = images.get(url) if url else None
        if data is None:
            return Paragraph("-", placeholder_style)
        try:
            width, height = ImageReader(io.BytesIO(data)).getSize()
        except Exception:
            return Paragraph("-", placeholder_style)
        if not width or not height:
            return Paragraph("-", placeholder_style)
        scale = min(THUMB_BOX[0] / width, THUMB_BOX[1] / height)
        return Image(io.BytesIO(data), width=width * scale, height=height * scale)

    # Build the table rows. The header row is first.
    headers = [FIELD_LABELS[name] for name in fieldnames]
    header_cells = [Paragraph("No", head_style)]
    header_cells.extend(
        Paragraph(escape(label), head_center_style if name == "thumbnail" else head_style)
        for label, name in zip(headers, fieldnames)
    )
    rows = [header_cells]
    for index, record in enumerate(records, start=1):
        row = [Paragraph(str(index), index_style)]
        for name in fieldnames:
            value = record[name]
            if name == "thumbnail":
                row.append(thumbnail_cell(value))
            else:
                text = "" if value is None else _clean(value)
                row.append(Paragraph(escape(text), field_styles[name]))
        rows.append(row)

    # Table styling: navy header, striped rows, thin grid. Thumbnails are centered in their column.
    table = Table(rows, colWidths=widths, repeatRows=1)  # repeatRows repeats the header on each page.
    style_commands = [
        ("BACKGROUND", (0, 0), (-1, 0), navy),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, accent),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, stripe]),
        ("GRID", (0, 0), (-1, -1), 0.25, rule),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    if "thumbnail" in fieldnames:
        thumb_column = fieldnames.index("thumbnail") + 1  # Column 0 is the number column.
        style_commands.append(("ALIGN", (thumb_column, 0), (thumb_column, -1), "CENTER"))
    table.setStyle(TableStyle(style_commands))

    story = [
        Paragraph("Eporner Video Listing", title_style),
        Paragraph(
            escape(
                f"Source: {metadata['source_url']}  |  Total videos: {metadata['total_videos']}  |  "
                f"Status: {metadata['status']}  |  Scraped at: {metadata['scraped_at']}"
            ),
            meta_style,
        ),
        Paragraph(
            escape(f"Author: {metadata['author']}  |  Homepage: {metadata['homepage']}"),
            meta_style,
        ),
        Spacer(1, 12),
        table,
    ]

    def draw_footer(canvas, doc):
        # Draw a footer line, author and homepage on the left, and page number on the right.
        canvas.saveState()
        canvas.setStrokeColor(rule)
        canvas.line(margin, 24, page_size[0] - margin, 24)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(margin, 14, f"{metadata['author']}  |  {metadata['homepage']}")
        canvas.drawRightString(page_size[0] - margin, 14, f"Page {doc.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(
        str(path),
        pagesize=page_size,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin + 8,
        title="Eporner Video Listing",
        author=AUTHOR,
    )
    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)


# Map each format name to its writer function. Every writer takes the same arguments.
WRITERS = {
    "txt": _write_txt,
    "csv": _write_csv,
    "json": _write_json,
    "md": _write_md,
    "html": _write_html,
    "pdf": _write_pdf,
}


def save_records(path, fmt, records, fieldnames, metadata, images, per_page):
    # Write to a .tmp file first, then rename it. If writing fails, delete the .tmp file.
    tmp = path.with_name(path.name + ".tmp")
    try:
        WRITERS[fmt](tmp, records, fieldnames, metadata, images, per_page)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def resolve_output(raw, fmt):
    # Expand "~", then add the format extension if the user did not type it.
    # A folder name is refused, and a name that ends in a different format is refused too,
    # so "hasil.json" with -f md is not silently saved as "hasil.json.md".
    text = raw.strip()
    if text.endswith(("/", "\\")) or text in ("~", ".", ".."):
        raise ValidationError("output must be a file name, not a folder")
    path = Path(text).expanduser()
    if not path.name or path.name in (".", ".."):
        raise ValidationError("output must be a file name, not a folder")
    suffix = path.suffix.lower().lstrip(".")
    if suffix == fmt:
        return path
    if suffix in FORMATS:
        raise ValidationError(
            f"the name ends in .{suffix} but -f is {fmt}. "
            f"Use a name ending in .{fmt}, or a name without an extension"
        )
    return path.with_name(path.name + f".{fmt}")


def _pdf_ready():
    # True if reportlab can be imported, which PDF output needs.
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


def _check_folder_writable(folder):
    # Create the folder if needed and confirm that a file can be written in it.
    probe = folder / ".write_probe"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"folder is not writable: {folder} ({exc})") from exc
    finally:
        probe.unlink(missing_ok=True)


def ensure_output_ready(path, fmt):
    # Check the output path and dependencies before scraping starts.
    if path.is_dir():
        raise ValidationError(f"output path is a folder: {path}")
    if fmt == "pdf" and not _pdf_ready():
        raise ValidationError("format pdf needs reportlab, install it with: pip install reportlab")
    _check_folder_writable(path.parent)


# ---------------------------------------------------------------------------
# Job and outcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Job:
    # All settings for one run, taken from the command line.
    url: str
    output: Path | None
    fmt: str
    fields: str
    workers: int
    attempts: int  # Tries per page before it is reported as failed.
    per_page: int  # Rows per page in HTML output.
    save_path: Path | None  # Where progress is written, or None for no checkpoint file.
    resume_path: Path | None  # Where progress is read from, or None to start from the beginning.
    verbose: bool


@dataclass(frozen=True)
class Outcome:
    # Result of a run, used for the summary.
    records: list
    output: Path | None
    fmt: str
    fieldnames: list
    elapsed: float
    complete: bool  # True only if the end of the listing was confirmed and every page was saved.


def fieldnames_for(fields):
    # "url" gives only the URL column. "all" gives title, thumbnail, views, and URL, in that order.
    return ["url"] if fields == "url" else list(ALL_FIELDS)


def _shape(videos, fields):
    # Keep only the columns the user asked for.
    keys = fieldnames_for(fields)
    return [{key: video[key] for key in keys} for video in videos]


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def run(job):
    # Load progress if asked to, crawl, save output, and report what was kept.
    started = time.monotonic()
    checkpoint = Checkpoint(job.save_path, job.url)
    if job.resume_path is not None:
        checkpoint.load(job.resume_path)
        if checkpoint.covers_listing():
            log("The resumed checkpoint already covers the whole listing.", "info")

    complete, error = crawl(job, checkpoint)
    if error:
        log(error, "error")

    fieldnames = fieldnames_for(job.fields)
    records = _shape(checkpoint.videos, job.fields)
    # A listing with no videos is never reported as complete.
    complete = complete and bool(records)
    saved = False

    if not records:
        log("No videos were extracted, so nothing was saved. Check the URL, network, or site structure.", "warn")
    elif job.output is None:
        log("No output path given, results were not saved.", "warn")
    elif not complete and job.output.exists():
        # An unfinished run must not replace a file that may hold a complete earlier result.
        log(f"Results are incomplete and {job.output} already exists, so it was not overwritten.", "warn")
    else:
        if job.output.exists():
            log(f"Replacing existing file {job.output}", "warn")
        metadata = {
            "source_url": job.url,
            "total_videos": len(records),
            "status": "complete" if complete else "incomplete",
            "scraped_at": datetime.now().isoformat(),
            "fields": fieldnames,
            "author": AUTHOR,
            "homepage": HOMEPAGE,
        }
        # Only the PDF embeds pictures, so only the PDF downloads them.
        images = {}
        if job.fmt == "pdf" and "thumbnail" in fieldnames:
            images = _download_thumbnails(job, records)
        log(f"Saving {len(records)} records to {job.output} ({job.fmt})...", "step")
        save_records(job.output, job.fmt, records, fieldnames, metadata, images, job.per_page)
        log(f"Saved to {job.output}", "success")
        saved = True

    # Checkpoint files are never deleted automatically. They stay until the user removes them.
    if not complete:
        if job.save_path is not None:
            log(
                f"Results are incomplete. Progress kept in {job.save_path}, "
                f"run with -R {job.save_path} to continue.",
                "warn",
            )
        else:
            log("Results are incomplete and no checkpoint was saved. Use -s FILE next time to keep progress.", "warn")
    elif job.save_path is not None:
        log(f"Checkpoint kept at {job.save_path}.", "info")

    return Outcome(
        records=records,
        output=job.output if saved else None,
        fmt=job.fmt,
        fieldnames=fieldnames,
        elapsed=time.monotonic() - started,
        complete=complete,
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _bounded(low, high):
    # Build an argparse type that accepts integers between low and high.
    def parse(text):
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value

    # argparse uses this name in its error message, for example "invalid integer 1-10 value".
    parse.__name__ = f"integer {low}-{high}"
    return parse


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="userporner.py",
        description=(
            "Scrape video listings from an Eporner profile.\n"
            f"author: {AUTHOR}  |  homepage: {HOMEPAGE}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,  # Reject shortened long options like --keep.
    )
    parser.add_argument(
        "-u", "--url", required=True,
        help="Eporner profile URL or username",
    )
    parser.add_argument(
        "-o", "--output",
        help="output file path, nothing is saved if omitted",
    )
    parser.add_argument(
        "-f", "--format", choices=FORMATS, default=DEFAULT_FORMAT,
        help=f"output format: {', '.join(FORMATS)} (default: {DEFAULT_FORMAT})",
    )
    parser.add_argument(
        "-F", "--fields", choices=FIELD_CHOICES, default=DEFAULT_FIELDS,
        help=f"all = title, thumbnail, views, url; url = url only (default: {DEFAULT_FIELDS})",
    )
    parser.add_argument(
        "-p", "--per-page", type=_bounded(1, MAX_PER_PAGE), default=None,
        help=f"rows per page in HTML output, 1-{MAX_PER_PAGE} (HTML only, default: {DEFAULT_PER_PAGE})",
    )
    parser.add_argument(
        "-w", "--workers", type=_bounded(1, MAX_WORKERS), default=DEFAULT_WORKERS,
        help=f"parallel requests, 1-{MAX_WORKERS} (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "-r", "--retries", type=_bounded(MIN_RETRIES, MAX_RETRIES), default=DEFAULT_RETRIES,
        help=f"attempts per page, {MIN_RETRIES}-{MAX_RETRIES} (default: {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "-s", "--save-checkpoint", metavar="FILE",
        help="save progress to FILE after every page, so the run can be continued with -R",
    )
    parser.add_argument(
        "-R", "--resume", metavar="FILE",
        help="continue an earlier run from FILE, a checkpoint made with -s",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="show the time taken for each batch of pages",
    )

    args = parser.parse_args(argv)

    if normalize_profile_url(args.url) is None:
        parser.error("-u/--url must be an Eporner profile URL or a username")
    for flag, value in (("-o/--output", args.output),
                        ("-s/--save-checkpoint", args.save_checkpoint),
                        ("-R/--resume", args.resume)):
        if value is not None and not value.strip():
            parser.error(f"{flag} must not be empty")
    if args.per_page is not None and args.format != "html":
        parser.error("-p/--per-page only applies to -f html")
    return args


def _path_or_none(text):
    # Turn an optional path from the command line into a Path, or None when it was not given.
    return Path(text).expanduser() if text else None


def _same_file(a, b):
    # True only when both paths are set and point to the same file.
    return a is not None and b is not None and a.resolve() == b.resolve()


def job_from_args(args):
    # Convert parsed CLI arguments into a Job, and refuse combinations that could lose data.
    save_path = _path_or_none(args.save_checkpoint)
    resume_path = _path_or_none(args.resume)
    output = resolve_output(args.output, args.format) if args.output else None

    if save_path is not None:
        if save_path.is_dir():
            raise ValidationError(f"checkpoint path is a folder: {save_path}")
        # Saving must never overwrite an older checkpoint, unless that is the file being resumed.
        if save_path.exists() and not _same_file(save_path, resume_path):
            raise ValidationError(
                f"{save_path} already exists. Use -R {save_path} to continue it, "
                "or choose another name for -s."
            )
        _check_folder_writable(save_path.parent)

    # The output file would replace the checkpoint, and the next resume would then fail.
    if output is not None and (_same_file(output, save_path) or _same_file(output, resume_path)):
        raise ValidationError("the output file cannot be the same as the checkpoint file")

    url = normalize_profile_url(args.url)
    return Job(
        url=url,
        output=output,
        fmt=args.format,
        fields=args.fields,
        workers=args.workers,
        attempts=args.retries,
        per_page=args.per_page if args.per_page is not None else DEFAULT_PER_PAGE,
        save_path=save_path,
        resume_path=resume_path,
        verbose=args.verbose,
    )


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_summary(outcome, job):
    # Print a plain text summary with no boxes.
    status = (
        f"{Fore.GREEN}COMPLETE{Style.RESET_ALL}"
        if outcome.complete
        else f"{Fore.RED}INCOMPLETE{Style.RESET_ALL}"
    )
    if outcome.output:
        saved = str(outcome.output)
    elif job.output:
        saved = "not saved (see messages above)"
    else:
        saved = "not saved (use -o)"
    print()
    print(f"Source     : {job.url}")
    print(f"Records    : {len(outcome.records)}")
    print(f"Output     : {saved}")
    print(f"Format     : {outcome.fmt if outcome.output else '-'}")
    print(f"Fields     : {', '.join(outcome.fieldnames)}")
    print(f"Checkpoint : {job.save_path if job.save_path else 'none'}")
    print(f"Elapsed    : {outcome.elapsed:.1f}s")
    print(f"Status     : {status}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    # Set up UTF-8 output and terminal colors, then run the scraper.
    # Return the exit code.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    init(autoreset=True)
    _STOP.clear()

    args = parse_args(argv)
    try:
        job = job_from_args(args)
        if job.output is not None:
            ensure_output_ready(job.output, job.fmt)
        outcome = run(job)
    except KeyboardInterrupt:
        print(flush=True)
        if args.save_checkpoint:
            log("Stopped by user. Progress saved with -s is kept, run with -R to continue.", "warn")
        else:
            log("Stopped by user. Nothing was saved, use -s FILE next time to keep progress.", "warn")
        return 130
    except ValidationError as exc:
        log(f"Validation error: {exc}", "error")
        return 2
    except Exception as exc:
        log(f"Fatal error: {type(exc).__name__}: {exc}", "error")
        return 1
    finally:
        # Tell any worker thread that is still running to stop.
        _STOP.set()

    print_summary(outcome, job)
    return 0 if outcome.complete else 1


if __name__ == "__main__":
    sys.exit(main())
