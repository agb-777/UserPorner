# UserPorner

![License](https://img.shields.io/badge/license-MIT-brightgreen.svg)
![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-purple.svg)

## About

A command-line scraper for [Eporner](https://www.eporner.com/) profile pages. Given a username, it walks through every page of that user's uploaded videos and collects the title and URL of each video.

The result is written to a CSV or JSON file. You can save both fields, or only the URLs.

The scraper uses Selenium with headless Chrome, so the page is loaded and rendered like a real browser. This is needed because the video list is built by JavaScript and cannot be read from the raw HTML.

## How It Works

The script runs in four phases.

**1. Page detection.** It opens page 1 of the profile and figures out how many pages exist. This uses three checks in order:

- The `<link rel="last">` tag if the page provides it.
- The highest page number found in the visible pagination links.
- A slow walk through `rel="next"` links if neither of the above yields a result.

**2. Parallel fetch.** All pages up to the detected last page are fetched in parallel using multiple Chrome instances. Each page is retried on failure. The extraction on each page looks for `.mb` containers first, then pulls every anchor that points to a video. Titles are read from the anchor's `title` attribute, then from a nested `img` alt text, then from the visible text.

**3. End verification.** The detected last page may be wrong if the site only shows a few page numbers in the pagination widget. To handle this, the script probes page by page beyond the detected last page until it finds an empty page. That empty page is the true end. If a page fails all retries, the probe stops and the run is resumable.

**4. Save.** Duplicates are removed by URL, then the results are written to the output file. CSV and JSON are written to a temporary file first, then renamed into place, so a crash during save will not leave a half written file.

Every page is also written to a checkpoint file as it completes. If the run is interrupted, running the same command again will pick up where it left off, skipping pages that are already done.

## Requirements

- Python 3.8 or newer
- Google Chrome or Chromium
- Internet connection

## Installation

### Step 1: Install Python

**Windows**

Open PowerShell and check if Python is already installed:

```powershell
py --version
```

If the command is not found, download the official installer from [python.org/downloads](https://www.python.org/downloads/). Run the installer and make sure to check **"Add python.exe to PATH"** on the first screen before clicking Install.

For Python 3.14 and newer on Windows, the default installer is the **Python Install Manager** (`pymanager`). It handles multiple Python versions and integrates with the `py` launcher. You can get it from python.org or the Microsoft Store.

After installation, verify:

```powershell
py --version
```

If `python` opens the Microsoft Store instead of running Python, disable the App Execution Aliases for `python.exe` and `python3.exe` in Windows Settings, then use `py` instead of `python`.

**macOS**

Check if Python 3 is available:

```bash
python3 --version
```

If not, you have two options.

Option A: Official installer. Download the macOS universal2 package from [python.org/downloads](https://www.python.org/downloads/), open the `.pkg` file, and follow the installer. This works on both Apple Silicon and Intel Macs.

Option B: Homebrew. If you use Homebrew, install Python with:

```bash
brew install python
```

This keeps Python updated alongside your other tools.

After installation, verify:

```bash
python3 --version
```

**Linux**

Most distributions come with Python 3 preinstalled. Check with:

```bash
python3 --version
```

If it is missing or you want a newer version, use your distribution's package manager.

Ubuntu and Debian:

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv
```

Fedora:

```bash
sudo dnf install python3 python3-pip
```

Arch Linux:

```bash
sudo pacman -S python python-pip
```

Verify:

```bash
python3 --version
```

### Step 2: Install Google Chrome

The script requires Chrome to render the pages.

**Windows**

Download Chrome from [google.com/chrome](https://www.google.com/chrome), run the installer, and follow the on-screen instructions. Chrome requires Windows 10 or newer.

**macOS**

Download Chrome from [google.com/chrome](https://www.google.com/chrome), open the `.dmg` file, and drag Chrome to the Applications folder. Chrome requires macOS 13 Ventura or newer.

**Linux**

Download the `.deb` package for Debian/Ubuntu or the `.rpm` package for Fedora/openSUSE from [google.com/chrome](https://www.google.com/chrome), then install it with your package manager.

For Ubuntu/Debian, the command line way:

```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install ./google-chrome-stable_current_amd64.deb
```

Chrome on Linux requires 64-bit Ubuntu 18.04+, Debian 10+, openSUSE 15.5+, or Fedora 39+.

### Step 3: Get the Script

Clone the repository:

```bash
git clone https://github.com/agb-777/userporner.git
cd userporner
```

Or download the script directly:

```bash
curl -O https://raw.githubusercontent.com/agb-777/userporner/main/userporner.py
```

### Step 4: Install Dependencies

From inside the project folder:

```bash
pip install selenium webdriver-manager colorama
```

If `pip` is not found on Linux, use `pip3` instead:

```bash
pip3 install selenium webdriver-manager colorama
```

On Windows, if `pip` is not on PATH, use the `py` launcher:

```powershell
py -m pip install selenium webdriver-manager colorama
```

Verify the dependencies:

```bash
pip show selenium
```

## Usage

```bash
python userporner.py -u "https://www.eporner.com/profile/USERNAME/"
```

The `/uploaded-videos/` suffix is added automatically, so both the profile URL and the full listing URL work.

On Windows, if `python` is not on PATH, use `py`:

```powershell
py userporner.py -u "https://www.eporner.com/profile/USERNAME/"
```

### Examples

```bash
# URL-only output as JSON
python userporner.py -u "https://www.eporner.com/profile/USERNAME/" -F url -f json -o urls

# Custom path, more workers
python userporner.py -u "https://www.eporner.com/profile/USERNAME/" -o results/out -w 5

# Fresh run, ignore existing checkpoint
python userporner.py -u "https://www.eporner.com/profile/USERNAME/" -n

# Verbose logs
python userporner.py -u "https://www.eporner.com/profile/USERNAME/" -v
```

### Options

| Short | Long | Default | Description |
|---|---|---|---|
| `-u` | `--url` | required | Eporner profile URL (auto-normalized to `/uploaded-videos/`) |
| `-o` | `--output` | `output` | Output file path |
| `-f` | `--format` | `csv` | Output format, `csv` or `json` |
| `-F` | `--fields` | `all` | `all` (title + url) or `url` |
| `-w` | `--workers` | `3` | Parallel workers, 1 to 10 |
| `-r` | `--retries` | `3` | Retries per page, 1 to 10 |
| `-c` | `--checkpoint` | `.scraper_checkpoint.json` | Checkpoint file path |
| `-n` | `--no-resume` | off | Ignore existing checkpoint |
| `-k` | `--keep-checkpoint` | off | Keep checkpoint after success |
| `-v` | `--verbose` | `0` | Verbosity, `-v` for page timings, `-vv` for per-video |
| `-h` | `--help` | none | Show help |

### Output

CSV:

```csv
title,url
Video Title 1,https://www.eporner.com/video-xxxxx/video-title-1/
Video Title 2,https://www.eporner.com/video-yyyyy/video-title-2/
```

JSON:

```json
{
  "source_url": "https://www.eporner.com/profile/USERNAME/uploaded-videos/",
  "total_videos": 726,
  "scraped_at": "2026-10-25T19:30:00",
  "fields": ["title", "url"],
  "records": [
    {
      "title": "Video Title 1",
      "url": "https://www.eporner.com/video-xxxxx/video-title-1/"
    }
  ]
}
```

### Resume

The checkpoint is saved after every page. If a run is interrupted, re-run the same command to continue. The checkpoint is tied to the source URL, so changing `-u` starts a fresh run.

## License

MIT
