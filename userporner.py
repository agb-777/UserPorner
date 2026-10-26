# userporner.py
# Export the video list of an Eporner profile to CSV, JSON, Markdown, or PDF.
# Author: agb-777
# Homepage: https://github.com/agb-777/userporner

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.sax.saxutils import escape

from colorama import Fore, Style, init

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTHOR = "agb-777"
HOMEPAGE = "https://github.com/agb-777/userporner"

# Each profile gets its own checkpoint file, so two profiles never overwrite each other.
CHECKPOINT_TEMPLATE = ".scraper_checkpoint_{}.json"
DEFAULT_FORMAT = "csv"
DEFAULT_FIELDS = "all"
DEFAULT_WORKERS = 3
DEFAULT_RETRIES = 3
FORMATS = ("csv", "json", "md", "pdf")
FIELD_CHOICES = ("all", "url")
FIELD_LABELS = {"title": "Title", "url": "URL"}

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

# Network settings, in seconds unless noted.
REQUEST_TIMEOUT = 20
REQUEST_DELAY = 0.5  # Pause before each request, per worker, to keep the load on the site low.
RETRY_DELAY = 2  # Multiplied by the attempt number, so retries back off.
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

# Characters that have a meaning in Markdown and must be escaped inside table cells.
_MD_SPECIAL = str.maketrans({char: "\\" + char for char in "\\`*_[]<>|"})
# Control characters that are not whitespace. They can break PDF generation.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0e-\x1b\x7f]")
# A CSV cell starting with one of these may be run as a formula by spreadsheet programs.
_CSV_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

# Reasons used in log messages when a page marks the end of the listing.
END_REASONS = {"missing": "does not exist", "end": "is empty"}

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


def checkpoint_name(listing_url):
    # Build the default checkpoint file name for one profile.
    username = listing_url.split("/profile/", 1)[1].split("/", 1)[0]
    return CHECKPOINT_TEMPLATE.format(username)


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


def _urls(videos):
    # The set of video URLs in a list of {title, url} dictionaries.
    return {video["url"] for video in videos}


class _VideoParser(HTMLParser):
    # Collects video links from one listing page.
    # Each link is tagged with whether it sits inside a div with class "mb",
    # which is the listing container the browser version used.

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self._div_stack = []  # One flag per open div: True if it is a listing container.
        self._href = None  # Absolute URL of the video link we are inside, or None.
        self._in_container = False
        self._title = ""
        self._alt = ""
        self._text = []
        self.videos = []  # (url, title, in_container) in page order.

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "div":
            self._div_stack.append("mb" in (attr.get("class") or "").split())
            return
        if self._href is not None:
            if tag == "img" and not self._alt:
                self._alt = (attr.get("alt") or "").strip()
            return
        if tag != "a":
            return
        href = attr.get("href") or ""
        if VIDEO_MARKER not in href:
            return
        # Relative links are resolved against the page URL, like a browser does.
        absolute = urljoin(self._base, href)
        # Ignore video-looking links that point to other sites, such as ads.
        if not _on_site(absolute):
            return
        self._href = absolute
        self._in_container = any(self._div_stack)
        self._title = (attr.get("title") or "").strip()
        self._alt = ""
        self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "div":
            if self._div_stack:
                self._div_stack.pop()
            return
        if tag != "a" or self._href is None:
            return
        # Title order: title attribute, then image alt text, then visible text.
        title = self._title or self._alt or _clean("".join(self._text))
        self.videos.append((self._href, title, self._in_container))
        self._href = None


def parse_videos(html, page_url_value):
    # Return a list of {title, url} dictionaries from one listing page.
    parser = _VideoParser(page_url_value)
    parser.feed(html)
    parser.close()
    # Prefer links inside listing containers. Fall back to every video link if the page has none.
    chosen = [item for item in parser.videos if item[2]] or parser.videos
    found = {}
    for url, title, _ in chosen:
        known = found.get(url, "")
        # Keep the first non-empty title seen for each URL.
        if url not in found or (title and not known):
            found[url] = title
    return [{"title": title, "url": url} for url, title in found.items()]


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
        return response.geturl(), response.read(), response.headers.get_content_charset()


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
            # Covers timeouts, connection resets, and incomplete reads.
            reason = type(exc).__name__
        else:
            try:
                html = _decode(raw, charset)
                if not _is_listing(final_url, number):
                    vote = "missing"
                else:
                    videos = parse_videos(html, final_url)
                    if videos:
                        return PageResult("ok", videos)
                    if LISTING_MARKER in html:
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


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------

def _is_video(item):
    # Check that a stored item has the right shape before using it.
    return (
        isinstance(item, dict)
        and isinstance(item.get("url"), str)
        and bool(item["url"])
        and isinstance(item.get("title"), str)
    )


def _is_page_key(key):
    # Page keys are stored as strings like "3". Check that they are digits within the valid range.
    return isinstance(key, str) and key.isascii() and key.isdigit() and 1 <= int(key) <= SAFETY_PAGE_LIMIT


class Checkpoint:
    # Saves each finished page to disk so an interrupted run can resume.

    def __init__(self, path, source_url):
        self.path = Path(path)
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
        # Every video URL stored so far.
        return {video["url"] for page in self.pages.values() for video in page}

    def covers_listing(self):
        # True when the end is known and every page up to it is stored.
        return self.last_page is not None and all(n in self.pages for n in range(1, self.last_page + 1))

    def load(self):
        # Read the checkpoint file.
        # A file that belongs to another profile is never overwritten, because that would lose its resume data.
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log(f"Checkpoint unreadable ({exc}), starting fresh.", "warn")
            return
        if not isinstance(data, dict):
            log("Checkpoint is invalid, starting fresh.", "warn")
            return
        if data.get("source_url") != self.source_url:
            if data.get("source_url"):
                raise ValueError(
                    f"checkpoint {self.path} belongs to another profile ({data['source_url']}). "
                    "Use -c with another file name, or -n to replace it."
                )
            log("Checkpoint has no source URL, starting fresh.", "warn")
            return

        last = data.get("last_page")
        if isinstance(last, int) and not isinstance(last, bool) and 0 <= last <= SAFETY_PAGE_LIMIT:
            self.last_page = last

        stored = data.get("pages")
        if isinstance(stored, dict):
            for key, videos in stored.items():
                if _is_page_key(key) and isinstance(videos, list):
                    self.pages[int(key)] = [video for video in videos if _is_video(video)]

        if self.pages or self.last_page is not None:
            log(f"Resuming from checkpoint: {len(self.pages)} pages, {len(self.videos)} videos", "success")

    def record(self, number, videos):
        # Store one page and save the whole checkpoint.
        self.pages[number] = videos
        self._save()

    def set_last_page(self, number):
        # Mark the end of the listing and save it.
        self.last_page = number
        self._save()

    def discard(self):
        # Delete the checkpoint file.
        self.path.unlink(missing_ok=True)

    def _save(self):
        # Write to a .tmp file first, then rename it. This prevents a half-written checkpoint.
        payload = {
            "source_url": self.source_url,
            "timestamp": datetime.now().isoformat(),
            "last_page": self.last_page,
            "pages": {str(number): videos for number, videos in sorted(self.pages.items())},
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log(f"Checkpoint could not be saved ({exc}); resume may not be available.", "warn")


# ---------------------------------------------------------------------------
# Crawling
# ---------------------------------------------------------------------------

def _already_seen(checkpoint, videos):
    # Some sites repeat earlier content for out-of-range page numbers.
    # A page whose videos were all collected before is a repeat, not new content.
    return bool(videos) and _urls(videos) <= checkpoint.known_urls()


def crawl(job, checkpoint):
    # Fetch pages in parallel batches until the end of the listing is confirmed.
    # Return (complete, error_message).
    while True:
        if checkpoint.last_page is not None:
            pending = [n for n in range(1, checkpoint.last_page + 1) if n not in checkpoint.pages]
            if not pending:
                return True, ""
            batch = pending[: job.workers]
        else:
            first = 1
            while first in checkpoint.pages:
                first += 1
            if first > SAFETY_PAGE_LIMIT:
                return False, "reached the safety page limit"
            batch = list(range(first, first + job.workers))

        began = time.monotonic()
        log(f"Fetching pages {batch[0]}-{batch[-1]}...", "step")
        results = _fetch_batch(job, batch)
        if job.verbose:
            log(f"Batch finished in {time.monotonic() - began:.1f}s", "dim")

        # Process results in page order, so the end of the listing is found correctly.
        for number, result in zip(batch, results):
            if result.kind == "fail":
                return False, f"page {number} failed ({result.reason}), run again to resume"

            ends_listing = result.kind in END_REASONS or (
                result.kind == "ok" and _already_seen(checkpoint, result.videos)
            )
            if ends_listing:
                # A checkpoint already knows where the listing ends. If that changed, do not shrink
                # the listing silently, because that would drop videos from the output.
                if checkpoint.last_page is not None:
                    return False, f"page {number} no longer matches the checkpoint, run with -n to start over"
                # An empty first page is never trusted as a finished listing. It usually means a wrong
                # username or a changed page layout.
                if number == 1:
                    return False, "page 1 has no videos or does not exist, check the username or the page layout"
                reason = END_REASONS.get(result.kind, "repeats earlier pages")
                checkpoint.set_last_page(number - 1)
                log(f"Page {number} {reason}, last page is {number - 1}.", "success")
                break

            checkpoint.record(number, result.videos)
            log(f"Page {number}: {len(result.videos)} videos", "success")


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _md_text(value):
    # Make text safe for a Markdown table cell.
    return _clean(value).translate(_MD_SPECIAL)


def _csv_safe(value):
    # Stop spreadsheet programs from running a title as a formula.
    return "'" + value if value.startswith(_CSV_TRIGGERS) else value


def _write_csv(path, records, fieldnames, metadata):
    # CSV has no room for metadata without breaking the table, so only records are written.
    # utf-8-sig adds a BOM so Excel reads non-Latin characters correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({key: _csv_safe(value) for key, value in record.items()})


def _write_json(path, records, fieldnames, metadata):
    # Metadata (including author and homepage) and records together in one JSON object.
    payload = {**metadata, "records": records}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_md(path, records, fieldnames, metadata):
    # Markdown table. URLs are wrapped in <> so they become clickable links.
    headers = ["No", *(FIELD_LABELS[name] for name in fieldnames)]
    alignment = [":--:", *([":--"] * len(fieldnames))]
    lines = [
        "# Eporner Video Listing",
        "",
        f"> **Source:** {metadata['source_url']}  ",
        f"> **Total videos:** {metadata['total_videos']}  ",
        f"> **Scraped at:** {metadata['scraped_at']}  ",
        f"> **Author:** {metadata['author']}  ",
        f"> **Homepage:** {metadata['homepage']}",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(alignment) + " |",
    ]
    for index, record in enumerate(records, start=1):
        cells = [str(index)]
        for name in fieldnames:
            if name == "url":
                # A pipe would break the table, so it is replaced with its percent-encoded form.
                url = _clean(record[name]).replace("|", "%7C")
                cells.append(f"<{url}>")
            else:
                cells.append(_md_text(record[name]))
        lines.append("| " + " | ".join(cells) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_pdf(path, records, fieldnames, metadata):
    # reportlab is imported here so it is only needed when PDF output is used.
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    # Colors
    navy = colors.HexColor("#1F2A44")
    accent = colors.HexColor("#2F6DB5")
    stripe = colors.HexColor("#F2F5FA")
    rule = colors.HexColor("#C9D3E3")
    muted = colors.HexColor("#5B6577")

    # Page layout: landscape A4, with the number column fixed and the text columns sharing the rest.
    page_size = landscape(A4)
    margin = 28
    usable = page_size[0] - 2 * margin
    number_width = 40
    text_width = usable - number_width
    shares = [0.45, 0.55] if len(fieldnames) == 2 else [1.0]
    widths = [number_width, *(text_width * share for share in shares)]

    # Text styles
    base = getSampleStyleSheet()["BodyText"]
    title_style = ParagraphStyle(
        "title", parent=base, fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=navy, spaceAfter=4
    )
    meta_style = ParagraphStyle("meta", parent=base, fontSize=8.5, leading=12, textColor=muted)
    head_style = ParagraphStyle(
        "head", parent=base, fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white
    )
    cell_style = ParagraphStyle("cell", parent=base, fontSize=7.5, leading=9.5, textColor=colors.black)
    link_style = ParagraphStyle("link", parent=cell_style, textColor=accent)
    index_style = ParagraphStyle("index", parent=cell_style, textColor=muted, alignment=TA_RIGHT)

    # Build the table rows. The header row is first.
    headers = ["No", *(FIELD_LABELS[name] for name in fieldnames)]
    rows = [[Paragraph(escape(header), head_style) for header in headers]]
    for index, record in enumerate(records, start=1):
        row = [Paragraph(str(index), index_style)]
        for name in fieldnames:
            style = link_style if name == "url" else cell_style
            row.append(Paragraph(escape(_clean(record[name])), style))
        rows.append(row)

    # Table styling: navy header, striped rows, thin grid.
    table = Table(rows, colWidths=widths, repeatRows=1)  # repeatRows repeats the header on each page.
    table.setStyle(
        TableStyle(
            [
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
        )
    )

    story = [
        Paragraph("Eporner Video Listing", title_style),
        Paragraph(
            escape(
                f"Source: {metadata['source_url']}  |  Total videos: {metadata['total_videos']}  |  "
                f"Scraped at: {metadata['scraped_at']}"
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


# Map each format name to its writer function.
WRITERS = {"csv": _write_csv, "json": _write_json, "md": _write_md, "pdf": _write_pdf}


def save_records(path, fmt, records, fieldnames, metadata):
    # Write to a .tmp file first, then rename it. If writing fails, delete the .tmp file.
    tmp = path.with_name(path.name + ".tmp")
    try:
        WRITERS[fmt](tmp, records, fieldnames, metadata)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def resolve_output(raw, fmt):
    # Add the format extension if the user did not type it.
    path = Path(raw)
    if not path.name or path.name in (".", ".."):
        raise ValueError("output must be a file name, not a folder")
    if path.suffix.lower() == f".{fmt}":
        return path
    return path.with_name(path.name + f".{fmt}")


def ensure_output_ready(path, fmt):
    # Check the output folder is writable and reportlab is installed before scraping starts.
    if fmt == "pdf":
        try:
            import reportlab  # noqa: F401
        except ImportError as exc:
            raise ValueError("format pdf needs reportlab, install it with: pip install reportlab") from exc
    folder = path.parent
    probe = folder / ".write_probe"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"output folder is not writable: {folder} ({exc})") from exc
    finally:
        probe.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Job and outcome
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Job:
    # All settings for one run, after parsing CLI or interactive input.
    url: str
    output: Path | None
    fmt: str
    fields: str
    workers: int
    attempts: int  # Tries per page before it is reported as failed.
    checkpoint_path: Path
    keep_checkpoint: bool
    fresh: bool  # True means delete the old checkpoint first.
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
    # "url" gives only the URL column. "all" gives title and URL.
    return ["url"] if fields == "url" else ["title", "url"]


def _shape(videos, fields):
    # Keep only the columns the user asked for.
    keys = fieldnames_for(fields)
    return [{key: video[key] for key in keys} for video in videos]


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def run(job):
    # Load checkpoint, crawl, save output, and decide what to keep.
    started = time.monotonic()
    checkpoint = Checkpoint(job.checkpoint_path, job.url)
    if job.fresh:
        checkpoint.discard()
    else:
        checkpoint.load()
        if checkpoint.covers_listing():
            log("Checkpoint already covers the whole listing. Use -n to fetch it again.", "info")

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
        metadata = {
            "source_url": job.url,
            "total_videos": len(records),
            "scraped_at": datetime.now().isoformat(),
            "fields": fieldnames,
            "author": AUTHOR,
            "homepage": HOMEPAGE,
        }
        log(f"Saving {len(records)} records to {job.output} ({job.fmt})...", "step")
        save_records(job.output, job.fmt, records, fieldnames, metadata)
        log(f"Saved to {job.output}", "success")
        saved = True

    # The checkpoint is only deleted when the run is complete AND the results are in a file.
    # Otherwise it is the only copy of the data, so it stays.
    if not complete:
        log("Results are incomplete. Checkpoint kept, run again to continue.", "warn")
    elif not saved:
        log("Checkpoint kept, because the results are not saved to a file yet.", "info")
    elif job.keep_checkpoint:
        log("Checkpoint kept (-k).", "info")
    else:
        checkpoint.discard()

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
        "-i", "--interactive", action="store_true",
        help="run in interactive mode, all settings are asked step by step",
    )

    # All options below only work in CLI mode.
    cli = parser.add_argument_group("CLI options (cannot be combined with -i)")
    cli.add_argument("-u", "--url", help="Eporner profile URL or username")
    cli.add_argument("-o", "--output", help="output file path, nothing is saved if omitted")
    cli.add_argument(
        "-f", "--format", choices=FORMATS,
        help=f"output format: {', '.join(FORMATS)} (default: {DEFAULT_FORMAT})",
    )
    cli.add_argument(
        "-F", "--fields", choices=FIELD_CHOICES,
        help=f"all = title and url, url = url only (default: {DEFAULT_FIELDS})",
    )
    cli.add_argument(
        "-w", "--workers", type=_bounded(1, MAX_WORKERS),
        help=f"parallel requests, 1-{MAX_WORKERS} (default: {DEFAULT_WORKERS})",
    )
    cli.add_argument(
        "-r", "--retries", type=_bounded(1, MAX_RETRIES),
        help=f"attempts per page, 1-{MAX_RETRIES} (default: {DEFAULT_RETRIES})",
    )
    cli.add_argument(
        "-c", "--checkpoint",
        help="checkpoint file path (default: .scraper_checkpoint_<username>.json)",
    )
    cli.add_argument(
        "-n", "--no-resume", action="store_true",
        help="ignore and delete the checkpoint file at the checkpoint path, start from scratch",
    )
    cli.add_argument(
        "-k", "--keep-checkpoint", action="store_true",
        help="keep the checkpoint file after a complete run",
    )
    cli.add_argument(
        "-v", "--verbose", action="store_true",
        help="show the time taken for each batch of pages",
    )

    args = parser.parse_args(argv)

    # Map each CLI flag to its value so we can detect flags used together with -i.
    cli_values = {
        "-u": args.url,
        "-o": args.output,
        "-f": args.format,
        "-F": args.fields,
        "-w": args.workers,
        "-r": args.retries,
        "-c": args.checkpoint,
        "-n": args.no_resume,
        "-k": args.keep_checkpoint,
        "-v": args.verbose,
    }

    if args.interactive:
        used = [flag for flag, value in cli_values.items() if value not in (None, False)]
        if used:
            parser.error(f"-i cannot be combined with {', '.join(used)}, interactive mode asks for them")
        return args

    if not args.url:
        parser.error("-u/--url is required unless -i is used")
    if normalize_profile_url(args.url) is None:
        parser.error("-u/--url must be an Eporner profile URL or a username")
    if args.output is not None and not args.output.strip():
        parser.error("-o/--output must not be empty")

    # Fill in defaults for anything the user did not set.
    args.format = args.format or DEFAULT_FORMAT
    args.fields = args.fields or DEFAULT_FIELDS
    args.workers = args.workers or DEFAULT_WORKERS
    args.retries = args.retries or DEFAULT_RETRIES
    return args


def make_job(url, fmt, fields, output, workers, attempts, checkpoint, keep_checkpoint, fresh, verbose):
    # Build a Job from plain values. Used by both CLI and interactive mode.
    return Job(
        url=url,
        output=resolve_output(output, fmt) if output else None,
        fmt=fmt,
        fields=fields,
        workers=workers,
        attempts=attempts,
        checkpoint_path=Path(checkpoint),
        keep_checkpoint=keep_checkpoint,
        fresh=fresh,
        verbose=verbose,
    )


def job_from_args(args):
    # Convert parsed CLI arguments into a Job.
    url = normalize_profile_url(args.url)
    return make_job(
        url=url,
        fmt=args.format,
        fields=args.fields,
        output=args.output,
        workers=args.workers,
        attempts=args.retries,
        checkpoint=args.checkpoint or checkpoint_name(url),
        keep_checkpoint=args.keep_checkpoint,
        fresh=args.no_resume,
        verbose=args.verbose,
    )


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def _ask(prompt, default=""):
    # Ask one question. Press Enter to accept the default.
    suffix = f" {Fore.WHITE}[{default}]{Style.RESET_ALL}" if default else ""
    return input(f"{Fore.CYAN}?{Style.RESET_ALL} {prompt}{suffix}: ").strip() or default


def _ask_choice(prompt, choices, default):
    # Ask until the answer is one of the allowed choices.
    options = "/".join(choices)
    while True:
        answer = _ask(f"{prompt} ({options})", default).lower()
        if answer in choices:
            return answer
        print(f"{Fore.YELLOW}Please choose one of: {options}{Style.RESET_ALL}")


def _ask_int(prompt, default, low, high):
    # Ask until the answer is an integer within range.
    while True:
        answer = _ask(f"{prompt} ({low}-{high})", str(default))
        try:
            value = int(answer)
        except ValueError:
            print(f"{Fore.YELLOW}Please enter a number.{Style.RESET_ALL}")
            continue
        if low <= value <= high:
            return value
        print(f"{Fore.YELLOW}Please enter a number between {low} and {high}.{Style.RESET_ALL}")


def _ask_url():
    # Ask until the input is a valid profile URL or username.
    while True:
        url = normalize_profile_url(_ask("Profile URL or username"))
        if url is not None:
            return url
        print(f"{Fore.YELLOW}Enter an Eporner profile URL or a username.{Style.RESET_ALL}")


def _ask_output():
    # Ask for the output file. An empty answer means do not save. A folder name is refused.
    while True:
        raw = _ask("Output file (empty = do not save)")
        if not raw:
            return ""
        if Path(raw).name not in ("", ".", ".."):
            return raw
        print(f"{Fore.YELLOW}Enter a file name, not a folder.{Style.RESET_ALL}")


def interactive_job():
    # Ask for settings in order. Format and fields are only asked if there is an output file.
    print(f"{Fore.CYAN}{Style.BRIGHT}Interactive mode{Style.RESET_ALL}")
    url = _ask_url()
    output = _ask_output()
    fmt, fields = DEFAULT_FORMAT, DEFAULT_FIELDS
    if output:
        fmt = _ask_choice("Format", FORMATS, DEFAULT_FORMAT)
        fields = _ask_choice("Fields", FIELD_CHOICES, DEFAULT_FIELDS)
    workers = _ask_int("Workers", DEFAULT_WORKERS, 1, MAX_WORKERS)
    attempts = _ask_int("Attempts per page", DEFAULT_RETRIES, 1, MAX_RETRIES)
    return make_job(
        url=url,
        fmt=fmt,
        fields=fields,
        output=output or None,
        workers=workers,
        attempts=attempts,
        checkpoint=checkpoint_name(url),
        keep_checkpoint=False,
        fresh=False,
        verbose=False,
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
    print(f"Source  : {job.url}")
    print(f"Records : {len(outcome.records)}")
    print(f"Output  : {saved}")
    print(f"Format  : {outcome.fmt if outcome.output else '-'}")
    print(f"Fields  : {', '.join(outcome.fieldnames)}")
    print(f"Elapsed : {outcome.elapsed:.1f}s")
    print(f"Status  : {status}")


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
        job = interactive_job() if args.interactive else job_from_args(args)
        if job.output is not None:
            ensure_output_ready(job.output, job.fmt)
        outcome = run(job)
    except KeyboardInterrupt:
        print(flush=True)
        log("Stopped by user. Run again to resume.", "warn")
        return 130
    except EOFError:
        print(flush=True)
        log("Input ended before setup was complete.", "error")
        return 2
    except ValueError as exc:
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
