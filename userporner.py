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

DEFAULT_CHECKPOINT = ".scraper_checkpoint.json"
FORMATS = ("csv", "json", "md", "pdf")
FIELD_CHOICES = ("all", "url")
PROFILE_PATTERN = re.compile(r"https?://(?:www\.)?eporner\.com/profile/([^/?#]+)", re.IGNORECASE)
PAGE_PATTERN = re.compile(r"/uploaded-videos/(\d+)(?:[/?#]|$)")
VIDEO_MARKER = "/video-"
VIDEO_SELECTOR = f'a[href*="{VIDEO_MARKER}"]'
CONTAINER_SELECTOR = ".mb"
UPLOADS_LINK_SELECTOR = 'a[href*="/uploaded-videos/"]'
LAST_PAGE_SELECTOR = 'link[rel="last"]'
NEXT_PAGE_SELECTOR = 'link[rel="next"]'
PAGE_LOAD_TIMEOUT = 30
RENDER_TIMEOUT = 15
RETRY_DELAY = 2
SAFETY_PAGE_LIMIT = 100_000
MAX_WORKERS = 10
MAX_RETRIES = 10
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
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

_LOG_LOCK = threading.Lock()
_LOG_COLORS = {
    "info": Fore.CYAN,
    "success": Fore.GREEN,
    "warn": Fore.YELLOW,
    "error": Fore.RED,
    "step": Fore.MAGENTA,
    "dim": Fore.WHITE,
}


def log(message, level="info"):
    stamp = datetime.now().strftime("%H:%M:%S")
    color = _LOG_COLORS.get(level, Fore.WHITE)
    with _LOG_LOCK:
        print(f"{color}[{stamp}] {message}{Style.RESET_ALL}", flush=True)


def _build_options():
    options = Options()
    for argument in CHROME_ARGUMENTS:
        options.add_argument(argument)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    return options


def _quit_quietly(driver):
    with suppress(Exception):
        driver.quit()


class DriverPool:
    def __init__(self):
        self._local = threading.local()
        self._drivers = []
        self._lock = threading.Lock()
        self._closed = False

    def acquire(self):
        if self._closed:
            raise RuntimeError("driver pool is closed")
        driver = getattr(self._local, "driver", None)
        if driver is None:
            driver = self._create()
            self._local.driver = driver
        return driver

    def discard(self):
        driver = getattr(self._local, "driver", None)
        self._local.driver = None
        if driver is not None:
            _quit_quietly(driver)
            with self._lock:
                if driver in self._drivers:
                    self._drivers.remove(driver)

    def close(self):
        self._closed = True
        with self._lock:
            drivers, self._drivers = self._drivers, []
        for driver in drivers:
            _quit_quietly(driver)

    def _create(self):
        driver = webdriver.Chrome(options=_build_options())
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        with self._lock:
            self._drivers.append(driver)
        return driver


def normalize_profile_url(raw):
    match = PROFILE_PATTERN.search(raw or "")
    if match is None:
        return None
    return f"https://www.eporner.com/profile/{match.group(1)}/uploaded-videos/"


def page_url(base, number):
    return base if number == 1 else f"{base}{number}/"


def page_number(url):
    match = PAGE_PATTERN.search(url or "")
    return int(match.group(1)) if match else 1


def _wait_for_videos(driver):
    WebDriverWait(driver, RENDER_TIMEOUT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, VIDEO_SELECTOR))
    )


def _is_empty(driver):
    return not (
        driver.find_elements(By.CSS_SELECTOR, CONTAINER_SELECTOR)
        or driver.find_elements(By.CSS_SELECTOR, VIDEO_SELECTOR)
    )


def _title(anchor):
    title = (anchor.get_attribute("title") or "").strip()
    if title:
        return title
    images = anchor.find_elements(By.TAG_NAME, "img")
    alt = (images[0].get_attribute("alt") or "").strip() if images else ""
    return alt or (anchor.text or "").strip()


def _extract(driver):
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
            continue
        if VIDEO_MARKER not in url:
            continue
        if url not in found or (title and not found[url]):
            found[url] = title
    return [{"title": title, "url": url} for url, title in found.items()]


def load_listing(driver, url):
    driver.get(url)
    if page_number(driver.current_url) != page_number(url):
        return []
    try:
        _wait_for_videos(driver)
    except TimeoutException:
        return [] if _is_empty(driver) else None
    return _extract(driver)


def fetch_page(pool, url, retries):
    reason = "unknown"
    for attempt in range(1, retries + 1):
        try:
            videos = load_listing(pool.acquire(), url)
            reason = "page did not render"
        except Exception as exc:
            pool.discard()
            videos = None
            reason = type(exc).__name__
        if videos is not None:
            return videos
        if attempt < retries:
            log(f"Attempt {attempt}/{retries} failed ({reason}): {url}", "warn")
            time.sleep(RETRY_DELAY)
    log(f"All {retries} attempts failed: {url}", "error")
    return None


def _last_page_hint(driver):
    candidate = 1
    for link in driver.find_elements(By.CSS_SELECTOR, LAST_PAGE_SELECTOR):
        candidate = max(candidate, page_number(link.get_attribute("href")))
    for anchor in driver.find_elements(By.CSS_SELECTOR, UPLOADS_LINK_SELECTOR):
        candidate = max(candidate, page_number(anchor.get_attribute("href")))
    return candidate


def _walk_next_links(driver, start_url):
    last = 1
    url = start_url
    visited = set()
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
    driver.get(start_url)
    try:
        _wait_for_videos(driver)
    except TimeoutException:
        if _is_empty(driver):
            return 1
        raise

    candidate = _last_page_hint(driver)
    if candidate == 1:
        log("No last-page marker found, walking rel=next links (this can be slow).", "warn")
        candidate = _walk_next_links(driver, start_url)
    return min(candidate, SAFETY_PAGE_LIMIT)


def detect_last_page(pool, start_url, retries):
    for attempt in range(1, retries + 1):
        try:
            return _detect_once(pool.acquire(), start_url)
        except Exception as exc:
            pool.discard()
            if attempt == retries:
                raise
            log(f"Last-page detection failed ({attempt}/{retries}): {type(exc).__name__}", "warn")
            time.sleep(RETRY_DELAY)


def _is_video(item):
    return (
        isinstance(item, dict)
        and isinstance(item.get("url"), str)
        and bool(item["url"])
        and isinstance(item.get("title"), str)
    )


def _is_page_key(key):
    return isinstance(key, str) and key.isascii() and key.isdigit() and 1 <= int(key) <= SAFETY_PAGE_LIMIT


class Checkpoint:
    def __init__(self, path, source_url):
        self.path = Path(path)
        self.source_url = source_url
        self.pages = {}

    @property
    def videos(self):
        unique = {}
        for number in sorted(self.pages):
            for video in self.pages[number]:
                unique.setdefault(video["url"], video)
        return list(unique.values())

    def load(self):
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
        for key, videos in stored.items():
            if _is_page_key(key) and isinstance(videos, list):
                self.pages[int(key)] = [video for video in videos if _is_video(video)]
        if self.pages:
            log(f"Resuming from checkpoint: {len(self.pages)} pages, {len(self.videos)} videos")

    def record(self, number, videos):
        self.pages[number] = videos
        self._save()

    def discard(self):
        self.path.unlink(missing_ok=True)

    def _save(self):
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


def _clean(value):
    return " ".join(str(value).split())


def _write_csv(path, records, fieldnames, metadata):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def _write_json(path, records, fieldnames, metadata):
    payload = {**metadata, "records": records}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_md(path, records, fieldnames, metadata):
    headers = ["No", *(name.capitalize() for name in fieldnames)]
    lines = [
        f"# Eporner videos",
        "",
        f"- Source: {metadata['source_url']}",
        f"- Total videos: {metadata['total_videos']}",
        f"- Scraped at: {metadata['scraped_at']}",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for index, record in enumerate(records, start=1):
        cells = [str(index), *(_clean(record[name]).replace("|", "\\|") for name in fieldnames)]
        lines.append("| " + " | ".join(cells) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_pdf(path, records, fieldnames, metadata):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    margin = 30
    page_size = landscape(A4)
    usable = page_size[0] - 2 * margin
    number_width = 40
    shares = [0.45, 0.55] if len(fieldnames) == 2 else [1.0]
    widths = [number_width] + [(usable - number_width) * share for share in shares]

    base = getSampleStyleSheet()["BodyText"]
    cell = ParagraphStyle("cell", parent=base, fontSize=7, leading=9)
    head = ParagraphStyle("head", parent=cell, fontName="Helvetica-Bold")

    headers = ["No", *(name.capitalize() for name in fieldnames)]
    rows = [[Paragraph(escape(header), head) for header in headers]]
    for index, record in enumerate(records, start=1):
        row = [Paragraph(str(index), cell)]
        row.extend(Paragraph(escape(_clean(record[name])), cell) for name in fieldnames)
        rows.append(row)

    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDDDDD")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    title = Paragraph(escape(f"Eporner videos: {metadata['source_url']}"), getSampleStyleSheet()["Heading2"])
    summary = Paragraph(
        escape(f"Total videos: {metadata['total_videos']} | Scraped at: {metadata['scraped_at']}"),
        base,
    )
    document = SimpleDocTemplate(
        str(path),
        pagesize=page_size,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title="Eporner videos",
    )
    document.build([title, summary, Spacer(1, 10), table])


WRITERS = {"csv": _write_csv, "json": _write_json, "md": _write_md, "pdf": _write_pdf}


def save_records(path, fmt, records, fieldnames, metadata):
    tmp = path.with_name(path.name + ".tmp")
    WRITERS[fmt](tmp, records, fieldnames, metadata)
    tmp.replace(path)


def resolve_output(raw, fmt):
    path = Path(raw)
    if path.suffix.lower() == f".{fmt}":
        return path
    return path.with_name(path.name + f".{fmt}")


def ensure_output_ready(path, fmt):
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


@dataclass(frozen=True)
class Job:
    url: str
    output: Path | None
    fmt: str
    fields: str
    workers: int
    retries: int
    checkpoint_path: Path
    keep_checkpoint: bool
    fresh: bool
    verbose: bool


@dataclass(frozen=True)
class Outcome:
    records: list
    output: Path | None
    fmt: str
    fieldnames: list
    elapsed: float
    complete: bool


def fieldnames_for(fields):
    return ["url"] if fields == "url" else ["title", "url"]


def _shape(videos, fields):
    keys = fieldnames_for(fields)
    return [{key: video[key] for key in keys} for video in videos]


def _record(checkpoint, page, videos):
    checkpoint.record(page, videos)
    log(f"Page {page}: {len(videos)} videos", "success")


def _fetch_and_record(pool, job, checkpoint, page):
    videos = fetch_page(pool, page_url(job.url, page), job.retries)
    if videos is None:
        return False
    _record(checkpoint, page, videos)
    return True


def _fetch_pages(pool, job, checkpoint, pages):
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

        for future in as_completed(futures):
            page = futures[future]
            videos = future.result()
            if videos is None:
                failed.append(page)
            else:
                _record(checkpoint, page, videos)
            if job.verbose:
                log(f"Page {page} finished in {time.monotonic() - started[page]:.1f}s", "dim")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if not failed:
        return []
    failed.sort()
    log(f"Retrying {len(failed)} failed pages: {failed}", "warn")
    return [page for page in failed if not _fetch_and_record(pool, job, checkpoint, page)]


def _verify_end(pool, job, checkpoint, page):
    while page <= SAFETY_PAGE_LIMIT:
        url = page_url(job.url, page)
        videos = fetch_page(pool, url, job.retries)
        if videos == []:
            videos = fetch_page(pool, url, job.retries)
        if videos is None:
            log(f"Verification stopped, page {page} failed.", "error")
            return False
        if not videos:
            log(f"Page {page} is empty, last page is {page - 1}.", "success")
            return True
        _record(checkpoint, page, videos)
        page += 1
    log("Reached the safety page limit.", "warn")
    return False


def run(job, pool):
    started = time.monotonic()
    checkpoint = Checkpoint(job.checkpoint_path, job.url)
    if job.fresh:
        checkpoint.discard()
    else:
        checkpoint.load()

    log("Detecting last page...", "step")
    candidate = detect_last_page(pool, page_url(job.url, 1), job.retries)
    log(f"Estimated last page: {candidate}")

    if 1 not in checkpoint.pages and not _fetch_and_record(pool, job, checkpoint, 1):
        raise RuntimeError("page 1 failed after all retries, checkpoint kept")

    last_known = max([candidate, *checkpoint.pages])
    pending = [page for page in range(2, last_known + 1) if page not in checkpoint.pages]
    missing = _fetch_pages(pool, job, checkpoint, pending)

    log("Verifying end of pagination...", "step")
    verified = _verify_end(pool, job, checkpoint, last_known + 1)
    complete = verified and not missing
    if missing:
        log(f"These pages still failed and are missing from the results: {missing}", "error")

    fieldnames = fieldnames_for(job.fields)
    records = _shape(checkpoint.videos, job.fields)

    if job.output is None:
        log("No output path given (-o), results are not saved.", "warn")
    else:
        metadata = {
            "source_url": job.url,
            "total_videos": len(records),
            "scraped_at": datetime.now().isoformat(),
            "fields": fieldnames,
        }
        log(f"Saving {len(records)} records to {job.output} ({job.fmt})...", "step")
        save_records(job.output, job.fmt, records, fieldnames, metadata)
        log(f"Saved to {job.output}", "success")

    if not records:
        log("WARNING: no videos were extracted. Check the URL, network, or site structure.", "warn")
    if not complete:
        log("Results are incomplete. Checkpoint kept, run again to continue.", "warn")
    elif not job.keep_checkpoint:
        checkpoint.discard()

    return Outcome(
        records=records,
        output=job.output,
        fmt=job.fmt,
        fieldnames=fieldnames,
        elapsed=time.monotonic() - started,
        complete=complete,
    )


def _bounded(low, high):
    def parse(text):
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value

    return parse


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Scrape video listings from an Eporner profile.")
    parser.add_argument("-i", "--interactive", action="store_true", help="run in interactive mode")
    parser.add_argument("-u", "--url", help="Eporner profile URL")
    parser.add_argument("-o", "--output", help="output file path (nothing is saved if omitted)")
    parser.add_argument("-f", "--format", choices=FORMATS, default="csv", help="output format")
    parser.add_argument(
        "-F", "--fields", choices=FIELD_CHOICES, default="all", help="all = title and url, url = url only"
    )
    parser.add_argument("-w", "--workers", type=_bounded(1, MAX_WORKERS), default=3, help="parallel workers")
    parser.add_argument("-r", "--retries", type=_bounded(1, MAX_RETRIES), default=3, help="retries per page")
    parser.add_argument("-c", "--checkpoint", default=DEFAULT_CHECKPOINT, help="checkpoint file path")
    parser.add_argument("-n", "--no-resume", action="store_true", help="ignore existing checkpoint")
    parser.add_argument("-k", "--keep-checkpoint", action="store_true", help="keep checkpoint after success")
    parser.add_argument("-v", "--verbose", action="store_true", help="show time per page")
    args = parser.parse_args(argv)

    if not args.interactive:
        if not args.url:
            parser.error("-u/--url is required unless -i is used")
        if normalize_profile_url(args.url) is None:
            parser.error("-u/--url must be an eporner.com/profile/<username> URL")
    return args


def make_job(
    url, fmt, fields, output, workers, retries, checkpoint, keep_checkpoint, fresh, verbose
):
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


def _ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def _ask_choice(prompt, choices, default):
    options = "/".join(choices)
    while True:
        answer = _ask(f"{prompt} ({options})", default).lower()
        if answer in choices:
            return answer
        print(f"Please choose one of: {options}")


def _ask_int(prompt, default, low, high):
    while True:
        answer = _ask(f"{prompt} ({low}-{high})", str(default))
        try:
            value = int(answer)
        except ValueError:
            print("Please enter a number.")
            continue
        if low <= value <= high:
            return value
        print(f"Please enter a number between {low} and {high}.")


def _ask_url():
    while True:
        url = normalize_profile_url(_ask("Profile URL (eporner.com/profile/<username>)"))
        if url is not None:
            return url
        print("That is not a valid Eporner profile URL.")


def interactive_job():
    print(f"{Fore.CYAN}Eporner scraper, interactive mode{Style.RESET_ALL}")
    url = _ask_url()
    fmt = _ask_choice("Format", FORMATS, "csv")
    fields = _ask_choice("Fields", FIELD_CHOICES, "all")
    output = _ask("Output file (empty = do not save)")
    workers = _ask_int("Workers", 3, 1, MAX_WORKERS)
    retries = _ask_int("Retries", 3, 1, MAX_RETRIES)
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


def print_summary(outcome, job):
    rule = "=" * 60
    status = "complete" if outcome.complete else "INCOMPLETE"
    saved = str(outcome.output) if outcome.output else "not saved (use -o to save)"
    print()
    print(rule)
    print(f"{Fore.CYAN}SUMMARY{Style.RESET_ALL}")
    print(rule)
    print(f"Source  : {job.url}")
    print(f"Records : {len(outcome.records)}")
    print(f"Output  : {saved}")
    print(f"Format  : {outcome.fmt}")
    print(f"Fields  : {', '.join(outcome.fieldnames)}")
    print(f"Elapsed : {outcome.elapsed:.1f}s")
    print(f"Status  : {status}")
    print(rule)
    for record in outcome.records[:5]:
        label = record.get("title") or record["url"]
        print(f"{Fore.GREEN}- {label[:60]}{Style.RESET_ALL}")
    if len(outcome.records) > 5:
        print(f"... and {len(outcome.records) - 5} more")


def main(argv=None):
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
        log("Stopped by user. Run again to resume.", "warn")
        return 130
    except ValueError as exc:
        log(f"Validation error: {exc}", "error")
        return 2
    except Exception as exc:
        log(f"Fatal error: {type(exc).__name__}: {exc}", "error")
        return 1
    finally:
        pool.close()

    print_summary(outcome, job)
    return 0 if outcome.complete else 1


if __name__ == "__main__":
    sys.exit(main())
