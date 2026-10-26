# UserPorner

![License](https://img.shields.io/badge/license-MIT-brightgreen.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-purple.svg)

<img src="https://github.com/agb-777/UserPorner/blob/main/img/icon.png" width="600" />

Scrape the video list of an Eporner profile into CSV, JSON, Markdown, or PDF.

## Table of Contents

- [Install](#install)
- [Usage](#usage)
- [Options](#options)
- [Examples](#examples)
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

## Usage

Save results to a file:

```bash
# Nothing is saved unless -o is given
python userporner.py -u https://www.eporner.com/profile/USERNAME/ -o result -f md
```

Interactive mode:

```bash
python userporner.py -i
```

## Options

| Short | Long | Default | Description |
|---|---|---|---|
| `-u` | `--url` | required | Eporner profile URL |
| `-o` | `--output` | none | Output file path |
| `-f` | `--format` | `csv` | `csv`, `json`, `md`, or `pdf` |
| `-F` | `--fields` | `all` | `all` (title and URL) or `url` (URL only) |
| `-w` | `--workers` | `3` | Parallel requests, 1 to 10 |
| `-r` | `--retries` | `3` | Retries per page, 1 to 10 |
| `-c` | `--checkpoint` | `.scraper_checkpoint.json` | Checkpoint file path |
| `-n` | `--no-resume` | off | Ignore and delete any existing checkpoint |
| `-k` | `--keep-checkpoint` | off | Keep the checkpoint after a complete run |
| `-v` | `--verbose` | off | Show the time taken for each batch of pages |
| `-i` | `--interactive` | off | Ask all settings step by step |
| `-h` | `--help` | | Show help |

Options other than `-i` and `-h` cannot be combined with `-i`.

## Examples

URLs only, as JSON:

```bash
python userporner.py -u https://www.eporner.com/profile/USERNAME/ -o urls -f json -F url
```

Title and URL as PDF, with 5 workers:

```bash
python userporner.py -u https://www.eporner.com/profile/USERNAME/ -o videos -f pdf -w 5
```

## Resume

Each finished page is saved to the checkpoint file. If a run stops, run the same command again to continue. The checkpoint belongs to one profile URL, so a different profile starts a new run.

## Screenshots

![Screenshot of the CLI run](https://github.com/agb-777/userporner/blob/main/img/sikrinsot1.png)

*Figure 1. Running the scraper from the command line.*

![Screenshot of the output](https://github.com/agb-777/userporner/blob/main/img/sikrinsot2.png)

*Figure 2. Result summary after a completed run.*

## License

[MIT](https://github.com/agb-777/userporner/blob/main/LICENSE)
