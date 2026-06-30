#!/usr/bin/env python3
"""
MediaWiki Article Scraper
=========================
A command-line tool that scrapes the full text content of MediaWiki articles
(any site using standard ``/wiki/<Title>`` article URLs) via the official
MediaWiki API and saves each article to a structured, parsable .txt file.

Supported URL format:
  https://<any-mediawiki-host>/wiki/<Page_Title>

Examples include Wikipedia language editions, Fandom wikis, and other
self-hosted MediaWiki installations. Non-wiki websites are rejected.

Extraction strategy:
  - Wikis with the TextExtracts extension: ``wikipedia-api`` library for
    clean plain text and native section hierarchy.
  - Other wikis: wikitext fetched via the revisions API, with basic markup
    stripping and section parsing.

Usage Examples:
  # Scrape a single article:
  python scrape_wikipedia.py --urls https://en.wikipedia.org/wiki/Artificial_intelligence

  # Scrape a Fandom wiki article:
  python scrape_wikipedia.py --urls https://marvel.fandom.com/wiki/Peter_Parker

  # Scrape from a file of URLs:
  python scrape_wikipedia.py --file urls.txt

  # Custom output directory and request delay:
  python scrape_wikipedia.py --file urls.txt --output-dir ./my_data --delay 2.0

Install dependencies:
  pip install -r requirements.txt
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import httpx
import wikipediaapi
from wikipediaapi.exceptions import (
    WikiConnectionError,
    WikiHttpError,
    WikiHttpTimeoutError,
    WikipediaException,
    WikiRateLimitError,
)


# ─── Constants ────────────────────────────────────────────────────────────────

USER_AGENT = "WikiDatasetScraper/1.0 (contact: your-email@example.com)"
DEFAULT_OUTPUT_DIR = "./scraped_data"
DEFAULT_DELAY = 1.0
DEFAULT_URLS_FILE = "sample_urls.txt"
MAX_RETRIES = 2
WIKI_PATH_PREFIX = "/wiki/"

SEPARATOR_MAJOR = "=" * 80
SEPARATOR_MINOR = "-" * 80
SEPARATOR_SUB   = "." * 80  # Used for nesting levels ≥ 2

# Patterns that appear in category names of disambiguation pages across
# many Wikipedia language editions.  Covers English, Spanish, Portuguese,
# German, French, Dutch, Polish, Russian, Hebrew, Japanese, and Chinese.
DISAMBIG_PATTERNS = [
    "disambig", "desambig", "begriffsklärung", "homonymie",
    "doorverwijspagina", "ujednoznacznienie", "значения",
    "פירושונים", "曖昧さ回避", "消歧义",
]

# Characters that are invalid in filenames on Windows / macOS / Linux.
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# MediaWiki namespaces that indicate non-article pages.
SPECIAL_NAMESPACES = [
    "Special:", "Talk:", "User:", "User_talk:", "Wikipedia:",
    "Wikipedia_talk:", "File:", "File_talk:", "MediaWiki:",
    "Template:", "Template_talk:", "Help:", "Help_talk:",
    "Category:", "Category_talk:", "Portal:", "Portal_talk:",
    "Draft:", "Draft_talk:", "Module:", "Module_talk:",
    "TimedText:", "TimedText_talk:", "Book:", "Book_talk:",
]


# ─── Argument Parsing ────────────────────────────────────────────────────────

def parse_args():
    """Parse and validate command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Scrape the full text content of MediaWiki articles via the "
            "MediaWiki API and save each one to a structured .txt file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap_dedent("""\
Examples
--------
  Scrape a single article:
    python scrape_wikipedia.py --urls https://en.wikipedia.org/wiki/Artificial_intelligence

  Scrape multiple articles:
    python scrape_wikipedia.py --urls \\
        https://en.wikipedia.org/wiki/Python_(programming_language) \\
        https://en.wikipedia.org/wiki/Machine_learning

  Scrape from a file of URLs (one per line, # comments and blanks ignored):
    python scrape_wikipedia.py --file urls.txt

  Custom output directory and inter-request delay:
    python scrape_wikipedia.py --file urls.txt --output-dir ./my_data --delay 2.0
        """),
    )

    input_group = parser.add_mutually_exclusive_group(required=False)
    input_group.add_argument(
        "--urls",
        nargs="+",
        metavar="URL",
        help="One or more MediaWiki article URLs to scrape (…/wiki/Page_Title).",
    )
    input_group.add_argument(
        "--file",
        metavar="FILE",
        help=(
            "Path to a .txt file containing wiki article URLs, one per line. "
            "Blank lines and lines starting with # are ignored. "
            f"If omitted (and --urls is not used), defaults to {DEFAULT_URLS_FILE} "
            "in the script directory when that file exists."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to save scraped .txt files (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=f"Delay in seconds between API requests (default: {DEFAULT_DELAY}).",
    )

    args = parser.parse_args()

    if not args.urls and not args.file:
        default_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            DEFAULT_URLS_FILE,
        )
        if os.path.isfile(default_file):
            args.file = default_file
        else:
            parser.error(
                "one of the arguments --urls --file is required "
                f"(default {DEFAULT_URLS_FILE} not found next to this script)"
            )

    return args


def textwrap_dedent(text):
    """Minimal dedent helper to avoid importing textwrap just for argparse."""
    import textwrap
    return textwrap.dedent(text)


# ─── URL Loading & Validation ────────────────────────────────────────────────

def is_valid_wiki_url(url):
    """
    Return True if *url* points to a MediaWiki article page.

    Accepts  https://<host>/wiki/<Title>
    Rejects  non-wiki sites, non-/wiki/ paths, and special-namespace pages.
    """
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.netloc:
            return False
        if not parsed.path.startswith(WIKI_PATH_PREFIX):
            return False
        title_part = unquote(parsed.path[len(WIKI_PATH_PREFIX):])
        for ns in SPECIAL_NAMESPACES:
            if title_part.startswith(ns):
                return False
        return bool(title_part)
    except Exception:
        return False


def load_urls(args):
    """
    Build a list of validated MediaWiki article URLs from CLI args or a file.

    Prints a warning for every skipped invalid URL.
    """
    urls = []

    if args.urls:
        for url in args.urls:
            url = url.strip()
            if is_valid_wiki_url(url):
                urls.append(url)
            else:
                print(f"  WARNING: Skipping invalid wiki URL: {url}")

    elif args.file:
        if not os.path.isfile(args.file):
            print(f"ERROR: File not found: {args.file}")
            sys.exit(1)
        with open(args.file, "r", encoding="utf-8") as fh:
            for line_num, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if not line.startswith(("http://", "https://")):
                    continue
                if is_valid_wiki_url(line):
                    urls.append(line)
                else:
                    print(
                        f"  WARNING: Skipping invalid URL on line {line_num}: "
                        f"{line}"
                    )

    return urls


def extract_url_info(url):
    """
    Extract site origin, article title, and site label from a wiki URL.

    >>> extract_url_info("https://en.wikipedia.org/wiki/Artificial_intelligence")
    ('https://en.wikipedia.org', 'Artificial_intelligence', 'en')
    """
    parsed = urlparse(url.strip())
    origin = f"{parsed.scheme}://{parsed.netloc}"
    title = unquote(parsed.path[len(WIKI_PATH_PREFIX):])
    if parsed.netloc.endswith(".wikipedia.org"):
        site_label = parsed.netloc.split(".")[0]
    else:
        site_label = parsed.netloc
    return origin, title, site_label


# ─── MediaWiki API Discovery & Client ────────────────────────────────────────

class CustomWikiClient(wikipediaapi.Wikipedia):
    """Wikipedia-API client pointed at an arbitrary MediaWiki API endpoint."""

    def __init__(self, user_agent, api_url, language="en", **kwargs):
        self._custom_api_url = api_url
        super().__init__(user_agent=user_agent, language=language, **kwargs)

    def _build_url(self, language):
        return self._custom_api_url


def discover_wiki_info(origin):
    """
    Probe *origin* for a MediaWiki API and return site metadata.

    Returns a dict with api_url, has_extracts, language, site_name — or None.
    """
    headers = {"User-Agent": USER_AGENT}
    candidates = (f"{origin}/w/api.php", f"{origin}/api.php")

    for api_url in candidates:
        try:
            response = httpx.get(
                api_url,
                params={
                    "action": "query",
                    "meta": "siteinfo",
                    "siprop": "general|extensions",
                    "format": "json",
                },
                headers=headers,
                timeout=15.0,
            )
            if response.status_code != 200:
                continue
            payload = response.json()
            if "query" not in payload:
                continue

            query = payload["query"]
            extensions = {ext["name"] for ext in query.get("extensions", [])}
            general = query.get("general", {})
            return {
                "api_url": api_url,
                "has_extracts": "TextExtracts" in extensions,
                "language": general.get("lang", "en"),
                "site_name": general.get("sitename", origin),
            }
        except Exception:
            continue

    return None


def get_wiki_info(origin, cache):
    """Return cached site metadata, discovering it on first use."""
    if origin not in cache:
        cache[origin] = discover_wiki_info(origin)
    return cache[origin]


def get_wiki_client(wiki_info, client_cache):
    """Return a CustomWikiClient for the given site metadata."""
    api_url = wiki_info["api_url"]
    if api_url not in client_cache:
        client_cache[api_url] = CustomWikiClient(
            user_agent=USER_AGENT,
            api_url=api_url,
            language=wiki_info["language"],
            extract_format=wikipediaapi.ExtractFormat.WIKI,
        )
    return client_cache[api_url]


# ─── Disambiguation Detection ────────────────────────────────────────────────

def is_disambiguation(page):
    """
    Heuristically detect disambiguation pages by scanning the page's
    categories for known disambiguation markers across multiple languages.
    """
    try:
        for cat_title in page.categories:
            cat_lower = cat_title.lower()
            for pattern in DISAMBIG_PATTERNS:
                if pattern in cat_lower:
                    return True
        return False
    except Exception:
        return False


# ─── Text Cleaning ───────────────────────────────────────────────────────────

def clean_text(text):
    """
    Strip citation artifacts and residual markup from extracted article text.

    Removes:
      - Numbered citation brackets  [1], [2], [123]
      - Foot-note / note refs       [note 1], [nb 1], [a], [b]
      - Inline tags                 [citation needed], [when?], [who?] …
      - Edit markers                [edit]
      - Excessive consecutive blank lines
      - Trailing whitespace per line
    """
    if not text:
        return ""

    # Numbered citation brackets
    text = re.sub(r"\[\d+\]", "", text)

    # Note / footnote references
    text = re.sub(r"\[note\s+\d+\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[nb\s+\d+\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[[a-z]\]", "", text)

    # Inline verification / clarification tags
    text = re.sub(
        r"\[(?:citation\s+needed|when|who|where|which|why|how|"
        r"clarification needed|failed verification|dubious|"
        r"better source needed|unreliable source)\??\]",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Edit-section markers
    text = re.sub(r"\[edit\]", "", text, flags=re.IGNORECASE)

    # Collapse runs of ≥3 newlines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Strip trailing whitespace per line
    text = "\n".join(line.rstrip() for line in text.split("\n"))

    return text.strip()


# ─── Wikitext Fallback (wikis without TextExtracts) ─────────────────────────

def strip_wikitext(text):
    """Best-effort conversion of wikitext markup to plain text."""
    if not text:
        return ""

    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<ref[^>]*/>", "", text, flags=re.IGNORECASE)
    for _ in range(5):
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:[^|\]]+\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://[^\s\]]+\s*([^\]]*)\]", r"\1", text)
    text = re.sub(r"\[https?://[^\]]+\]", "", text)
    text = re.sub(r"'''|''", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"^=+\s*.+?\s*=+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[\[Category:[^\]]+\]\]", "", text, flags=re.IGNORECASE)
    return clean_text(text)


def parse_wikitext_sections(wikitext):
    """
    Split wikitext into a lead summary and a nested section tree.

    MediaWiki headings (``== Title ==``) map to output levels starting at 0.
    """
    lines = wikitext.split("\n")
    lead_lines = []
    root_sections = []
    stack = []
    in_lead = True

    for line in lines:
        match = re.match(r"^(=+)(.+?)\1\s*$", line)
        if match:
            in_lead = False
            wiki_level = len(match.group(1))
            title = match.group(2).strip()
            output_level = max(0, wiki_level - 2)
            section = {
                "title": title,
                "text": "",
                "subsections": [],
                "_level": output_level,
            }

            while stack and stack[-1]["_level"] >= output_level:
                stack.pop()

            if stack:
                stack[-1]["subsections"].append(section)
            else:
                root_sections.append(section)
            stack.append(section)
        elif in_lead:
            lead_lines.append(line)
        elif stack:
            stack[-1]["text"] += line + "\n"

    def finalize(sections):
        for section in sections:
            section["text"] = strip_wikitext(section["text"])
            finalize(section["subsections"])
            section.pop("_level", None)

    finalize(root_sections)
    summary = strip_wikitext("\n".join(lead_lines))
    return summary, root_sections


def mediawiki_query(api_url, params, retries=MAX_RETRIES):
    """Run a MediaWiki API query with retry/backoff."""
    headers = {"User-Agent": USER_AGENT}
    merged = {"format": "json", "formatversion": "2", **params}
    last_error = None

    for attempt in range(retries + 1):
        try:
            response = httpx.get(
                api_url,
                params=merged,
                headers=headers,
                timeout=30.0,
            )
            if response.status_code == 429:
                wait = 5 * (2 ** attempt)
                if attempt < retries:
                    time.sleep(wait)
                    continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * (2 ** attempt))

    raise RuntimeError(f"MediaWiki API request failed: {last_error}")


def fetch_via_wikitext(url, wiki_info, site_label):
    """Fetch an article from a wiki that lacks the TextExtracts extension."""
    _, title, _ = extract_url_info(url)
    api_url = wiki_info["api_url"]

    payload = mediawiki_query(
        api_url,
        {
            "action": "query",
            "titles": title,
            "redirects": 1,
            "prop": "categories|revisions",
            "rvslots": "main",
            "rvprop": "content",
            "cllimit": "max",
        },
    )

    pages = payload.get("query", {}).get("pages", [])
    if not pages:
        return None, f"Page does not exist: '{title}'"

    page = pages[0]
    if page.get("missing"):
        return None, f"Page does not exist: '{title}'"

    canonical_title = page.get("title", title)
    requested_normalised = title.replace("_", " ")
    was_redirect = canonical_title.lower() != requested_normalised.lower()

    if was_redirect:
        print(f"\n  -> Redirect detected: '{title}' -> '{canonical_title}'")

    categories = [cat.get("title", "") for cat in page.get("categories", [])]
    if any(
        any(pattern in cat.lower() for pattern in DISAMBIG_PATTERNS)
        for cat in categories
    ):
        return None, f"[DISAMBIGUATION] '{canonical_title}'"

    revisions = page.get("revisions", [])
    if not revisions:
        return None, f"No content available for '{canonical_title}'"

    wikitext = revisions[0].get("slots", {}).get("main", {}).get("content", "")
    summary, sections = parse_wikitext_sections(wikitext)
    char_count = len(wikitext)

    return (
        {
            "title": canonical_title,
            "summary": summary,
            "sections": sections,
            "language": site_label,
            "was_redirect": was_redirect,
            "original_title": title,
            "char_count": char_count,
        },
        None,
    )


# ─── Section Extraction & Formatting ─────────────────────────────────────────

def extract_sections_data(wiki_sections):
    """
    Recursively convert *WikipediaPageSection* objects into plain dicts.

    Must be called while the page data is still in scope (property access
    on the wiki objects may trigger lazy API calls).
    """
    result = []
    for s in wiki_sections:
        result.append(
            {
                "title": s.title,
                "text": s.text,
                "subsections": (
                    extract_sections_data(s.sections) if s.sections else []
                ),
            }
        )
    return result


def format_sections(sections, level=0):
    """
    Recursively render sections into the structured plain-text format.

    Delimiter hierarchy
    -------------------
      Level 0  →  ====…  SECTION: Title       ====…
      Level 1  →  ----…  SUBSECTION: Title     ----…
      Level 2+ →  ....…  SUB-SUBSECTION: Title ....…

    Sections whose cleaned text is empty *and* that have no subsections
    are silently omitted.
    """
    parts = []

    for section in sections:
        section_text = clean_text(section["text"])
        has_subs = bool(section.get("subsections"))

        # Skip truly empty sections (no own text, no children)
        if not section_text and not has_subs:
            continue

        # --- header ---
        if level == 0:
            parts.append("")
            parts.append(SEPARATOR_MAJOR)
            parts.append(f"SECTION: {section['title']}")
            parts.append(SEPARATOR_MAJOR)
        elif level == 1:
            parts.append("")
            parts.append(SEPARATOR_MINOR)
            parts.append(f"SUBSECTION: {section['title']}")
            parts.append(SEPARATOR_MINOR)
        else:
            # Levels 2, 3, … get a "SUB-" prefix chain
            label = "SUB-" * (level - 1) + "SUBSECTION"
            parts.append("")
            parts.append(SEPARATOR_SUB)
            parts.append(f"{label}: {section['title']}")
            parts.append(SEPARATOR_SUB)

        if section_text:
            parts.append("")
            parts.append(section_text)

        # Recurse into subsections
        if has_subs:
            sub_text = format_sections(section["subsections"], level + 1)
            if sub_text:
                parts.append(sub_text)

    return "\n".join(parts)


# ─── Article Fetching (with retry logic) ─────────────────────────────────────

def fetch_via_extracts_api(url, wiki_info, wiki_client_cache, site_label):
    """
    Fetch an article via the *wikipedia-api* library (TextExtracts wikis).

    Implements up to *MAX_RETRIES* retries with exponential back-off for
    transient network errors and HTTP 429 rate-limit responses.

    Returns
    -------
    (article_dict | None, error_string | None)
    """
    _, title, _ = extract_url_info(url)
    wiki = get_wiki_client(wiki_info, wiki_client_cache)

    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            page = wiki.page(title)

            if not page.exists():
                return None, f"Page does not exist: '{title}'"

            canonical_title = page.title
            requested_normalised = title.replace("_", " ")
            was_redirect = canonical_title.lower() != requested_normalised.lower()

            if was_redirect:
                print(
                    f"\n  -> Redirect detected: '{title}' -> '{canonical_title}'"
                )

            if is_disambiguation(page):
                return None, f"[DISAMBIGUATION] '{canonical_title}'"

            summary = page.summary
            sections_data = extract_sections_data(page.sections)
            full_text = page.text
            char_count = len(full_text) if full_text else 0

            return (
                {
                    "title": canonical_title,
                    "summary": summary,
                    "sections": sections_data,
                    "language": site_label,
                    "was_redirect": was_redirect,
                    "original_title": title,
                    "char_count": char_count,
                },
                None,
            )

        except WikiRateLimitError as exc:
            last_error = exc
            wait = 5 * (2 ** attempt)
            if attempt < MAX_RETRIES:
                print(
                    f"\n  Rate-limited. Retry {attempt + 1}/{MAX_RETRIES} "
                    f"in {wait}s"
                )
                time.sleep(wait)

        except WikiHttpError as exc:
            last_error = exc
            wait = 2 * (2 ** attempt)
            if attempt < MAX_RETRIES:
                print(
                    f"\n  HTTP error. Retry {attempt + 1}/{MAX_RETRIES} "
                    f"in {wait}s: {exc}"
                )
                time.sleep(wait)

        except (WikiConnectionError, WikiHttpTimeoutError) as exc:
            last_error = exc
            wait = 2 * (2 ** attempt)
            if attempt < MAX_RETRIES:
                print(
                    f"\n  Network error — retry {attempt + 1}/{MAX_RETRIES} "
                    f"in {wait}s: {exc}"
                )
                time.sleep(wait)

        except (WikipediaException, Exception) as exc:  # noqa: BLE001
            last_error = exc
            wait = 2 * (2 ** attempt)
            if attempt < MAX_RETRIES:
                print(
                    f"\n  Error — retry {attempt + 1}/{MAX_RETRIES} "
                    f"in {wait}s: {exc}"
                )
                time.sleep(wait)

    return None, f"Failed after {MAX_RETRIES + 1} attempts: {last_error}"


def fetch_article(url, wiki_info_cache, wiki_client_cache):
    """
    Fetch a MediaWiki article, choosing the best API strategy per site.

    Returns (article_dict | None, error_string | None).
    """
    origin, title, site_label = extract_url_info(url)
    wiki_info = get_wiki_info(origin, wiki_info_cache)

    if not wiki_info:
        return None, f"Could not discover MediaWiki API for '{origin}'"

    if wiki_info["has_extracts"]:
        return fetch_via_extracts_api(
            url, wiki_info, wiki_client_cache, site_label
        )

    try:
        return fetch_via_wikitext(url, wiki_info, site_label)
    except RuntimeError as exc:
        return None, str(exc)


# ─── Output Formatting ───────────────────────────────────────────────────────

def format_output(article, url):
    """
    Compose the full structured plain-text output for a single article.

    Layout
    ------
      Metadata header  →  TITLE / SOURCE URL / LANGUAGE / SCRAPED
      ════════════════
      Lead paragraphs
      ════════════════  SECTION: …
      Section body
      ────────────────  SUBSECTION: …
      …
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    lines = [
        f"TITLE: {article['title']}",
        f"SOURCE URL: {url}",
        f"LANGUAGE: {article['language']}",
        f"SCRAPED: {timestamp}",
        SEPARATOR_MAJOR,
    ]

    # Lead / introduction (text before the first section heading)
    intro = clean_text(article["summary"])
    if intro:
        lines.append("")
        lines.append(intro)

    # Hierarchical sections
    body = format_sections(article["sections"])
    if body:
        lines.append(body)

    lines.append("")  # trailing newline
    return "\n".join(lines)


# ─── File Operations ─────────────────────────────────────────────────────────

def sanitize_filename(title):
    """
    Convert an article title into a cross-platform-safe filename.

    Replaces spaces → underscores, strips invalid chars, truncates to 250
    characters (leaving room for the .txt extension).
    """
    name = title.replace(" ", "_")
    name = INVALID_FILENAME_RE.sub("_", name)
    name = name.strip("._")
    if not name:
        name = "untitled"
    return name[:250] + ".txt"


def save_to_file(content, article_title, output_dir):
    """
    Write *content* to ``<output_dir>/<sanitized_title>.txt``.

    Creates *output_dir* if it does not exist.
    Returns the absolute path of the written file.
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = sanitize_filename(article_title)
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(content)

    return filepath


def log_failure(url, reason, output_dir):
    """Append a timestamped failure record to ``failed_urls.log``."""
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, "failed_urls.log")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] {url} -- {reason}\n")


# ─── Main Entry Point ────────────────────────────────────────────────────────

def main():
    """
    Parse arguments → load URLs → scrape each article → save files → summary.
    """
    args = parse_args()

    print()
    print("=" * 60)
    print("  MediaWiki Article Scraper")
    print("=" * 60)
    print()

    # --- load & validate URLs ---
    print("Loading URLs...")
    if args.file and not args.urls:
        print(f"  Using URL file: {os.path.abspath(args.file)}")
    urls = load_urls(args)

    if not urls:
        print("ERROR: No valid wiki article URLs to process.")
        sys.exit(1)

    output_dir = os.path.abspath(args.output_dir)
    print(f"  {len(urls)} valid URL(s) to scrape")
    print(f"  Output directory : {output_dir}")
    print(f"  Request delay    : {args.delay}s")
    print()

    # --- initialise state ---
    wiki_info_cache = {}   # origin → site metadata dict
    wiki_client_cache = {}  # api_url → CustomWikiClient
    stats = {
        "total": len(urls),
        "successful": 0,
        "failed": 0,
        "redirected": 0,
        "disambig_skipped": 0,
        "total_chars": 0,
        "total_bytes": 0,
    }

    # --- process each URL ---
    for idx, url in enumerate(urls, 1):
        _, display_title, _ = extract_url_info(url)
        print(
            f"[{idx}/{len(urls)}] Scraping: {display_title} ... ",
            end="",
            flush=True,
        )

        article, error = fetch_article(url, wiki_info_cache, wiki_client_cache)

        if error:
            if "[DISAMBIGUATION]" in error:
                stats["disambig_skipped"] += 1
                print(f"SKIPPED (disambiguation page)")
            else:
                stats["failed"] += 1
                print(f"FAILED — {error}")
                log_failure(url, error, output_dir)

            # Polite delay even after failures
            if idx < len(urls):
                time.sleep(args.delay)
            continue

        if article["was_redirect"]:
            stats["redirected"] += 1

        # Format and save
        content = format_output(article, url)
        filepath = save_to_file(content, article["title"], output_dir)
        file_size = os.path.getsize(filepath)

        stats["successful"] += 1
        stats["total_chars"] += article["char_count"]
        stats["total_bytes"] += file_size

        print(
            f"done ({article['char_count']:,} chars, "
            f"saved as {sanitize_filename(article['title'])})"
        )

        # Polite delay between requests
        if idx < len(urls):
            time.sleep(args.delay)

    # ─── Summary ──────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  Scraping Summary")
    print("=" * 60)
    print(f"  Total URLs processed     : {stats['total']}")
    print(f"  Successful               : {stats['successful']}")
    print(f"  Failed                   : {stats['failed']}")
    print(f"  Disambiguation skipped   : {stats['disambig_skipped']}")
    print(f"  Redirects followed       : {stats['redirected']}")
    print(f"  Total content            : {stats['total_chars']:,} characters")
    print(f"  Total output size        : {stats['total_bytes']:,} bytes "
          f"({stats['total_bytes'] / 1024:.1f} KB)")
    print(f"  Output directory         : {output_dir}")
    if stats["failed"] > 0:
        log_path = os.path.join(output_dir, "failed_urls.log")
        print(f"  Failed URLs log          : {os.path.abspath(log_path)}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
