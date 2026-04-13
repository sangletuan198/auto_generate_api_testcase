#!/usr/bin/env python3
"""
pull_confluence.py — Pull API spec pages from Confluence → input/

Tự động kéo tài liệu API từ Confluence space về thư mục input/ dưới dạng HTML,
sẵn sàng cho parse_html_docs.py xử lý.

Usage:
    # Pull tất cả API pages trong space MYPROJECT:
    python3 scripts/pull_confluence.py

    # Pull theo CQL tùy chỉnh:
    python3 scripts/pull_confluence.py --cql 'space = "MYPROJECT" AND label = "api-spec"'

    # Pull 1 page cụ thể theo ID:
    python3 scripts/pull_confluence.py --page-id 123456

    # Pull theo title:
    python3 scripts/pull_confluence.py --title "getSavingAccountTransactions"

    # Xem danh sách pages sẽ pull (dry-run):
    python3 scripts/pull_confluence.py --dry-run

Config:  baseline/confluence_config.json  (URL, space, auth)
Output:  input/  (HTML files, ready for parse_html_docs.py)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
BASELINE_DIR = ROOT_DIR / "baseline"
INPUT_DIR = ROOT_DIR / "input"
CONFIG_PATH = BASELINE_DIR / "confluence_config.json"

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "confluence_url": "",
    "space_key": "",
    "username": "",
    "api_token": "",
    "personal_token": "",
    "auth_type": "token",
    "cql_filter": 'type = page AND title ~ "API"',
    "label_filter": "",
    "ancestor_page_id": "",
    "body_format": "storage",
    "filename_pattern": "view-source_{slug}.html",
    "max_pages": 200,
    "rate_limit_delay": 0.2,
    "skip_if_exists": False,
}

# ── Config ────────────────────────────────────────────────────────────────────


def load_config() -> dict:
    """Load confluence config, merging defaults with user overrides."""
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user_config = json.load(f)
        config.update({k: v for k, v in user_config.items() if v})
    # Env vars override (cho CI/CD hoặc security)
    import os
    env_map = {
        "CONFLUENCE_URL": "confluence_url",
        "CONFLUENCE_USERNAME": "username",
        "CONFLUENCE_API_TOKEN": "api_token",
        "CONFLUENCE_PERSONAL_TOKEN": "personal_token",
        "CONFLUENCE_SPACE_KEY": "space_key",
    }
    for env_key, config_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            config[config_key] = val
    return config


def validate_config(config: dict) -> list[str]:
    """Validate config, return list of errors."""
    errors = []
    if not config.get("confluence_url"):
        errors.append("confluence_url chưa set (baseline/confluence_config.json hoặc CONFLUENCE_URL env)")
    if not config.get("space_key") and not config.get("ancestor_page_id"):
        errors.append("Cần space_key hoặc ancestor_page_id")
    if config.get("auth_type") == "token":
        if not config.get("api_token") and not config.get("personal_token"):
            errors.append("Cần api_token hoặc personal_token")
        if config.get("api_token") and not config.get("username"):
            errors.append("api_token cần username (email)")
    return errors


# ── Confluence Client ─────────────────────────────────────────────────────────


def create_client(config: dict):
    """Create Confluence client from config."""
    try:
        from atlassian import Confluence
    except ImportError:
        print("❌ Chưa cài atlassian-python-api")
        print("   → pip install atlassian-python-api")
        sys.exit(1)

    url = config["confluence_url"].rstrip("/")

    if config.get("personal_token"):
        return Confluence(url=url, token=config["personal_token"])
    elif config.get("api_token"):
        return Confluence(
            url=url,
            username=config["username"],
            password=config["api_token"],
            cloud=("atlassian.net" in url),
        )
    else:
        print("❌ Không tìm thấy credentials")
        sys.exit(1)


# ── Page Discovery ────────────────────────────────────────────────────────────


def discover_pages(client, config: dict, args) -> list[dict]:
    """Discover pages to pull based on args and config."""
    pages = []

    if args.page_id:
        # Single page by ID
        page = client.get_page_by_id(
            args.page_id, expand="body.storage,version,space"
        )
        if page:
            pages.append(page)
        return pages

    if args.title:
        # Search by title
        page = client.get_page_by_title(
            config["space_key"], args.title, expand="body.storage,version"
        )
        if page:
            pages.append(page)
        return pages

    # CQL search
    cql = args.cql or _build_cql(config)
    limit = config.get("max_pages", 200)

    print(f"  CQL: {cql}")
    print(f"  Max pages: {limit}")

    start = 0
    batch_size = 50

    while start < limit:
        fetch = min(batch_size, limit - start)
        results = client.cql(cql, start=start, limit=fetch, expand="body.storage,version,space")
        batch = results.get("results", [])
        if not batch:
            break

        for r in batch:
            content = r.get("content", r)
            pages.append(content)

        print(f"  Fetched {len(pages)} pages...")
        start += len(batch)

        if len(batch) < fetch:
            break

        time.sleep(config.get("rate_limit_delay", 0.2))

    return pages


def _build_cql(config: dict) -> str:
    """Build CQL from config filters."""
    parts = ["type = page"]

    if config.get("space_key"):
        parts.append(f'space = "{config["space_key"]}"')

    if config.get("ancestor_page_id"):
        parts.append(f'ancestor = {config["ancestor_page_id"]}')

    if config.get("label_filter"):
        parts.append(f'label = "{config["label_filter"]}"')

    if config.get("cql_filter"):
        # Nếu user config có cql_filter riêng, dùng nó thay cho title filter
        parts.append(f'({config["cql_filter"]})')

    return " AND ".join(parts)


# ── Page → HTML File ──────────────────────────────────────────────────────────


def _slug_from_page(page: dict) -> str:
    """Extract a usable slug from page title or content."""
    title = page.get("title", "")

    # Thử lấy API name pattern: "API get-saving-account-transactions"
    m = re.search(r'API[+\s_-]+([a-z][a-z0-9-]+)', title, re.IGNORECASE)
    if m:
        return _kebab_to_camel(m.group(1))

    # Thử camelCase trực tiếp trong title
    m = re.search(r'\b([a-z][a-zA-Z0-9]{5,})\b', title)
    if m:
        return m.group(1)

    # Fallback: normalize title
    slug = re.sub(r'[^a-zA-Z0-9]+', '_', title).strip('_')[:60]
    return slug or "unknown_page"


def _kebab_to_camel(s: str) -> str:
    """Convert kebab-case to camelCase."""
    parts = re.split(r'[-_]+', s)
    return parts[0].lower() + ''.join(p.capitalize() for p in parts[1:])


def save_page_html(page: dict, config: dict) -> Optional[Path]:
    """Save a Confluence page as HTML in input/."""
    body = page.get("body", {}).get("storage", {}).get("value", "")
    if not body:
        return None

    slug = _slug_from_page(page)
    pattern = config.get("filename_pattern", "view-source_{slug}.html")
    filename = pattern.replace("{slug}", slug)
    filepath = INPUT_DIR / filename

    if config.get("skip_if_exists") and filepath.exists():
        return None

    # Wrap body với HTML wrapper cho parse_html_docs.py
    title = page.get("title", slug)
    page_id = page.get("id", "")
    version = page.get("version", {}).get("number", "")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<meta name="confluence-page-id" content="{page_id}">
<meta name="confluence-version" content="{version}">
</head>
<body>
{body}
</body>
</html>"""

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath.write_text(html, encoding="utf-8")
    return filepath


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Pull API spec pages from Confluence → input/"
    )
    parser.add_argument("--page-id", help="Pull single page by ID")
    parser.add_argument("--title", help="Pull single page by title")
    parser.add_argument("--cql", help="Custom CQL query")
    parser.add_argument("--dry-run", action="store_true", help="List pages without downloading")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = parser.parse_args()

    print("=" * 60)
    print("  Confluence → input/ (Pull API Spec Pages)")
    print("=" * 60)

    # Load & validate config
    config = load_config()
    if args.force:
        config["skip_if_exists"] = False

    errors = validate_config(config)
    if errors:
        print("\n❌ Config errors:")
        for e in errors:
            print(f"   • {e}")
        print(f"\n   Config file: {CONFIG_PATH}")
        if not CONFIG_PATH.exists():
            _create_template_config()
            print(f"   → Template created, edit the file and retry.")
        sys.exit(1)

    print(f"\n  URL:   {config['confluence_url']}")
    print(f"  Space: {config.get('space_key', '(from CQL)')}")

    # Connect
    client = create_client(config)

    # Discover pages
    print(f"\n{'─'*40}")
    print("  Discovering pages...")
    pages = discover_pages(client, config, args)

    if not pages:
        print("\n⚠️  Không tìm thấy page nào.")
        return

    print(f"\n  Found {len(pages)} page(s):")
    print(f"  {'#':<4} {'Title':<50} {'ID':<12} {'Slug'}")
    print(f"  {'─'*4} {'─'*50} {'─'*12} {'─'*30}")

    for i, p in enumerate(pages, 1):
        title = p.get("title", "?")[:50]
        pid = p.get("id", "?")
        slug = _slug_from_page(p)
        print(f"  {i:<4} {title:<50} {pid:<12} {slug}")

    if args.dry_run:
        print("\n  (dry-run — không download)")
        return

    # Save pages
    print(f"\n{'─'*40}")
    print("  Downloading...")
    saved = 0
    skipped = 0

    for p in pages:
        filepath = save_page_html(p, config)
        if filepath:
            print(f"  ✅ {filepath.name}")
            saved += 1
        else:
            slug = _slug_from_page(p)
            print(f"  ⏭️  {slug} (skipped — exists or empty)")
            skipped += 1

    print(f"\n{'='*60}")
    print(f"  Done: {saved} saved, {skipped} skipped")
    print(f"  Output: {INPUT_DIR}/")
    print(f"  Next:   python3 scripts/parse_html_docs.py  (hoặc /parse)")
    print(f"{'='*60}")


def _create_template_config():
    """Create template confluence_config.json."""
    template = {
        "_comment": "Confluence connection config — edit values, hoặc dùng env vars (CONFLUENCE_URL, etc.)",
        "confluence_url": "https://your-domain.atlassian.net/wiki",
        "space_key": "MYPROJECT",
        "username": "your.email@company.com",
        "api_token": "",
        "personal_token": "",
        "auth_type": "token",
        "cql_filter": 'title ~ "API"',
        "label_filter": "api-spec",
        "ancestor_page_id": "",
        "body_format": "storage",
        "filename_pattern": "view-source_{slug}.html",
        "max_pages": 200,
        "rate_limit_delay": 0.2,
        "skip_if_exists": False,
    }
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
