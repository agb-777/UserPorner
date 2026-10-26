# UserPorner

![License](https://img.shields.io/badge/license-MIT-brightgreen.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-purple.svg)

<img src="https://github.com/agb-777/userporner/blob/main/img/icon.png" width="600" />

Export the video list of an Eporner profile to CSV, JSON, Markdown, or PDF.

## Table of Contents

- [Install](#install)
- [Usage](#usage)
- [Options](#options)
- [Examples](#examples)
- [Output](#output)
- [Resume](#resume)
- [Notes](#notes)
- [Screenshots](#screenshots)
- [License](#license)

## Install

Core dependency:

```bash
pip install colorama
```

Optional, only for PDF output:

```bash
pip install reportlab
```

No browser is needed. Pages are read over plain HTTP.

Requires Python 3.9 or newer.

## Usage

The `-u` option accepts a full profile URL or just the username:

```bash
python userporner.py -u https://www.eporner.com/profile/USERNAME/ -o result -f md
python userporner.py -u USERNAME -o result -f md
```

Interactive mode asks each setting step by step:

```bash
python userporner.py -i
```

Nothing is saved unless `-o` is given.

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

## Examples

Save URLs only, as JSON:

```bash
python userporner.py -u USERNAME -o urls -f json -F url
```

Save titles and URLs as PDF, with 2 parallel requests:

```bash
python userporner.py -u USERNAME -o videos -f pdf -w 2
```

Keep the checkpoint after a complete run, so a later run can reuse it:

```bash
python userporner.py -u USERNAME -o videos -k
```

## Output

| Format | Contents |
|---|---|
| `csv` | Records only, UTF-8 with BOM so Excel shows non-Latin text correctly |
| `json` | Author, homepage, source URL, total count, timestamp, fields, and records |
| `md` | Numbered Markdown table with a header block and clickable URLs |
| `pdf` | Landscape A4 table with a repeated header row, author line, and page numbers |

The PDF uses a standard font, so titles in non-Latin scripts may not render.

## Resume

Each finished page is saved to a checkpoint file while the run is going. If a run stops, run the same command again to continue.

Each profile has its own checkpoint file, so two profiles never overwrite each other. The checkpoint is removed only when the run is complete and the results were saved with `-o`, unless `-k` is used. Without `-o`, the checkpoint is kept so the data is not lost.

A checkpoint that is already complete is reused as it is. Videos added to the profile after that point are not fetched until you run with `-n`.

If the site no longer matches a checkpoint, the run stops and asks you to use `-n`.

## Notes

- The scraper reads the page layout: the `/video-` link pattern, the `/uploaded-videos/` listing marker, and the `mb` container class. If the site changes its layout, scraping may return no results. The patterns are at the top of `userporner.py`, and the `mb` class is in `_VideoParser`.
- Keep the request count low. Use a small number of workers and avoid running several scrapes at the same time.
- Check the site's terms of service before scraping.

## Screenshots

![Screenshot of the CLI run](https://github.com/agb-777/userporner/blob/main/img/sikrinsot1.png)

*Figure 1. Running the scraper from the command line.*

![Screenshot of the output](https://github.com/agb-777/userporner/blob/main/img/sikrinsot2.png)

*Figure 2. Result summary after a completed run.*

## License

[MIT](https://github.com/agb-777/userporner/blob/main/LICENSE)
