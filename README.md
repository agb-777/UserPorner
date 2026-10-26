# UserPorner

![License](https://img.shields.io/badge/license-MIT-brightgreen.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-purple.svg)

<img src="https://github.com/agb-777/userporner/blob/main/img/icon.png" width="600" />

Export the video list of an [Eporner](https://www.eporner.com/) profile to CSV, JSON, Markdown, or PDF.

## Table of Contents

- [About](#about)
- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Options](#options)
- [How It Works](#how-it-works)
- [Output](#output)
- [Resume](#resume)
- [Limitations](#limitations)
- [Disclaimer](#disclaimer)
- [Screenshots](#screenshots)
- [License](#license)

## About

UserPorner reads every page of a profile's uploaded videos and saves the titles and URLs to a file. It uses plain HTTP and a small HTML parser, so it does not need a browser.

Pages are fetched in small parallel batches with a short pause between requests. Progress is saved after every page, so an interrupted run can continue where it stopped.

## Features

- Accepts a full profile URL or just the username.
- Four output formats: CSV, JSON, Markdown table, and PDF.
- Title and URL, or URL only.
- Parallel requests with a configurable number of workers.
- Automatic retries per page, with growing delays.
- Resume support through a checkpoint file for each profile.
- Interactive mode for step-by-step setup.

## Requirements

- Python 3.9 or newer
- Git, to clone the repository
- Internet connection
- `colorama` (installed in the next section)
- `reportlab` (optional, only for PDF output)

No browser is needed.

## Install

Clone the repository and install the dependencies:

```bash
git clone https://github.com/agb-777/userporner.git
cd userporner
pip install colorama
pip install reportlab   # optional, only for PDF
```

Optional, create a virtual environment first.

Linux and macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows:

```powershell
py -m venv .venv
.venv\Scripts\activate
```

## Usage

The `-u` option accepts a full profile URL or just the username:

```bash
python userporner.py -u USERNAME -o result -f md
python userporner.py -u https://www.eporner.com/profile/USERNAME/ -o urls -f json -F url
```

Interactive mode asks for each setting one by one:

```bash
python userporner.py -i
```

Nothing is saved unless `-o` is given. Without `-o`, the scraper still runs and prints a summary.

## Options

| Short | Long | Default | Description |
|---|---|---|---|
| `-u` | `--url` | required | Profile URL or username |
| `-o` | `--output` | none | Output file path. The extension is added from `-f` if missing |
| `-f` | `--format` | `csv` | `csv`, `json`, `md`, or `pdf` |
| `-F` | `--fields` | `all` | `all` (title and URL) or `url` (URL only) |
| `-w` | `--workers` | `3` | Parallel requests, 1 to 10 |
| `-r` | `--retries` | `3` | Attempts per page, 1 to 10 |
| `-c` | `--checkpoint` | `.scraper_checkpoint_<username>.json` | Checkpoint file path |
| `-n` | `--no-resume` | off | Delete the checkpoint and start from scratch |
| `-k` | `--keep-checkpoint` | off | Keep the checkpoint after a complete run |
| `-v` | `--verbose` | off | Show the time taken for each batch of pages |
| `-i` | `--interactive` | off | Ask all settings step by step |
| `-h` | `--help` | | Show help |

`-i` cannot be combined with the other options.

## How It Works

1. **Check the input.** The URL or username is converted into the profile's `/uploaded-videos/` listing address.
2. **Load the checkpoint.** If a checkpoint for this profile exists, pages already saved are not fetched again.
3. **Fetch in batches.** Pages are requested in groups of `-w` pages at a time. Each request waits briefly before it is sent.
4. **Read each page.** The script collects links to videos on `eporner.com`. A title comes from the link's `title` attribute, then the image's alt text, then the link text.
5. **Find the end.** The listing ends when a page is missing (404 or redirected away), when it is a real listing with no videos, or when it repeats the previous page. Each of these must be confirmed on every attempt, so one temporary error cannot end the listing early.
6. **Stop on failure.** If a page still fails after all attempts, the run stops and is marked incomplete. Run the same command again to continue.
7. **Save the output.** Duplicate URLs are removed. The file is written to a temporary file first and then renamed, so a crash never leaves a half-written output.

## Output

| Format | Contents |
|---|---|
| `csv` | Records only, UTF-8 with BOM so Excel shows non-Latin text correctly |
| `json` | Author, homepage, source URL, total count, timestamp, fields, and records |
| `md` | Title, source, totals, author, and homepage, followed by a numbered table with clickable URLs |
| `pdf` | Landscape A4 with a title, source line, author line, repeated table header, and page numbers |

The PDF uses a standard font, so titles in non-Latin scripts may not render.

## Resume

Each finished page is saved to a checkpoint file while the run is going. If a run stops, run the same command again to continue.

Each profile has its own checkpoint file, so two profiles never overwrite each other. The checkpoint is removed only when the run is complete and the results were saved with `-o`, unless `-k` is used. Without `-o`, the checkpoint is kept so the data is not lost.

A checkpoint that is already complete is reused as it is. Videos added to the profile after that point are not fetched until you run with `-n`.

If the site no longer matches a checkpoint, the run stops and asks you to use `-n`.

## Limitations

- The scraper reads the page layout: the `/video-` link pattern, the `/uploaded-videos/` listing marker, and the `mb` container class. If the site changes its layout, scraping may return no results. The patterns are at the top of `userporner.py`, and the `mb` class is in `_VideoParser`.
- Pages that need JavaScript or cookies to show their videos are not supported. The run will report the problem instead of saving partial data as complete.
- Use a small number of workers and avoid running several scrapes at the same time, to keep the load on the site low.

## Disclaimer

- This project is not affiliated with, endorsed by, or connected to Eporner.
- The source site contains adult content. Use this tool only if you are at least 18 years old and the content is legal where you live.
- You are responsible for how you use the output. Follow the site's terms of service, `robots.txt`, and the copyright and privacy laws that apply to you.
- The software is provided as is, without warranty of any kind. The author is not liable for any damage, account action, or legal issue that results from its use.

## Screenshots

![Screenshot of the CLI run](https://github.com/agb-777/userporner/blob/main/img/sikrinsot1.png)

*Figure 1. Running the scraper from the command line.*

![Screenshot of the output](https://github.com/agb-777/userporner/blob/main/img/sikrinsot2.png)

*Figure 2. Result summary after a completed run.*

## License

[MIT](https://github.com/agb-777/userporner/blob/main/LICENSE)
