# UserPorner

<img src="https://github.com/agb-777/userporner/blob/main/img/icon.png" width="600" />

![License](https://img.shields.io/badge/license-MIT-brightgreen.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-purple.svg)

Export the video list of an [Eporner](https://www.eporner.com/) profile to TXT, CSV, JSON, Markdown, HTML, or PDF.

## Table of Contents

- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Options](#options)
- [Formats](#formats)
- [Resume](#resume)
- [Notes](#notes)
- [Disclaimer](#disclaimer)
- [License](#license)

## Requirements

| Requirement | Used for | Needed |
|---|---|---|
| Python 3.9 or newer | Runs the script | Yes |
| Git | Downloads the repository | Yes |
| Internet connection | Reads profile pages from eporner.com | Yes |
| `colorama` | Colored terminal output | Yes |
| `reportlab` | Builds PDF files (`-f pdf`) | Only for PDF |

## Install

Clone the repository, then install the dependencies:

```bash
git clone https://github.com/agb-777/userporner.git
cd userporner
pip install colorama reportlab
```

Notes:

- `reportlab` is only used for `-f pdf`. The other formats work without it. To skip it, run `pip install colorama`.
- Use `pip3` if `pip` is not found. On Windows, use `py -m pip` if `pip` is not on PATH.
- To keep the packages separate from other projects, create a virtual environment first with `python -m venv .venv`, then activate it. Windows uses `.venv\Scripts\activate`, Linux and macOS use `source .venv/bin/activate`.

Check that the script runs:

```bash
python userporner.py --help
```

On Windows, use `py userporner.py --help` if `python` is not on PATH.

## Usage

Save the video list as a text file. The default format is `txt`:

```bash
python userporner.py -u USERNAME -o result
```

Save as an HTML page with thumbnails shown as pictures:

```bash
python userporner.py -u USERNAME -o result -f html
```

Save only the URLs as CSV:

```bash
python userporner.py -u USERNAME -o urls -f csv -F url
```

Save as a PDF. Thumbnails are downloaded first, so this takes longer:

```bash
python userporner.py -u USERNAME -o result -f pdf -w 2
```

Save progress so the run can be continued later:

```bash
python userporner.py -u USERNAME -o result -s progress.json
```

Continue an earlier run from its progress file:

```bash
python userporner.py -u USERNAME -o result -R progress.json
```

Use a full profile URL instead of the username:

```bash
python userporner.py -u https://www.eporner.com/profile/USERNAME/ -o result -f md
```

No output file is written unless `-o` is given. Progress files are only written when you use `-s`.

## Options

| Option | Default | Description |
|---|---|---|
| `-u`, `--url` | required | Profile URL or username |
| `-o`, `--output` | none | Output file. The extension is added from `-f` if missing |
| `-f`, `--format` | `txt` | `txt`, `csv`, `json`, `md`, `html`, or `pdf` |
| `-F`, `--fields` | `all` | `all` (title, thumbnail, views, URL) or `url` (URL only) |
| `-w`, `--workers` | `3` | Parallel requests, 1 to 10 |
| `-r`, `--retries` | `3` | Attempts per page, 1 to 10 |
| `-s`, `--save-checkpoint` | none | Save progress to FILE after every page |
| `-R`, `--resume` | none | Continue from a FILE made with `-s` |
| `-v`, `--verbose` | off | Show time per batch of pages |
| `-h`, `--help` | | Show help |

## Formats

| Format | Thumbnail | Notes |
|---|---|---|
| `txt` | URL as text | Plain text, one block per video |
| `csv` | URL as text | Records only, opens in Excel |
| `json` | URL as text | Metadata and records |
| `md` | Picture | Markdown table. Pictures load from the Eporner CDN when viewed |
| `html` | Picture | Styled page, follows light or dark mode. Pictures load from the Eporner CDN when viewed |
| `pdf` | Picture | Downloads the thumbnails first. Only JPEG and PNG up to 1 MB are embedded. Others show `-` |

Notes:

- With `-F all`, columns are in this order: title, thumbnail, views, URL. With `-F url`, only the URL column is written.
- Views are the count at the time of scraping, not a live value.

## Resume

Progress is saved only when you ask for it with `-s`. Without `-s`, the run keeps nothing on disk.

- `-s FILE` saves progress after every page. It refuses a file that already exists, unless that file is the same one given to `-R`.
- `-R FILE` continues from that file. If the file is missing, unreadable, or belongs to another profile, the run stops with an error.
- Use `-R FILE -s FILE` with the same file to keep saving progress while continuing.
- The output file cannot be the same file as the progress file. The run stops before scraping if they match.

Progress files are never deleted automatically. Remove them yourself when you no longer need them.

## Notes

- The scraper depends on the page layout. If the site changes, it may return no results. The patterns are at the top of `userporner.py`.
- Use a small number of workers, and avoid running several scrapes at once.
- Check the site's terms of service before scraping.

## Disclaimer

- This project is not affiliated with Eporner.
- The source site contains adult content. Use this tool only if you are 18 or older and the content is legal where you live.
- You are responsible for how you use the output.
- The software is provided as is, without warranty of any kind.

## License

[MIT](LICENSE)
