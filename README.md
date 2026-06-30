# Wikipedia Article Scraper

A Python command-line tool that scrapes the full text content of Wikipedia articles via the official MediaWiki API and saves each one to a structured `.txt` file.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Scrape a single article

```bash
python scrape_wikipedia.py --urls https://en.wikipedia.org/wiki/Artificial_intelligence
```

### Scrape multiple articles (pass URLs directly)

```bash
python scrape_wikipedia.py --urls \
    https://en.wikipedia.org/wiki/Artificial_intelligence \
    https://en.wikipedia.org/wiki/Python_(programming_language) \
    https://en.wikipedia.org/wiki/Machine_learning
```

### Scrape from a file of URLs

Create a `urls.txt` with one URL per line (blank lines and `#` comments are ignored):

```text
# English articles
https://en.wikipedia.org/wiki/Artificial_intelligence
https://en.wikipedia.org/wiki/Climate_change

# Non-English articles
https://es.wikipedia.org/wiki/Inteligencia_artificial
https://de.wikipedia.org/wiki/Künstliche_Intelligenz
```

Then run:

```bash
python scrape_wikipedia.py --file urls.txt
```

### All command-line options

| Flag | Default | Description |
|------|---------|-------------|
| `--urls URL [URL ...]` | — | One or more Wikipedia URLs to scrape |
| `--file FILE` | — | Path to a `.txt` file containing URLs |
| `--output-dir DIR` | `./scraped_data` | Directory for output `.txt` files |
| `--delay SECONDS` | `1.0` | Delay between API requests |

`--urls` and `--file` are mutually exclusive (use one or the other).

```bash
python scrape_wikipedia.py --help
```

## Output Format

Each article is saved as `<Sanitized_Title>.txt` with this structure:

```
TITLE: Artificial intelligence
SOURCE URL: https://en.wikipedia.org/wiki/Artificial_intelligence
LANGUAGE: en
SCRAPED: 2026-06-30T10:00:00+00:00
================================================================================

<Lead / introduction paragraphs>

================================================================================
SECTION: History
================================================================================

<section content>

--------------------------------------------------------------------------------
SUBSECTION: Early research
--------------------------------------------------------------------------------

<subsection content>

================================================================================
SECTION: Applications
================================================================================

<section content>
```

## Design Decisions & Trade-offs

### API Approach
Uses the `wikipedia-api` Python library (PyPI: `Wikipedia-API`) which wraps the MediaWiki `action=query&prop=extracts` endpoint. This gives clean plain text with native section hierarchy — no HTML parsing needed. The library handles redirects transparently and supports every Wikipedia language edition.

### Tables
The `prop=extracts` API endpoint flattens or drops most tables. This is an accepted trade-off: the user requirement explicitly states "reasonable best-effort" table conversion is fine. For projects needing rich table extraction, a hybrid approach using `action=parse&prop=text` with HTML-to-text table conversion would be needed.

### Section Nesting
The script supports arbitrary nesting depth. Levels 0, 1, and 2+ use visually distinct delimiters (`===`, `---`, `...`) with labels `SECTION`, `SUBSECTION`, `SUB-SUBSECTION`, `SUB-SUB-SUBSECTION`, etc. This makes the output both human-readable and machine-parsable.

### Disambiguation Detection
Disambiguation pages are detected by scanning the page's categories for known markers across ~10 languages. This heuristic works well but is not guaranteed to catch every disambiguation page in every language edition.

### Rate Limiting & Retries
The script sets a descriptive `User-Agent` header per Wikipedia's API etiquette, enforces a configurable delay between requests, and retries up to 2 times with exponential backoff for transient network errors and HTTP 429 responses.
