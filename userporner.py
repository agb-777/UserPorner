import argparse
import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from colorama import Fore, Style, init
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

init(autoreset=True)

DEFAULT_CHECKPOINT = ".scraper_checkpoint.json"
PROFILE_RE = re.compile(r"https?://(?:www\.)?eporner\.com/profile/([^/?#]+)", re.IGNORECASE)
PAGE_RE = re.compile(r"/uploaded-videos/(\d+)(?:[/?#]|$)")
VIDEO_SELECTOR = 'a[href*="/video-"]'
CONTAINER_SELECTOR = ".mb"
PAGE_LOAD_TIMEOUT = 30
RENDER_TIMEOUT = 15
RETRY_DELAY = 2
SAFETY_PAGE_LIMIT = 100000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
COLORS = {
    "info": Fore.CYAN,
    "success": Fore.GREEN,
    "warn": Fore.YELLOW,
    "error": Fore.RED,
    "step": Fore.MAGENTA,
    "dim": Fore.WHITE,
}

_print_lock = threading.Lock()
_tls = threading.local()
_drivers = []
_drivers_lock = threading.Lock()
_closing = threading.Event()
_verbose = False


def log(msg, level="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    with _print_lock:
        print(f"{COLORS.get(level, Fore.WHITE)}[{ts}] {msg}{Style.RESET_ALL}", flush=True)


def build_options():
    opts = Options()
    for arg in (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1920,1080",
        "--disable-blink-features=AutomationControlled",
        "--log-level=3",
        f"user-agent={USER_AGENT}",
    ):
        opts.add_argument(arg)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return opts


def new_driver():
    driver = webdriver.Chrome(options=build_options())
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    with _drivers_lock:
        _drivers.append(driver)
    return driver


def quit_driver(driver):
    try:
        driver.quit()
    except Exception:
        pass
    with _drivers_lock:
        if driver in _drivers:
            _drivers.remove(driver)


def thread_driver():
    if _closing.is_set():
        raise RuntimeError("proses sedang dihentikan")
    driver = getattr(_tls, "driver", None)
    if driver is None:
        driver = new_driver()
        _tls.driver = driver
    return driver


def discard_thread_driver():
    driver = getattr(_tls, "driver", None)
    if driver is not None:
        _tls.driver = None
        quit_driver(driver)


def close_all_drivers():
    _closing.set()
    with _drivers_lock:
        drivers = list(_drivers)
    for driver in drivers:
        quit_driver(driver)


def page_url(base, n):
    return base if n == 1 else f"{base}{n}/"


def page_number_from(url):
    m = PAGE_RE.search(url or "")
    return int(m.group(1)) if m else 1


def normalize_profile_url(raw):
    m = PROFILE_RE.search(raw)
    return f"https://www.eporner.com/profile/{m.group(1)}/uploaded-videos/" if m else None


def wait_for_videos(driver):
    WebDriverWait(driver, RENDER_TIMEOUT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, VIDEO_SELECTOR))
    )


def page_is_empty(driver):
    return not (
        driver.find_elements(By.CSS_SELECTOR, CONTAINER_SELECTOR)
        or driver.find_elements(By.CSS_SELECTOR, VIDEO_SELECTOR)
    )


def title_of(anchor):
    title = (anchor.get_attribute("title") or "").strip()
    if title:
        return title
    imgs = anchor.find_elements(By.TAG_NAME, "img")
    if imgs:
        alt = (imgs[0].get_attribute("alt") or "").strip()
        if alt:
            return alt
    return (anchor.text or "").strip()


def extract_videos(driver):
    anchors = []
    for container in driver.find_elements(By.CSS_SELECTOR, CONTAINER_SELECTOR):
        anchors.extend(container.find_elements(By.CSS_SELECTOR, VIDEO_SELECTOR))
    if not anchors:
        anchors = driver.find_elements(By.CSS_SELECTOR, VIDEO_SELECTOR)

    found = {}
    for anchor in anchors:
        try:
            url = anchor.get_attribute("href") or ""
            title = title_of(anchor)
        except WebDriverException:
            continue
        if "/video-" not in url:
            continue
        if url not in found or (title and not found[url]):
            found[url] = title
    return [{"title": t, "url": u} for u, t in found.items()]


def load_listing(driver, url):
    driver.get(url)
    if page_number_from(driver.current_url) != page_number_from(url):
        return []
    try:
        wait_for_videos(driver)
    except TimeoutException:
        return [] if page_is_empty(driver) else None
    return extract_videos(driver)


def fetch_page(url, retries):
    for attempt in range(1, retries + 1):
        try:
            videos = load_listing(thread_driver(), url)
            reason = "halaman tidak tampil"
        except Exception as exc:
            discard_thread_driver()
            videos = None
            reason = type(exc).__name__
        if videos is not None:
            return videos
        if attempt < retries:
            log(f"Percobaan {attempt}/{retries} gagal ({reason}): {url}", "warn")
            time.sleep(RETRY_DELAY)
    log(f"Semua {retries} percobaan gagal: {url}", "error")
    return None


def walk_next_links(driver, start_url):
    last = 1
    url = start_url
    visited = set()
    while url and url not in visited and len(visited) < SAFETY_PAGE_LIMIT:
        visited.add(url)
        driver.get(url)
        try:
            wait_for_videos(driver)
        except TimeoutException:
            break
        last = max(last, page_number_from(driver.current_url))
        links = driver.find_elements(By.CSS_SELECTOR, 'link[rel="next"]')
        url = links[0].get_attribute("href") if links else None
    return last


def detect_once(driver, start_url):
    driver.get(start_url)
    try:
        wait_for_videos(driver)
    except TimeoutException:
        if page_is_empty(driver):
            return 1
        raise

    candidate = 1
    for link in driver.find_elements(By.CSS_SELECTOR, 'link[rel="last"]'):
        candidate = max(candidate, page_number_from(link.get_attribute("href")))
    for anchor in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/uploaded-videos/"]'):
        candidate = max(candidate, page_number_from(anchor.get_attribute("href")))

    if candidate == 1:
        log("Tidak ada penanda halaman terakhir, menelusuri rel=next satu per satu (bisa lambat).", "warn")
        candidate = walk_next_links(driver, start_url)
    return min(candidate, SAFETY_PAGE_LIMIT)


def detect_last_page(start_url, retries):
    for attempt in range(1, retries + 1):
        try:
            return detect_once(thread_driver(), start_url)
        except Exception as exc:
            discard_thread_driver()
            if attempt == retries:
                raise
            log(f"Deteksi halaman terakhir gagal ({attempt}/{retries}): {type(exc).__name__}", "warn")
            time.sleep(RETRY_DELAY)


class Store:
    def __init__(self, path, source_url):
        self.path = Path(path)
        self.source_url = source_url
        self.videos = []
        self.urls = set()
        self.pages_done = set()

    def load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log(f"Checkpoint tidak bisa dibaca ({exc}), mulai dari awal.", "warn")
            return
        if not isinstance(data, dict) or data.get("source_url") != self.source_url:
            log("Checkpoint milik URL lain atau rusak, diabaikan.", "warn")
            return
        for v in data.get("videos", []):
            if isinstance(v, dict) and isinstance(v.get("url"), str) and isinstance(v.get("title"), str) and v["url"]:
                if v["url"] not in self.urls:
                    self.urls.add(v["url"])
                    self.videos.append({"title": v["title"], "url": v["url"]})
        for n in data.get("pages_done", []):
            if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= SAFETY_PAGE_LIMIT:
                self.pages_done.add(n)
        if self.videos:
            log(f"Melanjutkan dengan {len(self.videos)} video dari {len(self.pages_done)} halaman")

    def add_page(self, page, videos):
        added = 0
        for v in videos:
            if v["url"] not in self.urls:
                self.urls.add(v["url"])
                self.videos.append(v)
                added += 1
        self.pages_done.add(page)
        self.save()
        return added

    def save(self):
        payload = {
            "source_url": self.source_url,
            "videos": self.videos,
            "pages_done": sorted(self.pages_done),
            "timestamp": datetime.now().isoformat(),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log(f"Checkpoint gagal disimpan ({exc}); resume mungkin tidak tersedia.", "warn")

    def remove(self):
        self.path.unlink(missing_ok=True)


def record_page(store, page, videos):
    added = store.add_page(page, videos)
    log(f"Halaman {page}: +{added}, total {len(store.videos)}", "success")


def fetch_and_record(store, base, page, retries):
    videos = fetch_page(page_url(base, page), retries)
    if videos is None:
        return False
    record_page(store, page, videos)
    return True


def fetch_parallel(base, pages, retries, workers):
    results = {}
    started = {}
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {}
        for n in pages:
            started[n] = time.monotonic()
            futures[executor.submit(fetch_page, page_url(base, n), retries)] = n
        for future in as_completed(futures):
            n = futures[future]
            try:
                results[n] = future.result()
            except Exception:
                results[n] = None
            if _verbose:
                log(f"Halaman {n} selesai dalam {time.monotonic() - started[n]:.1f}s", "dim")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def shape_records(videos, fields):
    if fields == "url":
        return [{"url": v["url"]} for v in videos]
    return [{"title": v["title"], "url": v["url"]} for v in videos]


def output_path(raw, fmt):
    p = Path(raw)
    if p.suffix.lower() != f".{fmt}":
        p = p.with_name(p.name + f".{fmt}")
    return p


def check_writable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    probe = path.parent / ".write_probe"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"folder output tidak bisa ditulis: {path.parent} ({exc})")
    finally:
        probe.unlink(missing_ok=True)


def write_output(path, fmt, records, fieldnames, metadata):
    tmp = path.with_name(path.name + ".tmp")
    if fmt == "csv":
        with tmp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)
    else:
        payload = {**metadata, "records": records}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass
class Result:
    records: list
    out_path: Path
    fieldnames: list
    elapsed: float
    complete: bool


def run(args):
    start = time.monotonic()
    fieldnames = ["url"] if args.fields == "url" else ["title", "url"]
    out_path = output_path(args.output, args.format)
    check_writable(out_path)

    checkpoint = Path(args.checkpoint)
    if args.no_resume:
        checkpoint.unlink(missing_ok=True)

    store = Store(checkpoint, args.url)
    store.load()

    log(f"Target: {args.url}", "step")
    log(f"Konfigurasi: format={args.format}, fields={args.fields}, workers={args.workers}, retries={args.retries}")

    log("Mendeteksi halaman terakhir...", "step")
    candidate = detect_last_page(page_url(args.url, 1), args.retries)
    log(f"Perkiraan halaman terakhir: {candidate}")

    if 1 not in store.pages_done:
        if not fetch_and_record(store, args.url, 1, args.retries):
            raise RuntimeError("halaman 1 gagal setelah beberapa percobaan; checkpoint dipertahankan")

    last_known = max(candidate, max(store.pages_done, default=1))
    pending = [n for n in range(2, last_known + 1) if n not in store.pages_done]
    still_missing = []

    if pending:
        log(f"Mengambil {len(pending)} halaman dengan {args.workers} worker...", "step")
        results = fetch_parallel(args.url, pending, args.retries, args.workers)
        failed = []
        for n in sorted(results):
            if results[n] is None:
                failed.append(n)
            else:
                record_page(store, n, results[n])
        if failed:
            log(f"Mengulang {len(failed)} halaman yang gagal: {failed}", "warn")
            still_missing = [n for n in failed if not fetch_and_record(store, args.url, n, args.retries)]

    log("Memverifikasi akhir pagination...", "step")
    probe = last_known + 1
    verified = True
    while probe <= SAFETY_PAGE_LIMIT:
        videos = fetch_page(page_url(args.url, probe), args.retries)
        if videos is None:
            log(f"Halaman {probe} gagal, verifikasi dihentikan.", "error")
            verified = False
            break
        if not videos:
            log(f"Halaman {probe} kosong, akhir data ada di halaman {probe - 1}.", "success")
            break
        record_page(store, probe, videos)
        probe += 1

    complete = verified and not still_missing
    if still_missing:
        log(f"Halaman berikut tetap gagal dan tidak masuk output: {still_missing}", "error")

    records = shape_records(store.videos, args.fields)
    metadata = {
        "source_url": args.url,
        "total_videos": len(records),
        "scraped_at": datetime.now().isoformat(),
        "fields": fieldnames,
    }
    log(f"Menyimpan {len(records)} record ke {out_path}...", "step")
    write_output(out_path, args.format, records, fieldnames, metadata)

    if not records:
        log("PERINGATAN: tidak ada video yang diambil. Cek URL, jaringan, atau struktur situs.", "warn")

    if complete:
        if not args.keep_checkpoint:
            store.remove()
    else:
        log("Hasil belum lengkap. Checkpoint dipertahankan, jalankan ulang untuk melengkapi.", "warn")

    return Result(records, out_path, fieldnames, time.monotonic() - start, complete)


def print_summary(result, args):
    print()
    print("=" * 60)
    print(f"{Fore.CYAN}RINGKASAN{Style.RESET_ALL}")
    print("=" * 60)
    print(f"Sumber    : {Fore.BLUE}{args.url}{Style.RESET_ALL}")
    print(f"Record    : {Fore.GREEN}{len(result.records)}{Style.RESET_ALL}")
    print(f"Output    : {Fore.BLUE}{result.out_path}{Style.RESET_ALL}")
    print(f"Format    : {args.format}")
    print(f"Fields    : {', '.join(result.fieldnames)}")
    print(f"Waktu     : {result.elapsed:.1f}s")
    print(f"Status    : {'lengkap' if result.complete else 'TIDAK LENGKAP'}")
    print("=" * 60)
    for r in result.records[:5]:
        if r.get("title"):
            print(f"{Fore.GREEN}- {r['title'][:60]}{Style.RESET_ALL}")
            print(f"  {Fore.BLUE}{r['url']}{Style.RESET_ALL}")
        else:
            print(f"{Fore.BLUE}{r['url']}{Style.RESET_ALL}")
    if len(result.records) > 5:
        print(f"{Fore.YELLOW}... dan {len(result.records) - 5} lainnya{Style.RESET_ALL}")


def positive_int(low, high):
    def parse(text):
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"harus di antara {low} dan {high}")
        return value
    return parse


def parse_args():
    parser = argparse.ArgumentParser(description="Scraper video profil Eporner")
    parser.add_argument("-u", "--url", required=True, help="URL profil Eporner")
    parser.add_argument("-o", "--output", default="output", help="Path file output")
    parser.add_argument("-f", "--format", choices=["csv", "json"], default="csv")
    parser.add_argument("-F", "--fields", choices=["all", "url"], default="all", help="all = title+url, url = hanya url")
    parser.add_argument("-w", "--workers", type=positive_int(1, 10), default=3)
    parser.add_argument("-r", "--retries", type=positive_int(1, 10), default=3)
    parser.add_argument("-c", "--checkpoint", default=DEFAULT_CHECKPOINT)
    parser.add_argument("-n", "--no-resume", action="store_true", help="abaikan checkpoint lama")
    parser.add_argument("-k", "--keep-checkpoint", action="store_true", help="simpan checkpoint setelah sukses")
    parser.add_argument("-v", "--verbose", action="store_true", help="tampilkan waktu tiap halaman")
    args = parser.parse_args()

    normalized = normalize_profile_url(args.url)
    if normalized is None:
        parser.error("-u/--url harus berupa URL eporner.com/profile/<username>")
    args.url = normalized
    return args


def main():
    global _verbose
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    _verbose = args.verbose

    try:
        result = run(args)
    except KeyboardInterrupt:
        log("Dihentikan oleh pengguna. Jalankan ulang untuk melanjutkan.", "warn")
        return 130
    except ValueError as exc:
        log(f"Error validasi: {exc}", "error")
        return 2
    except Exception as exc:
        log(f"Error fatal: {type(exc).__name__}: {exc}", "error")
        return 1
    finally:
        close_all_drivers()

    print_summary(result, args)
    return 0 if result.complete else 1


if __name__ == "__main__":
    sys.exit(main())
