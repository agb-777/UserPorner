import argparse
import csv
import json
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from colorama import init, Fore, Style
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

init(autoreset=True)

_print_lock = threading.Lock()
_tls = threading.local()
_drivers = []
_drivers_lock = threading.Lock()
_VERBOSE = 0
_checkpoint_broken = False

LOG_COLORS = {
    "info": Fore.CYAN,
    "success": Fore.GREEN,
    "warn": Fore.YELLOW,
    "error": Fore.RED,
    "step": Fore.MAGENTA,
    "dim": Fore.WHITE,
}

DEFAULT_CHECKPOINT = ".scraper_checkpoint.json"
PROFILE_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?eporner\.com/profile/([^/?#]+)", re.IGNORECASE
)
PAGE_NUM_PATTERN = re.compile(r"/uploaded-videos/(\d+)(?:[/?#]|$)")
SAFETY_PAGE_LIMIT = 100000
TICKER_INTERVAL = 5


def log(msg, level="info"):
    color = LOG_COLORS.get(level, Fore.WHITE)
    with _print_lock:
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            print(f"{color}[{ts}] {msg}{Style.RESET_ALL}", flush=True)
        except Exception:
            pass


def vlog(msg, min_level=1, level="info"):
    if _VERBOSE >= min_level:
        log(msg, level)


class ProgressTicker:
    def __init__(self, total, label, interval=TICKER_INTERVAL):
        self.total = total
        self.label = label
        self.interval = interval
        self.done = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def __enter__(self):
        if self.total > 0:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                with self._lock:
                    done = self.done
                remaining = self.total - done
                if remaining > 0:
                    log(f"{self.label}: {done}/{self.total} done, {remaining} remaining")
            except Exception:
                pass

    def update(self):
        with self._lock:
            self.done += 1


def build_options():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return opts


def new_driver():
    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=build_options(),
    )


def worker_driver():
    if not hasattr(_tls, "driver"):
        d = new_driver()
        with _drivers_lock:
            _drivers.append(d)
        _tls.driver = d
    return _tls.driver


def drop_worker_driver():
    if hasattr(_tls, "driver"):
        d = _tls.driver
        del _tls.driver
        try:
            d.quit()
        except Exception as exc:
            vlog(f"Driver quit failed: {exc}", min_level=1, level="dim")
        with _drivers_lock:
            try:
                _drivers.remove(d)
            except ValueError:
                pass


def close_all_drivers():
    with _drivers_lock:
        drivers = list(_drivers)
        _drivers.clear()
    for d in drivers:
        try:
            d.quit()
        except Exception as exc:
            vlog(f"Driver quit failed during cleanup: {exc}", min_level=1, level="dim")


def _title_of(anchor):
    try:
        t = (anchor.get_attribute("title") or "").strip()
        if t:
            return t
    except Exception:
        pass
    try:
        img = anchor.find_element(By.TAG_NAME, "img")
        alt = (img.get_attribute("alt") or "").strip()
        if alt:
            return alt
    except Exception:
        pass
    try:
        return (anchor.text or "").strip()
    except Exception:
        return ""


def extract_videos(driver, page_label=None):
    by_url = {}
    fail_count = 0

    try:
        containers = driver.find_elements(By.CSS_SELECTOR, ".mb")
    except Exception as exc:
        log(f"Could not query .mb containers: {exc}", "warn")
        containers = []

    scoped = []
    if containers:
        for c in containers:
            try:
                scoped.extend(c.find_elements(By.CSS_SELECTOR, 'a[href*="/video-"]'))
            except Exception:
                fail_count += 1
                continue

    if scoped:
        anchors = scoped
    else:
        try:
            anchors = driver.find_elements(By.CSS_SELECTOR, 'a[href*="/video-"]')
        except Exception as exc:
            log(f"Could not query video anchors: {exc}", "warn")
            return []
        if anchors:
            log("Scoped extraction empty; using global anchor scan.", "warn")

    for a in anchors:
        try:
            url = a.get_attribute("href")
            if not url or "/video-" not in url:
                continue
            if url in by_url and by_url[url]:
                continue
            title = _title_of(a)
            if title:
                by_url[url] = title
                if page_label is not None:
                    vlog(f"  [{page_label}] {title[:70]}", min_level=2, level="dim")
            else:
                by_url.setdefault(url, "")
        except Exception:
            fail_count += 1
            continue

    if fail_count > 0:
        vlog(
            f"  [{page_label or '?'}] {fail_count} element(s) failed extraction",
            min_level=1,
            level="warn",
        )

    return [{"title": t, "url": u} for u, t in by_url.items()]


def _page_is_empty(driver):
    try:
        if driver.find_elements(By.CSS_SELECTOR, ".mb"):
            return False
        if driver.find_elements(By.CSS_SELECTOR, 'a[href*="/video-"]'):
            return False
        return True
    except Exception:
        return False


def fetch_page(url, retries, page_label=None):
    for attempt in range(1, retries + 1):
        try:
            d = worker_driver()
            d.set_page_load_timeout(30)
            d.get(url)
            try:
                WebDriverWait(d, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'a[href*="/video-"]')
                    )
                )
            except TimeoutException:
                if _page_is_empty(d):
                    log(f"Page is empty (no containers, no anchors): {url}")
                    return []
                log(f"Page did not render anchors within 15s: {url}", "warn")
                return None
            return extract_videos(d, page_label)
        except Exception as exc:
            drop_worker_driver()
            if attempt < retries:
                log(
                    f"Attempt {attempt}/{retries} failed for {url} "
                    f"({type(exc).__name__}). Retrying.",
                    "warn",
                )
                time.sleep(2)
            else:
                log(f"All {retries} attempts failed for {url}: {exc}", "error")
                return None
    return None


def page_url(base_url, n):
    base = base_url.rstrip("/")
    return base + "/" if n == 1 else f"{base}/{n}/"


def _max_visible_page(driver):
    last = 1
    try:
        anchors = driver.find_elements(By.CSS_SELECTOR, 'a[href*="/uploaded-videos/"]')
    except Exception:
        return last
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        m = PAGE_NUM_PATTERN.search(href)
        if m:
            last = max(last, int(m.group(1)))
    return last


def _walk_next_links(driver, start_url):
    last = 1
    next_url = start_url
    seen = set()
    while next_url and len(seen) < SAFETY_PAGE_LIMIT:
        if next_url in seen:
            break
        seen.add(next_url)
        m = PAGE_NUM_PATTERN.search(next_url)
        if not m:
            break
        current_page = int(m.group(1))
        try:
            driver.get(next_url)
        except Exception:
            break
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'a[href*="/video-"]')
                )
            )
        except TimeoutException:
            break
        last = max(last, current_page)
        try:
            nxt = driver.find_element(By.CSS_SELECTOR, 'link[rel="next"]')
            href = nxt.get_attribute("href") or ""
        except Exception:
            break
        if not href:
            break
        next_url = href
    return last


def detect_candidate_last_page(start_url, retries):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            d = new_driver()
        except Exception as exc:
            last_exc = exc
            log(
                f"Driver creation failed (attempt {attempt}/{retries}): "
                f"{type(exc).__name__}: {exc}",
                "warn",
            )
            if attempt < retries:
                time.sleep(3)
                continue
            raise RuntimeError(f"Could not create driver: {last_exc}") from last_exc

        try:
            d.set_page_load_timeout(30)
            d.get(start_url)
            try:
                WebDriverWait(d, 20).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'a[href*="/video-"]')
                    )
                )
            except TimeoutException:
                if _page_is_empty(d):
                    log("Page 1 is empty.", "warn")
                    return 1
                last_exc = TimeoutException("Page 1 anchors did not appear in 20s")
                if attempt < retries:
                    log(f"Page 1 attempt {attempt} timed out. Retrying.", "warn")
                    time.sleep(2)
                    continue
                raise last_exc

            candidate = 1

            try:
                link = d.find_element(By.CSS_SELECTOR, 'link[rel="last"]')
                href = link.get_attribute("href") or ""
                m = PAGE_NUM_PATTERN.search(href)
                if m:
                    candidate = max(candidate, int(m.group(1)))
            except NoSuchElementException:
                pass

            candidate = max(candidate, _max_visible_page(d))

            if candidate == 1:
                log(
                    "No last-page marker or visible pagination; "
                    "walking rel=next sequentially (this may be slow).",
                    "warn",
                )
                candidate = _walk_next_links(d, start_url)
                log(f"Sequential walk found last page: {candidate}")

            candidate = min(candidate, SAFETY_PAGE_LIMIT)
            vlog(f"Detection candidate last page = {candidate}", min_level=1)
            return candidate
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                log(f"detect_candidate_last_page attempt {attempt} failed: {exc}", "warn")
                time.sleep(2)
            continue
        finally:
            try:
                d.quit()
            except Exception as exc:
                vlog(f"Driver quit failed in detect: {exc}", min_level=1, level="dim")
    raise RuntimeError(f"detect_candidate_last_page failed: {last_exc}")


def load_checkpoint(path, source_url):
    p = Path(path)
    if not p.exists():
        return [], set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"Checkpoint unreadable ({exc}); starting fresh.", "warn")
        return [], set()
    if not isinstance(data, dict):
        log("Checkpoint has invalid top-level structure; starting fresh.", "warn")
        return [], set()
    if data.get("source_url") != source_url:
        log("Checkpoint belongs to a different source URL; ignoring.", "warn")
        return [], set()

    raw_videos = data.get("videos", [])
    raw_pages = data.get("pages_done", [])

    if not isinstance(raw_videos, list) or not isinstance(raw_pages, list):
        log("Checkpoint has invalid data types; starting fresh.", "warn")
        return [], set()

    valid_videos = []
    dropped_v = 0
    for v in raw_videos:
        if (
            isinstance(v, dict)
            and isinstance(v.get("url"), str)
            and isinstance(v.get("title"), str)
            and v["url"]
        ):
            valid_videos.append({"title": v["title"], "url": v["url"]})
        else:
            dropped_v += 1
    if dropped_v:
        log(f"Checkpoint: dropped {dropped_v} malformed video entries.", "warn")

    valid_pages = set()
    dropped_p = 0
    for p_ in raw_pages:
        try:
            n = int(p_)
        except (TypeError, ValueError):
            dropped_p += 1
            continue
        if 1 <= n <= SAFETY_PAGE_LIMIT:
            valid_pages.add(n)
        else:
            dropped_p += 1
    if dropped_p:
        log(f"Checkpoint: dropped {dropped_p} out-of-range page entries.", "warn")

    return valid_videos, valid_pages


def save_checkpoint(path, source_url, videos, pages_done):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    payload = {
        "source_url": source_url,
        "videos": videos,
        "pages_done": sorted(pages_done),
        "timestamp": datetime.now().isoformat(),
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def save_checkpoint_safe(path, source_url, videos, pages_done):
    global _checkpoint_broken
    if _checkpoint_broken:
        return
    try:
        save_checkpoint(path, source_url, videos, pages_done)
    except Exception as exc:
        _checkpoint_broken = True
        log(
            f"Checkpoint save failed ({type(exc).__name__}: {exc}); "
            f"resume disabled for this run.",
            "warn",
        )


def add_unique(videos, seen_urls, target):
    added = 0
    for v in videos:
        if v["url"] not in seen_urls:
            seen_urls.add(v["url"])
            target.append(v)
            added += 1
    return added


def fetch_range_parallel(base_url, pages, retries, workers, label):
    results = {}
    if not pages:
        return results
    submit_time = {}
    with ProgressTicker(len(pages), label) as ticker:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {}
            for n in pages:
                submit_time[n] = time.time()
                futures[ex.submit(fetch_page, page_url(base_url, n), retries, f"p{n}")] = n
            for fut in as_completed(futures):
                n = futures[fut]
                try:
                    results[n] = fut.result()
                except Exception as exc:
                    log(f"Page {n} raised {type(exc).__name__}: {exc}", "error")
                    results[n] = None
                elapsed = time.time() - submit_time[n]
                vlog(f"Page {n} finished in {elapsed:.1f}s", min_level=1)
                ticker.update()
    return results


def shape_records(videos, fields):
    if fields == "url":
        return [{"url": v["url"]} for v in videos]
    return [{"title": v["title"], "url": v["url"]} for v in videos]


def write_csv(path, records, fieldnames):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(records)
    tmp.replace(p)


def write_json(path, records, metadata):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    payload = dict(metadata)
    payload["records"] = records
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def save_output(path, fmt, records, fieldnames, metadata):
    try:
        if fmt == "csv":
            write_csv(path, records, fieldnames)
        elif fmt == "json":
            write_json(path, records, metadata)
        else:
            raise ValueError(f"Unsupported format: {fmt}")
    except Exception as exc:
        log(f"CRITICAL: failed to save output to {path}: {type(exc).__name__}: {exc}", "error")
        raise


def resolve_output_path(raw, fmt):
    p = Path(raw)
    if p.suffix.lower() != f".{fmt}":
        p = p.with_suffix(f".{fmt}")
    return p


def validate_output_path(path):
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    probe = parent / ".scraper_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        raise ValueError(f"Output directory not writable: {parent} ({exc})")


def normalize_profile_url(raw):
    m = PROFILE_URL_PATTERN.search(raw)
    if not m:
        return None
    return f"https://www.eporner.com/profile/{m.group(1)}/uploaded-videos/"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Eporner profile video scraper",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-u", "--url",
        required=True,
        help="Eporner profile URL (auto-normalized to /uploaded-videos/)",
    )
    parser.add_argument("-o", "--output", default="output", help="Output file path")
    parser.add_argument(
        "-f", "--format", choices=["csv", "json"], default="csv", help="Output format"
    )
    parser.add_argument(
        "-F", "--fields",
        choices=["all", "url"],
        default="all",
        help="Fields to save (all = title+url, url = url only)",
    )
    parser.add_argument("-w", "--workers", type=int, default=3, help="Parallel workers (1-10)")
    parser.add_argument("-r", "--retries", type=int, default=3, help="Retries per page (1-10)")
    parser.add_argument(
        "-c", "--checkpoint", default=DEFAULT_CHECKPOINT, help="Checkpoint file path"
    )
    parser.add_argument("-n", "--no-resume", action="store_true", help="Ignore existing checkpoint")
    parser.add_argument(
        "-k", "--keep-checkpoint",
        action="store_true",
        help="Keep checkpoint file after successful run",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v page timings, -vv per-video extraction)",
    )
    args = parser.parse_args()

    normalized = normalize_profile_url(args.url)
    if not normalized:
        parser.error("-u/--url must be an eporner.com/profile/<username> URL")
    args.url = normalized

    if args.workers < 1 or args.workers > 10:
        parser.error("-w/--workers must be between 1 and 10")
    if args.retries < 1 or args.retries > 10:
        parser.error("-r/--retries must be between 1 and 10")
    return args


def run(args):
    global _VERBOSE, _checkpoint_broken
    _VERBOSE = args.verbose
    _checkpoint_broken = False

    start = time.time()
    fieldnames = ["url"] if args.fields == "url" else ["title", "url"]

    if args.no_resume and Path(args.checkpoint).exists():
        try:
            Path(args.checkpoint).unlink()
        except Exception as exc:
            log(f"Could not remove checkpoint: {exc}", "warn")

    out_path = resolve_output_path(args.output, args.format)
    validate_output_path(out_path)

    log(f"Target: {args.url}", "step")
    log(
        f"Config: format={args.format}, fields={args.fields}, "
        f"workers={args.workers}, retries={args.retries}, verbose={args.verbose}"
    )

    all_videos, pages_done = load_checkpoint(args.checkpoint, args.url)
    if all_videos:
        log(f"Resuming with {len(all_videos)} videos across {len(pages_done)} pages")
    seen_urls = {v["url"] for v in all_videos}
    visited_page_urls = {page_url(args.url, p) for p in pages_done}

    log("Fetching page 1 and detecting candidate last page...", "step")
    needs_page_one = 1 not in pages_done
    candidate_last = detect_candidate_last_page(page_url(args.url, 1), args.retries)

    if needs_page_one:
        page1 = fetch_page(page_url(args.url, 1), args.retries, "p1")
        if page1 is None:
            log("Page 1 failed after retries; aborting run (resumable).", "error")
            return [], out_path, fieldnames, time.time() - start
        added = add_unique(page1, seen_urls, all_videos)
        pages_done.add(1)
        visited_page_urls.add(page_url(args.url, 1))
        save_checkpoint_safe(args.checkpoint, args.url, all_videos, pages_done)
        log(f"Page 1: +{added}, total {len(all_videos)}", "success")

    log(f"Candidate last page: {candidate_last}")

    target_max = max(candidate_last, max(pages_done, default=0))
    remaining = [n for n in range(2, target_max + 1) if n not in pages_done]

    if remaining:
        log(f"Fetching {len(remaining)} pages in parallel ({args.workers} workers)...", "step")
        results = fetch_range_parallel(
            args.url, remaining, args.retries, args.workers, "Parallel fetch"
        )
        for n in sorted(results.keys()):
            videos = results[n]
            if videos is None:
                log(f"Page {n} failed in parallel phase; queued for retry.", "warn")
                continue
            added = add_unique(videos, seen_urls, all_videos)
            pages_done.add(n)
            visited_page_urls.add(page_url(args.url, n))
            save_checkpoint_safe(args.checkpoint, args.url, all_videos, pages_done)
            log(f"Page {n}: +{added}, total {len(all_videos)}", "success")

        failed = sorted([n for n, v in results.items() if v is None])
        if failed:
            log(f"Retrying {len(failed)} failed pages sequentially: {failed}", "warn")
            for n in failed:
                videos = fetch_page(page_url(args.url, n), args.retries, f"p{n}")
                if videos is None:
                    log(f"Page {n} still failed after retry; missing from output.", "error")
                    continue
                added = add_unique(videos, seen_urls, all_videos)
                pages_done.add(n)
                visited_page_urls.add(page_url(args.url, n))
                save_checkpoint_safe(args.checkpoint, args.url, all_videos, pages_done)
                log(f"Page {n} (retry): +{added}, total {len(all_videos)}", "success")

    log("Verifying true end of pagination (probing beyond candidate)...", "step")
    probe = max(candidate_last, max(pages_done, default=0)) + 1
    extended = 0

    while probe <= SAFETY_PAGE_LIMIT:
        url = page_url(args.url, probe)
        if url in visited_page_urls:
            log(f"Page {probe} URL already visited; stopping (cycle detected).", "warn")
            break

        videos = fetch_page(url, args.retries, f"p{probe}")
        visited_page_urls.add(url)

        if videos is None:
            log(f"Page {probe} failed; sleeping 3s then retrying once.", "warn")
            time.sleep(3)
            videos = fetch_page(url, args.retries, f"p{probe}")

        if videos is None:
            log(f"Page {probe} still failed; stopping verification.", "error")
            break

        if not videos:
            log(f"Page {probe} is empty; confirmed true end at page {probe - 1}.", "success")
            break

        added = add_unique(videos, seen_urls, all_videos)
        pages_done.add(probe)
        save_checkpoint_safe(args.checkpoint, args.url, all_videos, pages_done)
        extended += 1
        log(f"Page {probe} (extension): +{added}, total {len(all_videos)}", "success")
        probe += 1

    if extended:
        log(f"Extended beyond candidate by {extended} page(s).")

    if _checkpoint_broken:
        log(
            "Note: checkpoint was broken during this run; "
            "resume unavailable for next run.",
            "warn",
        )

    records = shape_records(all_videos, args.fields)

    metadata = {
        "source_url": args.url,
        "total_videos": len(records),
        "scraped_at": datetime.now().isoformat(),
        "fields": fieldnames,
    }

    log(f"Saving {len(records)} records to {out_path} ({args.format})...", "step")
    save_output(str(out_path), args.format, records, fieldnames, metadata)

    if not args.keep_checkpoint and Path(args.checkpoint).exists():
        try:
            Path(args.checkpoint).unlink()
            log("Checkpoint removed.")
        except Exception as exc:
            log(f"Could not remove checkpoint: {exc}", "warn")

    if not records:
        log(
            "WARNING: no videos were extracted. Check URL, network, or site structure.",
            "warn",
        )

    elapsed = time.time() - start
    log(f"Done in {elapsed:.1f}s. {len(records)} records written.", "success")
    return records, out_path, fieldnames, elapsed


def print_summary(records, out_path, fieldnames, elapsed, args):
    print()
    print("=" * 60)
    print(f"{Fore.CYAN}SUMMARY{Style.RESET_ALL}")
    print("=" * 60)
    print(f"Source    : {Fore.BLUE}{args.url}{Style.RESET_ALL}")
    print(f"Records   : {Fore.GREEN}{len(records)}{Style.RESET_ALL}")
    print(f"Output    : {Fore.BLUE}{out_path}{Style.RESET_ALL}")
    print(f"Format    : {Fore.BLUE}{args.format}{Style.RESET_ALL}")
    print(f"Fields    : {Fore.BLUE}{', '.join(fieldnames)}{Style.RESET_ALL}")
    print(f"Elapsed   : {Fore.BLUE}{elapsed:.1f}s{Style.RESET_ALL}")
    print("=" * 60)
    for r in records[:5]:
        if "title" in r and r["title"]:
            print(f"{Fore.GREEN}- {r['title'][:60]}{Style.RESET_ALL}")
            print(f"  {Fore.BLUE}{r['url']}{Style.RESET_ALL}")
        else:
            print(f"{Fore.BLUE}{r['url']}{Style.RESET_ALL}")
    if len(records) > 5:
        print(f"{Fore.YELLOW}... and {len(records) - 5} more{Style.RESET_ALL}")


def main():
    args = parse_args()
    try:
        records, out_path, fieldnames, elapsed = run(args)
    except KeyboardInterrupt:
        log("Interrupted by user.", "warn")
        return 130
    except ValueError as exc:
        log(f"Validation error: {exc}", "error")
        return 2
    except Exception as exc:
        log(f"Fatal error: {type(exc).__name__}: {exc}", "error")
        return 1
    finally:
        close_all_drivers()

    try:
        print_summary(records, out_path, fieldnames, elapsed, args)
    except Exception as exc:
        log(f"Summary display failed ({exc}); output already saved.", "warn")

    return 0


if __name__ == "__main__":
    sys.exit(main())
