# userporner.py
# Scrape the video list of an Eporner profile into CSV, JSON, Markdown, or PDF.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from colorama import Fore, Style, init
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTHOR = "agb-777"
HOMEPAGE = "https://github.com/agb-777/userporner"

DEFAULT_CHECKPOINT = ".scraper_checkpoint.json"
DEFAULT_FORMAT = "csv"
DEFAULT_FIELDS = "all"
DEFAULT_WORKERS = 3
DEFAULT_RETRIES = 3
FORMATS = ("csv", "json", "md", "pdf")
FIELD_CHOICES = ("all", "url")
FIELD_LABELS = {"title": "Title", "url": "URL"}

# Matches a profile URL and captures the username.
PROFILE_PATTERN = re.compile(r"https?://(?:www\.)?eporner\.com/profile/([^/?#]+)", re.IGNORECASE)
# Matches a listing page number such as /uploaded-videos/3/.
PAGE_PATTERN = re.compile(r"/uploaded-videos/(\d+)(?:[/?#]|$)")

# CSS selectors used to read the page. If the site changes its layout, update these.
VIDEO_MARKER = "/video-"
VIDEO_SELECTOR = f'a[href*="{VIDEO_MARKER}"]'
CONTAINER_SELECTOR = ".mb"
UPLOADS_LINK_SELECTOR = 'a[href*="/uploaded-videos/"]'
LAST_PAGE_SELECTOR = 'link[rel="last"]'
NEXT_PAGE_SELECTOR = 'link[rel="next"]'

# Timeouts and limits, in seconds unless noted.
PAGE_LOAD_TIMEOUT = 30
RENDER_TIMEOUT = 15
RETRY_DELAY = 2
SAFETY_PAGE_LIMIT = 100_000  # Hard stop so the scraper cannot loop forever.
MAX_WORKERS = 10
MAX_RETRIES = 10

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Chrome flags. Headless mode runs without a window.
CHROME_ARGUMENTS = (
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1920,1080",
    "--disable-blink-features=AutomationControlled",
    "--log-level=3",
    f"user-agent={USER_AGENT}",
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Lock so messages from several threads do not mix on one line.
_LOG_LOCK = threading.Lock()
# Lock so only one thread starts Chrome at a time. This avoids a race in
# Selenium Manager's metadata file.
_DRIVER_START_LOCK = threading.Lock()
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
# Browser management
# ---------------------------------------------------------------------------

def _build_options():
    # Build Chrome options from CHROME_ARGUMENTS and hide the automation flags.
    options = Options()
    for argument in CHROME_ARGUMENTS:
        options.add_argument(argument)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    return options


def _quit_quietly(driver):
    # Close a driver and ignore errors, because it may already be dead.
    with suppress(Exception):
        driver.quit()


class DriverPool:
    # Gives each worker thread its own Chrome driver and tracks them all for cleanup.

    def __init__(self):
        self._local = threading.local()  # One driver per thread.
        self._drivers = []
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self):
        return self._closed

    def acquire(self):
        # Return this thread's driver, creating it on first use.
        if self._closed:
            raise RuntimeError("driver pool is closed")
        driver = getattr(self._local, "driver", None)
        if driver is None:
            driver = self._create()
            self._local.driver = driver
        return driver

    def discard(self):
        # Drop this thread's driver, for example after a crash, so the next call creates a new one.
        driver = getattr(self._local, "driver", None)
        self._local.driver = None
        if driver is not None:
            _quit_quietly(driver)
            with self._lock:
                if driver in self._drivers:
                    self._drivers.remove(driver)

    def close(self):
        # Shut down every driver. Called once at the end of the program.
        self._closed = True
        with self._lock:
            drivers, self._drivers = self._drivers, []
        for driver in drivers:
            _quit_quietly(driver)

    def _create(self):
        # Start a new Chrome driver. The global lock serializes startup.
        with _DRIVER_START_LOCK:
            driver = webdriver.Chrome(options=_build_options())
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        with self._lock:
            self._drivers.append(driver)
        return driver


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def normalize_profile_url(raw):
    # Turn any profile URL into the full /uploaded-videos/ listing URL. Return None if invalid.
    match = PROFILE_PATTERN.search(raw or "")
    if match is None:
        return None
    return f"https://www.eporner.com/profile/{match.group(1)}/uploaded-videos/"


def page_url(base, number):
    # Page 1 is the base URL. Other pages are base + number + "/".
    return base if number == 1 else f"{base}{number}/"


def page_number(url):
    # Read the page number from a URL. Default to 1 when there is none.
    match = PAGE_PATTERN.search(url or "")
    return int(match.group(1)) if match else 1


# ---------------------------------------------------------------------------
# Page reading
# ---------------------------------------------------------------------------

def _wait_for_videos(driver):
    # Wait until at least one video link appears, or raise TimeoutException.
    WebDriverWait(driver, RENDER_TIMEOUT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, VIDEO_SELECTOR))
    )


def _is_empty(driver):
    # A page is empty when it has neither video containers nor video links.
    return not (
        driver.find_elements(By.CSS_SELECTOR, CONTAINER_SELECTOR)
        or driver.find_elements(By.CSS_SELECTOR, VIDEO_SELECTOR)
    )


def _title(anchor):
    # Get the title from the link attribute first, then the image alt text, then the visible text.
    title = (anchor.get_attribute("title") or "").strip()
    if title:
        return title
    images = anchor.find_elements(By.TAG_NAME, "img")
    alt = (images[0].get_attribute("alt") or "").strip() if images else ""
    return alt or (anchor.text or "").strip()


def _extract(driver):
    # Collect unique {title, url} pairs from the page.
    # Prefer links inside .mb containers. Fall back to all video links if there are none.
    anchors = []
    for container in driver.find_elements(By.CSS_SELECTOR, CONTAINER_SELECTOR):
        anchors.extend(container.find_elements(By.CSS_SELECTOR, VIDEO_SELECTOR))
    if not anchors:
        anchors = driver.find_elements(By.CSS_SELECTOR, VIDEO_SELECTOR)

    found = {}
    for anchor in anchors:
        try:
            url = anchor.get_attribute("href") or ""
            title = _title(anchor)
        except WebDriverException:
            # The element went stale while reading. Skip it.
            continue
        if VIDEO_MARKER not in url:
            continue
        # Keep the first non-empty title seen for each URL.
        if url not in found or (title and not found[url]):
            found[url] = title
    return [{"title": title, "url": url} for url, title in found.items()]


def load_listing(driver, url):
    # Load one listing page.
    # Return a list of videos, an empty list for a real empty page, or None if the page failed to render.
    driver.get(url)
    # The site may redirect a missing page number back to another page. Treat that as empty.
    if page_number(driver.current_url) != page_number(url):
        return []
    try:
        _wait_for_videos(driver)
    except TimeoutException:
        return [] if _is_empty(driver) else None
    return _extract(driver)


def fetch_page(pool, url, retries):
    # Try to load a page up to `retries` times. Return the videos, or None if every attempt failed.
    reason = "unknown"
    for attempt in range(1, retries + 1):
        if pool.closed:
            return None
        try:
            videos = load_listing(pool.acquire(), url)
            reason = "page did not render"
        except Exception as exc:
            # The driver may be broken, so throw it away and create a new one next time.
            pool.discard()
            videos = None
            reason = type(exc).__name__
        if videos is not None:
            return videos
        if attempt < retries and not pool.closed:
            log(f"Attempt {attempt}/{retries} failed ({reason}): {url}", "warn")
            time.sleep(RETRY_DELAY)
    log(f"All {retries} attempts failed: {url}", "error")
    return None


# ---------------------------------------------------------------------------
# Last page detection
# ---------------------------------------------------------------------------

def _last_page_hint(driver):
    # Look for the highest page number in the <link rel="last"> tag and in the pagination links.
    candidate = 1
    for link in driver.find_elements(By.CSS_SELECTOR, LAST_PAGE_SELECTOR):
        candidate = max(candidate, page_number(link.get_attribute("href")))
    for anchor in driver.find_elements(By.CSS_SELECTOR, UPLOADS_LINK_SELECTOR):
        candidate = max(candidate, page_number(anchor.get_attribute("href")))
    return candidate


def _walk_next_links(driver, start_url):
    # Last resort: follow rel="next" links one by one until there are none. This is slow.
    last = 1
    url = start_url
    visited = set()  # Prevents loops.
    while url and url not in visited and len(visited) < SAFETY_PAGE_LIMIT:
        visited.add(url)
        driver.get(url)
        try:
            _wait_for_videos(driver)
        except TimeoutException:
            break
        last = max(last, page_number(driver.current_url))
        next_links = driver.find_elements(By.CSS_SELECTOR, NEXT_PAGE_SELECTOR)
        url = next_links[0].get_attribute("href") if next_links else None
    return last


def _detect_once(driver, start_url):
    # Estimate the last page number from page 1.
    driver.get(start_url)
    try:
        _wait_for_videos(driver)
    except TimeoutException:
        # An empty profile has only one page.
        if _is_empty(driver):
            return 1
        raise

    candidate = _last_page_hint(driver)
    if candidate == 1:
        log("No last-page marker found, walking rel=next links (this can be slow).", "warn")
        candidate = _walk_next_links(driver, start_url)
    return min(candidate, SAFETY_PAGE_LIMIT)


def detect_last_page(pool, start_url, retries):
    # Run the detection and retry on failure. Raise if the last attempt also fails.
    for attempt in range(1, retries + 1):
        try:
            return _detect_once(pool.acquire(), start_url)
        except Exception as exc:
            pool.discard()
            if attempt == retries:
                raise
            log(f"Last-page detection failed ({attempt}/{retries}): {type(exc).__name__}", "warn")
            time.sleep(RETRY_DELAY)


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

    @property
    def videos(self):
        # All videos from all pages, with duplicates removed by URL.
        unique = {}
        for number in sorted(self.pages):
            for video in self.pages[number]:
                unique.setdefault(video["url"], video)
        return list(unique.values())

    def load(self):
        # Read the checkpoint file. Ignore it if it is broken or belongs to a different URL.
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log(f"Checkpoint unreadable ({exc}), starting fresh.", "warn")
            return
        if not isinstance(data, dict) or data.get("source_url") != self.source_url:
            log("Checkpoint belongs to another URL or is invalid, ignoring it.", "warn")
            return
        stored = data.get("pages")
        if not isinstance(stored, dict):
            return
        # Keep only the valid pages and valid videos.
        for key, videos in stored.items():
            if _is_page_key(key) and isinstance(videos, list):
                self.pages[int(key)] = [video for video in videos if _is_video(video)]
        if self.pages:
            log(f"Resuming from checkpoint: {len(self.pages)} pages, {len(self.videos)} videos", "success")

    def record(self, number, videos):
        # Store one page and save the whole checkpoint.
        self.pages[number] = videos
        self._save()

    def discard(self):
        # Delete the checkpoint file.
        self.path.unlink(missing_ok=True)

    def _save(self):
        # Write to a .tmp file first, then rename it. This prevents a half-written checkpoint.
        payload = {
            "source_url": self.source_url,
            "timestamp": datetime.now().isoformat(),
            "pages": {str(number): videos for number, videos in sorted(self.pages.items())},
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log(f"Checkpoint could not be saved ({exc}); resume may not be available.", "warn")


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _clean(value):
    # Collapse whitespace and newlines into single spaces.
    return " ".join(str(value).split())


def _write_csv(path, records, fieldnames, metadata):
    # CSV has no room for metadata without breaking the table, so only records are written.
    # utf-8-sig adds a BOM so Excel reads non-Latin characters correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


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
            # Escape pipes so they do not break the table.
            value = _clean(record[name]).replace("|", "\\|")
            cells.append(f"<{value}>" if name == "url" else value)
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
    retries: int
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
    complete: bool  # True only if every page was fetched and the end was verified.


def fieldnames_for(fields):
    # "url" gives only the URL column. "all" gives title and URL.
    return ["url"] if fields == "url" else ["title", "url"]


def _shape(videos, fields):
    # Keep only the columns the user asked for.
    keys = fieldnames_for(fields)
    return [{key: video[key] for key in keys} for video in videos]


# ---------------------------------------------------------------------------
# Scraping flow
# ---------------------------------------------------------------------------

def _record(checkpoint, page, videos):
    # Save one page to the checkpoint and print progress.
    checkpoint.record(page, videos)
    log(f"Page {page}: {len(videos)} videos", "success")


def _fetch_and_record(pool, job, checkpoint, page):
    # Fetch one page in the current thread. Return True if it worked.
    videos = fetch_page(pool, page_url(job.url, page), job.retries)
    if videos is None:
        return False
    _record(checkpoint, page, videos)
    return True


def _fetch_pages(pool, job, checkpoint, pages):
    # Fetch many pages in parallel. Return the list of pages that still failed afterward.
    if not pages:
        return []

    log(f"Fetching {len(pages)} pages with {job.workers} workers...", "step")
    failed = []
    started = {}
    executor = ThreadPoolExecutor(max_workers=job.workers)
    try:
        futures = {}
        for page in pages:
            started[page] = time.monotonic()
            future = executor.submit(fetch_page, pool, page_url(job.url, page), job.retries)
            futures[future] = page

        # Handle each page as soon as it finishes, not in submission order.
        for future in as_completed(futures):
            page = futures[future]
            try:
                videos = future.result()
            except Exception as exc:
                log(f"Page {page} raised {type(exc).__name__}: {exc}", "error")
                videos = None
            if videos is None:
                failed.append(page)
            else:
                _record(checkpoint, page, videos)
            if job.verbose:
                log(f"Page {page} finished in {time.monotonic() - started[page]:.1f}s", "dim")
    finally:
        # Do not wait for unfinished tasks if the user pressed Ctrl+C.
        executor.shutdown(wait=False, cancel_futures=True)

    if not failed:
        return []
    failed.sort()
    log(f"Retrying {len(failed)} failed pages: {failed}", "warn")
    # Retry each failed page once, one at a time.
    return [page for page in failed if not _fetch_and_record(pool, job, checkpoint, page)]


def _verify_end(pool, job, checkpoint, page):
    # Probe pages after the estimated last page until one is empty or repeats the previous page.
    previous = None
    while page <= SAFETY_PAGE_LIMIT:
        url = page_url(job.url, page)
        videos = fetch_page(pool, url, job.retries)
        if videos == []:
            # Check an empty page once more in case the site was just slow to render.
            videos = fetch_page(pool, url, job.retries)
        if videos is None:
            log(f"Verification stopped, page {page} failed.", "error")
            return False
        if not videos:
            log(f"Page {page} is empty, last page is {page - 1}.", "success")
            return True
        # Some sites show the last page again for out-of-range numbers. Stop when that happens.
        urls = {video["url"] for video in videos}
        if urls == previous:
            log(f"Page {page} repeats the previous page, last page is {page - 1}.", "success")
            return True
        previous = urls
        _record(checkpoint, page, videos)
        page += 1
    log("Reached the safety page limit.", "warn")
    return False


def run(job, pool):
    # Main flow: load checkpoint, find last page, fetch, verify, save, and clean up.
    started = time.monotonic()
    checkpoint = Checkpoint(job.checkpoint_path, job.url)
    if job.fresh:
        checkpoint.discard()
    else:
        checkpoint.load()

    # Step 1: estimate how many pages exist.
    log("Detecting last page...", "step")
    candidate = detect_last_page(pool, page_url(job.url, 1), job.retries)
    log(f"Estimated last page: {candidate}", "info")

    # Step 2: page 1 is required. Stop if it fails.
    if 1 not in checkpoint.pages and not _fetch_and_record(pool, job, checkpoint, 1):
        raise RuntimeError("page 1 failed after all retries, checkpoint kept")

    # Step 3: fetch the remaining pages that are not already in the checkpoint.
    last_known = max([candidate, *checkpoint.pages])
    pending = [page for page in range(2, last_known + 1) if page not in checkpoint.pages]
    missing = _fetch_pages(pool, job, checkpoint, pending)

    # Step 4: check for pages beyond the estimate.
    log("Verifying end of pagination...", "step")
    verified = _verify_end(pool, job, checkpoint, last_known + 1)
    complete = verified and not missing
    if missing:
        log(f"These pages still failed and are missing from the results: {missing}", "error")

    # Step 5: shape the data and save it if an output path was given.
    fieldnames = fieldnames_for(job.fields)
    records = _shape(checkpoint.videos, job.fields)
    saved = False

    if job.output is None:
        log("No output path given, results were not saved.", "warn")
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

    # Step 6: decide whether to delete the checkpoint.
    if not records:
        log("No videos were extracted. Check the URL, network, or site structure.", "warn")
    if not complete:
        log("Results are incomplete. Checkpoint kept, run again to continue.", "warn")
    elif saved and not job.keep_checkpoint:
        checkpoint.discard()
    elif not saved:
        log("Checkpoint kept so you can save later.", "info")

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

    return parse


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="userporner.py",
        description="Scrape video listings from an Eporner profile.",
        epilog=(
            "examples:\n"
            "  python userporner.py -u https://www.eporner.com/profile/NAME/ -o hasil -f md\n"
            "  python userporner.py -u https://www.eporner.com/profile/NAME/ -o hasil -f pdf -F url -k\n"
            "  python userporner.py -u https://www.eporner.com/profile/NAME/\n"
            "  python userporner.py -i"
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
    cli.add_argument("-u", "--url", help="Eporner profile URL")
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
        help=f"parallel workers, 1-{MAX_WORKERS} (default: {DEFAULT_WORKERS})",
    )
    cli.add_argument(
        "-r", "--retries", type=_bounded(1, MAX_RETRIES),
        help=f"retries per page, 1-{MAX_RETRIES} (default: {DEFAULT_RETRIES})",
    )
    cli.add_argument(
        "-c", "--checkpoint",
        help=f"checkpoint file path (default: {DEFAULT_CHECKPOINT})",
    )
    cli.add_argument(
        "-n", "--no-resume", action="store_true",
        help="ignore and delete any existing checkpoint, start from scratch",
    )
    cli.add_argument(
        "-k", "--keep-checkpoint", action="store_true",
        help="keep the checkpoint file after a complete run",
    )
    cli.add_argument(
        "-v", "--verbose", action="store_true",
        help="show the time taken for each page",
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
        parser.error("-u/--url must be an eporner.com/profile/<username> URL")

    # Fill in defaults for anything the user did not set.
    args.format = args.format or DEFAULT_FORMAT
    args.fields = args.fields or DEFAULT_FIELDS
    args.workers = args.workers or DEFAULT_WORKERS
    args.retries = args.retries or DEFAULT_RETRIES
    args.checkpoint = args.checkpoint or DEFAULT_CHECKPOINT
    return args


def make_job(url, fmt, fields, output, workers, retries, checkpoint, keep_checkpoint, fresh, verbose):
    # Build a Job from plain values. Used by both CLI and interactive mode.
    return Job(
        url=url,
        output=resolve_output(output, fmt) if output else None,
        fmt=fmt,
        fields=fields,
        workers=workers,
        retries=retries,
        checkpoint_path=Path(checkpoint),
        keep_checkpoint=keep_checkpoint,
        fresh=fresh,
        verbose=verbose,
    )


def job_from_args(args):
    # Convert parsed CLI arguments into a Job.
    return make_job(
        url=normalize_profile_url(args.url),
        fmt=args.format,
        fields=args.fields,
        output=args.output,
        workers=args.workers,
        retries=args.retries,
        checkpoint=args.checkpoint,
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
    # Ask until the URL is a valid Eporner profile URL.
    while True:
        url = normalize_profile_url(_ask("Profile URL (eporner.com/profile/<username>)"))
        if url is not None:
            return url
        print(f"{Fore.YELLOW}That is not a valid Eporner profile URL.{Style.RESET_ALL}")


def interactive_job():
    # Ask for settings in order. Format and fields are only asked if there is an output file.
    print_banner("Interactive mode")
    url = _ask_url()
    output = _ask("Output file (empty = do not save)")
    fmt, fields = DEFAULT_FORMAT, DEFAULT_FIELDS
    if output:
        fmt = _ask_choice("Format", FORMATS, DEFAULT_FORMAT)
        fields = _ask_choice("Fields", FIELD_CHOICES, DEFAULT_FIELDS)
    workers = _ask_int("Workers", DEFAULT_WORKERS, 1, MAX_WORKERS)
    retries = _ask_int("Retries", DEFAULT_RETRIES, 1, MAX_RETRIES)
    return make_job(
        url=url,
        fmt=fmt,
        fields=fields,
        output=output or None,
        workers=workers,
        retries=retries,
        checkpoint=DEFAULT_CHECKPOINT,
        keep_checkpoint=False,
        fresh=False,
        verbose=False,
    )


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_banner(subtitle=""):
    # Print the title box at the top of the screen.
    width = 60
    line = f"{Fore.BLUE}{'=' * width}{Style.RESET_ALL}"
    print(line)
    print(f"{Fore.CYAN}{Style.BRIGHT}{'EPORNER SCRAPER':^{width}}{Style.RESET_ALL}")
    if subtitle:
        print(f"{Fore.WHITE}{subtitle:^{width}}{Style.RESET_ALL}")
    print(line)


def print_summary(outcome, job):
    # Print the final summary and the first five records.
    width = 60
    rule = f"{Fore.BLUE}{'-' * width}{Style.RESET_ALL}"
    status = (
        f"{Fore.GREEN}{Style.BRIGHT}COMPLETE{Style.RESET_ALL}"
        if outcome.complete
        else f"{Fore.RED}{Style.BRIGHT}INCOMPLETE{Style.RESET_ALL}"
    )
    saved = str(outcome.output) if outcome.output else f"{Fore.YELLOW}not saved (use -o){Style.RESET_ALL}"
    print()
    print_banner("Summary")
    print(f"{Fore.CYAN}Source  {Style.RESET_ALL}: {job.url}")
    print(f"{Fore.CYAN}Records {Style.RESET_ALL}: {Fore.GREEN}{Style.BRIGHT}{len(outcome.records)}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}Output  {Style.RESET_ALL}: {saved}")
    print(f"{Fore.CYAN}Format  {Style.RESET_ALL}: {outcome.fmt}")
    print(f"{Fore.CYAN}Fields  {Style.RESET_ALL}: {', '.join(outcome.fieldnames)}")
    print(f"{Fore.CYAN}Elapsed {Style.RESET_ALL}: {outcome.elapsed:.1f}s")
    print(f"{Fore.CYAN}Status  {Style.RESET_ALL}: {status}")
    print(rule)
    for record in outcome.records[:5]:
        label = record.get("title") or record["url"]
        print(f"{Fore.GREEN}  - {label[:60]}{Style.RESET_ALL}")
    if len(outcome.records) > 5:
        print(f"{Fore.WHITE}  ... and {len(outcome.records) - 5} more{Style.RESET_ALL}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    # Set up terminal colors and UTF-8 output, then run the scraper.
    # Return the exit code.
    init(autoreset=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args(argv)
    pool = DriverPool()
    try:
        job = interactive_job() if args.interactive else job_from_args(args)
        if job.output is not None:
            ensure_output_ready(job.output, job.fmt)
        outcome = run(job, pool)
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
        # Always close Chrome, even when there was an error.
        pool.close()

    print_summary(outcome, job)
    return 0 if outcome.complete else 1


if __name__ == "__main__":
    sys.exit(main())
