#!/usr/bin/env python3
"""
Extract API contracts from doc files in input/.
Supports:  .html (Chrome view-source),  .docx (Word),  .doc (legacy Word)
Identifies struck-through fields, compares with sampler, saves clean contracts.
"""
import json
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup, Tag

# Ensure scripts/ is on sys.path for sibling imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
from soap_body_utils import detect_soap_body, soap_body_to_flat_dict, extract_soap_operation

ROOT    = Path(__file__).resolve().parent.parent
DOCS    = ROOT / 'input'


def _find_samplers() -> list:
    """Auto-detect ALL .postman_collection.json files in postman/.
    Returns list sorted with 'New Collection' first (user-calibrated priority),
    then other files alphabetically."""
    postman_dir = ROOT / 'postman'
    samplers = []
    if postman_dir.is_dir():
        for p in sorted(postman_dir.iterdir()):
            if p.name.endswith('.postman_collection.json'):
                samplers.append(p)
    # Prioritise 'New Collection' (user-calibrated) by putting it first
    samplers.sort(key=lambda p: (0 if 'New Collection' in p.name else 1, p.name))
    return samplers


SAMPLERS = _find_samplers()
# Backward compat: SAMPLER points to first file (or dummy)
SAMPLER = SAMPLERS[0] if SAMPLERS else (ROOT / 'postman' / 'sampler_NOT_FOUND.json')

# Try to import docx parser (optional — only needed for .docx/.doc files)
try:
    from parse_docx import parse_docx_file, slug_from_docx, slug_from_doc, convert_doc_to_html, HAS_DOCX
except ImportError:
    HAS_DOCX = False

# Slug mapping for existing URL-style filenames (backward compatibility).
_KNOWN_SLUG_MAP: dict = {}
# HTML_FILES populated after decode/helper functions are defined (see below)

# ── Load baseline configs ────────────────────────────────────────────────────
BASELINE = ROOT / 'baseline'

def _load_json(path: Path, fallback=None):
    """Load JSON file or return fallback if missing."""
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return fallback if fallback is not None else {}

# sampler URL → slug overrides (strip internal comments)
_raw_url_overrides = _load_json(BASELINE / 'sampler_url_overrides.json', {})
SAMPLER_URL_TO_SLUG: dict = {k: v for k, v in _raw_url_overrides.items() if not k.startswith('_')}
# SOAP operation name → slug overrides (when SOAP op differs from doc slug)
SOAP_OP_TO_SLUG: dict = _raw_url_overrides.get('_soap_op_overrides', {})

# manual prerequisites per API slug
# Keys are auto-normalised to camelCase so users can write either
# "create-resource" or "createResource" — both will match.
_raw_manual_prereqs = _load_json(BASELINE / 'manual_prerequisites.json', {})
MANUAL_PREREQUISITES: dict = {}
def _kebab_to_camel(s: str) -> str:
    """Inline normaliser: 'create-resource' → 'createResource'."""
    s = s.strip('/ ').strip()
    parts = re.split(r'[-_]+', s)
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])
for _k, _v in _raw_manual_prereqs.items():
    if _k.startswith('_') or not isinstance(_v, list):
        continue
    _norm_key = _kebab_to_camel(_k)
    if _norm_key in MANUAL_PREREQUISITES:
        # merge: keep the longer list (avoid silent data loss)
        if len(_v) > len(MANUAL_PREREQUISITES[_norm_key]):
            MANUAL_PREREQUISITES[_norm_key] = _v
    else:
        MANUAL_PREREQUISITES[_norm_key] = _v

# table detection config
_TABLE_CFG = _load_json(BASELINE / 'table_detection.json', {})
_REQ_TABLE_KW_GROUPS  = _TABLE_CFG.get('request_table_keyword_groups', [])
_ERR_TABLE_KW_GROUPS  = _TABLE_CFG.get('error_table_keyword_groups', [])
_ERROR_CODE_PATTERN   = _TABLE_CFG.get('error_code_pattern', r'^[A-Z]{1,5}\.')
_ERR_COL0_IS_SEQ      = _TABLE_CFG.get('error_table_col0_is_sequence', True)
_REQ_COL_ALIASES      = _TABLE_CFG.get('request_table_column_aliases', {})
_ERR_COL_ALIASES      = _TABLE_CFG.get('error_table_column_aliases', {})
_PROSE_KEYWORDS       = _TABLE_CFG.get('prose_filter_keywords', [])

# Multi-scenario SOAP config (one doc → multiple test suites)
_raw_multi_scenario = _load_json(BASELINE / 'multi_scenario_soap.json', {})
MULTI_SCENARIO_SOAP: dict = {k: v for k, v in _raw_multi_scenario.items() if not k.startswith('_')}
# Build reverse mapping: virtual_slug → (base_slug, scenario_cfg)
_VIRTUAL_SLUG_MAP: dict = {}  # virtual_slug → {'base_slug': str, 'scenario': dict}
# Build reverse mapping: sampler_request_name → virtual_slug
_SAMPLER_NAME_TO_VIRTUAL: dict = {}
for _base_slug, _ms_cfg in MULTI_SCENARIO_SOAP.items():
    for _vslug, _scen in _ms_cfg.get('scenarios', {}).items():
        _VIRTUAL_SLUG_MAP[_vslug] = {'base_slug': _base_slug, 'scenario': _scen}
        _req_name = _scen.get('sampler_request_name', '')
        if _req_name:
            _SAMPLER_NAME_TO_VIRTUAL[_req_name] = _vslug


# ---------------------------------------------------------------------------
# 1. Decode Chrome view-source page → real HTML
# ---------------------------------------------------------------------------

def decode_viewsource(path: Path) -> str:
    """
    Chrome view-source pages put the actual source into
    <td class="line-content"> cells as HTML-entity-encoded text.
    Reconstruct the original HTML by joining those cells.

    If no view-source structure is detected (e.g. plain HTML from textutil
    conversion), returns the raw file content instead.
    """
    with open(path, encoding='utf-8', errors='replace') as f:
        raw = f.read()
    vs = BeautifulSoup(raw, 'lxml')
    tds = vs.find_all('td', class_='line-content')
    if not tds:
        # Not a Chrome view-source page — return raw HTML as-is
        return raw
    lines = []
    for td in tds:
        # decode_contents() gives the inner HTML, which still has entities;
        # BeautifulSoup will convert those entities back to real chars
        inner_soup = BeautifulSoup(td.decode_contents(), 'lxml')
        lines.append(inner_soup.get_text())
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 1b. HTML decode cache + slug helpers + dynamic HTML_FILES discovery
# ---------------------------------------------------------------------------

_html_cache: dict = {}  # path → decoded HTML string

def _cached_decode(path: Path) -> str:
    """Return decoded HTML for path, caching to avoid double-decode."""
    if path not in _html_cache:
        _html_cache[path] = decode_viewsource(path)
    return _html_cache[path]


def _kebab_to_slug(s: str) -> str:
    """Convert /get-account-transactions → getAccountTransactions."""
    s = s.strip('/ ').strip()
    parts = re.split(r'[-_]+', s)
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def extract_api_name_from_html(soup: BeautifulSoup) -> str:
    """Return '/get-xxx' from the 'API Name' row in the Confluence overview table."""
    for td in soup.find_all(['td', 'th']):
        if td.get_text(' ', strip=True).strip().lower() == 'api name':
            nxt = td.find_next_sibling(['td', 'th'])
            if nxt:
                return nxt.get_text(strip=True)
    return ''


def _discover_html_files() -> dict:
    """
    Auto-discover all doc files in input/ and assign slugs.
    Supports: view-source_*.html  AND  *.docx  AND  *.doc

    Priority per HTML file:
      1. Known URL-style filename  → fixed slug (backward compat)
      2. view-source_<Slug>.html   → use <Slug> directly
      3. Parse 'API Name' row from HTML overview table  → kebab-to-camelCase
      4. Normalized filename stem  (last resort)

    For .docx files:
      1. Parse 'API Name' from Word tables → kebab-to-camelCase
      2. Filename stem (last resort)

    For .doc files:
      1. Auto-convert to .docx, then same as .docx
      2. Filename stem (last resort)
    """
    result = {}

    # ── HTML files ───────────────────────────────────────────────────────
    for html_path in sorted(DOCS.glob('view-source_*.html')):
        fname = html_path.name
        # 1. Known mapping
        if fname in _KNOWN_SLUG_MAP:
            result[_KNOWN_SLUG_MAP[fname]] = html_path
            continue
        # 2. Simple filename convention: view-source_<slug>.html
        stem = fname.removeprefix('view-source_').removesuffix('.html')
        if re.match(r'^[A-Za-z][A-Za-z0-9]+$', stem):
            result[stem] = html_path
            continue
        # 2.5. Filename contains _API_<endpoint-path> pattern
        #      e.g. view-source_US-1.45.7.7.1_API_get-account-statistic-monthly.html
        api_match = re.search(r'_API_([a-z0-9][a-z0-9\-]+)$', stem, re.IGNORECASE)
        if api_match:
            slug = _kebab_to_slug(api_match.group(1))
            if slug not in result:
                result[slug] = html_path
            else:
                print(f'⚠️  Slug collision "{slug}" for {fname}, skipping')
            continue
        # 2.6. Confluence URL filename with +API+<endpoint> pattern
        #      e.g. ...title=US+1.45.7.6.3+API+get-account-transactions.html
        api_plus_match = re.search(r'\+API\+([a-z0-9][a-z0-9\-]+)$', stem, re.IGNORECASE)
        if api_plus_match:
            slug = _kebab_to_slug(api_plus_match.group(1))
            if slug not in result:
                result[slug] = html_path
            else:
                print(f'⚠️  Slug collision "{slug}" for {fname}, skipping')
            continue
        # 3. Parse API Name row from HTML
        real_html = _cached_decode(html_path)
        soup_tmp  = BeautifulSoup(real_html, 'lxml')
        api_name  = extract_api_name_from_html(soup_tmp)
        if api_name:
            slug = _kebab_to_slug(api_name)
            if slug not in result:
                result[slug] = html_path
            else:
                print(f'⚠️  Slug collision "{slug}" for {fname}, skipping')
            continue
        # 4. Last resort
        slug = re.sub(r'[^A-Za-z0-9]', '_', stem)[:40]
        result[slug] = html_path

    # ── DOCX files ───────────────────────────────────────────────────────
    if HAS_DOCX:
        for docx_path in sorted(DOCS.glob('*.docx')):
            # Skip temp files (Word creates ~$filename.docx)
            if docx_path.name.startswith('~$'):
                continue
            slug = slug_from_docx(docx_path)
            # Multi-scenario SOAP: register virtual slugs instead of base slug
            if slug in MULTI_SCENARIO_SOAP:
                ms_cfg = MULTI_SCENARIO_SOAP[slug]
                for vslug in ms_cfg.get('scenarios', {}):
                    if vslug in result:
                        print(f'⚠️  Virtual slug "{vslug}" từ {docx_path.name} trùng, bỏ qua')
                        continue
                    result[vslug] = docx_path
                    print(f'  ℹ️  Multi-scenario: registered virtual slug "{vslug}" ← {docx_path.name}')
                continue
            if slug in result:
                print(f'⚠️  Slug "{slug}" từ {docx_path.name} trùng với file khác, bỏ qua')
                continue
            result[slug] = docx_path
    elif list(DOCS.glob('*.docx')):
        print('⚠️  Có file .docx trong input/ nhưng chưa cài python-docx.')
        print('   Chạy: pip install python-docx')

    # ── DOC files (legacy Word 97-2003) ──────────────────────────────────
    #    Strategy: convert .doc → .html (preserves tables), then parse as HTML.
    #    textutil (macOS) or libreoffice needed.  python-docx NOT required for .doc.
    doc_files = sorted(DOCS.glob('*.doc'))
    # Exclude *.docx from glob('*.doc') on case-insensitive filesystems
    doc_files = [p for p in doc_files if p.suffix.lower() == '.doc']
    if doc_files:
        try:
            from parse_docx import convert_doc_to_html as _conv_fn, slug_from_doc as _slug_fn
            _conv_available = True
        except ImportError:
            _conv_available = False

        if _conv_available:
            for doc_path in doc_files:
                if doc_path.name.startswith('~$'):
                    continue
                stem_slug = _slug_fn(doc_path)
                if stem_slug in result:
                    continue
                # Convert .doc → .html (textutil/libreoffice)
                print(f'🔄  Converting {doc_path.name} → .html ...')
                html_path = _conv_fn(doc_path)
                if html_path is None:
                    continue
                # Parse API Name from converted HTML to get proper slug
                try:
                    with open(html_path, encoding='utf-8', errors='replace') as fh:
                        soup_tmp = BeautifulSoup(fh, 'lxml')
                    api_name = extract_api_name_from_html(soup_tmp)
                    slug = _kebab_to_slug(api_name) if api_name else stem_slug
                except Exception:
                    slug = stem_slug
                if slug in result:
                    print(f'⚠️  Slug "{slug}" từ {doc_path.name} trùng với file khác, bỏ qua')
                    continue
                # Register as HTML — main loop will parse via HTML path
                result[slug] = html_path
        else:
            print('⚠️  Found .doc files in input/ but parse_docx could not be imported.')
            print('   Chạy: pip install python-docx')
            import platform
            os_name = platform.system()
            if os_name != 'Darwin':
                print('   Ngoài ra cần libreoffice để convert .doc → .html:')
                if os_name == 'Linux':
                    print('     sudo apt install libreoffice')
                else:
                    print('     https://www.libreoffice.org/download/')
            print('   Hoặc convert file .doc → .docx bằng Word/Google Docs rồi đặt vào input/')

    return result


HTML_FILES = _discover_html_files()


# ---------------------------------------------------------------------------
# 2. Strikethrough detection
# ---------------------------------------------------------------------------

def td_is_struck(td: Tag) -> bool:
    """Check if a single cell (or its descendants) is struck through."""
    for el in [td] + list(td.descendants):
        if not isinstance(el, Tag):
            continue
        if el.name in ('del', 's'):
            return True
        if 'line-through' in el.get('style', ''):
            return True
        cls = ' '.join(el.get('class', []))
        if 'strikethrough' in cls or 'line-through' in cls:
            return True
    return False


def _is_row_struck(tr: Tag, tds: list, name_col_idx: int) -> bool:
    """Check if a row is struck: name cell struck, OR <tr> itself struck,
    OR majority of non-empty cells in the row are struck."""
    # Direct check on name cell
    if len(tds) > name_col_idx and td_is_struck(tds[name_col_idx]):
        return True
    # Check <tr> tag itself for strikethrough style/class
    tr_style = tr.get('style', '')
    tr_cls = ' '.join(tr.get('class', []))
    if 'line-through' in tr_style or 'strikethrough' in tr_cls or 'line-through' in tr_cls:
        return True
    # Check if <tr> wraps a <del> or <s> that contains the whole row
    for child in tr.children:
        if isinstance(child, Tag) and child.name in ('del', 's'):
            # The <del>/<s> wraps most of the row content
            if len(child.find_all(['td', 'th'])) >= len(tds) // 2:
                return True
    # Majority heuristic: if ≥ 2/3 of non-empty cells are struck → row is struck
    non_empty = [td for td in tds if cell_text(td)]
    if len(non_empty) >= 2:
        struck_count = sum(1 for td in non_empty if td_is_struck(td))
        if struck_count >= len(non_empty) * 2 / 3:
            return True
    return False


def cell_text(td: Tag) -> str:
    return td.get_text(' ', strip=True)


# ---------------------------------------------------------------------------
# 3. Extract Method from API Overview table
# ---------------------------------------------------------------------------

def extract_method(soup: BeautifulSoup):
    for td in soup.find_all(['td', 'th']):
        if td.get_text(' ', strip=True).strip().lower() == 'method':
            nxt = td.find_next_sibling(['td', 'th'])
            if nxt:
                return nxt.get_text(strip=True).upper()
    return None


# ---------------------------------------------------------------------------
# 4. Find a table whose first 20 cells contain all given keywords
# ---------------------------------------------------------------------------

def find_table_containing(soup: BeautifulSoup, keywords: list):
    """
    Find first table whose full text (first 100 cells) contains ALL keywords.
    Normalises whitespace and non-breaking spaces before comparing.
    """
    for tbl in soup.find_all('table'):
        sample = ' '.join(
            td.get_text(' ', strip=True)
            for td in tbl.find_all(['td', 'th'])[:100]
        ).replace('\xa0', ' ').lower()
        if all(kw.lower() in sample for kw in keywords):
            return tbl
    return None


def find_all_tables_containing(soup: BeautifulSoup, keywords: list) -> list:
    """Return ALL tables whose first 100 cells contain ALL keywords."""
    result = []
    for tbl in soup.find_all('table'):
        sample = ' '.join(
            td.get_text(' ', strip=True)
            for td in tbl.find_all(['td', 'th'])[:100]
        ).replace('\xa0', ' ').lower()
        if all(kw.lower() in sample for kw in keywords):
            result.append(tbl)
    return result


# ---------------------------------------------------------------------------
# 5. Parse request body fields table
# ---------------------------------------------------------------------------
# Column positions are AUTO-DETECTED from the header row using aliases in
# baseline/table_detection.json → request_table_column_aliases.
# Fallback: legacy wide/narrow heuristic (12 vs 11 cells).
# ---------------------------------------------------------------------------

def _match_alias(header_text: str, alias_list: list) -> bool:
    """Return True if header_text matches any alias (case-insensitive, stripped)."""
    h = header_text.replace('\xa0', ' ').strip().lower()
    return any(h == a.lower() for a in alias_list)


def _detect_columns(header_cells: list, aliases: dict) -> dict:
    """
    Auto-detect column indices from header row using alias config.
    Returns dict like {'parameter': 1, 'type': 5, 'mandatory': 7, ...}
    Values are column indices (0-based).  Missing roles get -1.
    """
    result = {role: -1 for role in aliases}
    for col_idx, td in enumerate(header_cells):
        h = cell_text(td)
        for role, alias_list in aliases.items():
            if result[role] == -1 and _match_alias(h, alias_list):
                result[role] = col_idx
    return result


def _find_header_row(data_rows: list, aliases: dict) -> int:
    """Scan rows to find the header row (the one matching the most alias columns).
    Returns the index into data_rows.  Falls back to 0 if nothing matches well."""
    best_idx, best_score = 0, 0
    # Scan up to 15 early rows (header is usually near the top, but some docs
    # have overview rows before the actual field table header)
    for i, tr in enumerate(data_rows[:15]):
        cells = tr.find_all(['td', 'th'])
        col_map = _detect_columns(cells, aliases)
        score = sum(1 for v in col_map.values() if v >= 0)
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


def _find_all_request_tables(soup: BeautifulSoup) -> list:
    """Find ALL candidate request-body tables using configurable keyword groups.
    Tables that also match error-table keyword groups are excluded to prevent
    error descriptions containing words like 'parameter'/'mandatory' from
    being mistakenly treated as request fields.
    """
    # First, collect IDs of all error tables so we can exclude them
    error_tbl_ids = set()
    for kw_group in _ERR_TABLE_KW_GROUPS:
        for tbl in find_all_tables_containing(soup, kw_group):
            error_tbl_ids.add(id(tbl))

    seen_ids = set()
    tables = []
    for kw_group in _REQ_TABLE_KW_GROUPS:
        for tbl in find_all_tables_containing(soup, kw_group):
            tbl_id = id(tbl)
            if tbl_id not in seen_ids and tbl_id not in error_tbl_ids:
                seen_ids.add(tbl_id)
                tables.append(tbl)
    # Ultimate fallback
    if not tables:
        fallback = find_table_containing(soup, ['parameter', 'type'])
        if fallback and id(fallback) not in error_tbl_ids:
            tables.append(fallback)
    return tables


def _parse_single_request_table(tbl) -> tuple:
    """Parse one request-body table. Returns (active, struck) field lists."""
    fields = []
    data_rows = [tr for tr in tbl.find_all('tr') if tr.find_all(['td', 'th'])]
    if len(data_rows) < 2:
        return [], []

    # ── Smart header row detection: scan rows for best alias match ────────
    header_idx = _find_header_row(data_rows, _REQ_COL_ALIASES) if _REQ_COL_ALIASES else 0
    header_cells = data_rows[header_idx].find_all(['td', 'th'])
    col_map = _detect_columns(header_cells, _REQ_COL_ALIASES) if _REQ_COL_ALIASES else {}

    COL_NAME1 = col_map.get('parameter', -1)
    COL_LEVEL = col_map.get('level', -1)
    COL_TYPE  = col_map.get('type', -1)
    COL_MAND  = col_map.get('mandatory', -1)
    # note is always the last column unless explicitly detected
    COL_NOTE  = col_map.get('note', -1)
    COL_DEFVAL = col_map.get('default_value', -1)
    COL_DESC  = col_map.get('description', -1)

    # Fallback: legacy wide/narrow heuristic when header detection fails
    if COL_NAME1 == -1:
        first_data_idx = header_idx + 1
        first_data = data_rows[first_data_idx].find_all(['td', 'th']) if first_data_idx < len(data_rows) else []
        wide = len(first_data) >= 12
        COL_NAME1 = 1
        COL_LEVEL = 3 if wide else 2
        COL_TYPE  = 5 if wide else 4
        COL_MAND  = 7 if wide else 6

    # Detect if there are sub-field columns (data row has more cells than header due to colspan)
    header_count = len(header_cells)
    first_data_idx = header_idx + 1
    # Use MAX cell count from a sample of rows after the header.
    # This correctly handles docs where the first row after the header is a
    # sub-header (fewer cells due to rowspan carryover) — we want the actual
    # data row width, not the sub-header width.
    _sample_counts = [
        len(data_rows[_i].find_all(['td', 'th']))
        for _i in range(first_data_idx, min(first_data_idx + 10, len(data_rows)))
    ]
    first_data_count = max(_sample_counts) if _sample_counts else header_count
    extra_cols = first_data_count - header_count
    # If data rows have extra columns (colspan in header), shift detected positions
    COL_NAME2 = None
    if extra_cols > 0 and COL_NAME1 >= 0:
        COL_NAME2 = COL_NAME1 + 1  # sub-field column right after name
        # Shift all columns after name by the extra amount
        if COL_LEVEL > COL_NAME1:
            COL_LEVEL += extra_cols
        if COL_TYPE > COL_NAME1:
            COL_TYPE += extra_cols
        if COL_MAND > COL_NAME1:
            COL_MAND += extra_cols
        if COL_NOTE > COL_NAME1:
            COL_NOTE += extra_cols
        if COL_DEFVAL > COL_NAME1:
            COL_DEFVAL += extra_cols
        if COL_DESC > COL_NAME1:
            COL_DESC += extra_cols

    for i, tr in enumerate(data_rows):
        if i <= header_idx:  # skip header and any rows before it
            continue
        tds = tr.find_all(['td', 'th'])
        # Skip rows that have fewer cells than the header — these are section
        # labels, sub-headers with rowspan carryover, or 'end X' delimiters.
        if header_count > 0 and len(tds) < header_count:
            continue
        texts = [cell_text(td) for td in tds]

        # name: col1 (L1) OR col2 (L2 sub-field, wide tables only)
        name = texts[COL_NAME1].strip('* ') if len(texts) > COL_NAME1 >= 0 else ''
        name_col_idx = COL_NAME1
        if not name and COL_NAME2 is not None and len(texts) > COL_NAME2:
            name = texts[COL_NAME2].strip('* ')
            name_col_idx = COL_NAME2
        # normalise rendering artefacts ('transactionA ctivity')
        name = re.sub(r'\s+', '', name)
        if not name or re.match(r'^\d+$', name):
            continue

        level = texts[COL_LEVEL].strip() if len(texts) > COL_LEVEL >= 0 else ''
        typ   = texts[COL_TYPE ].strip() if len(texts) > COL_TYPE  >= 0 else ''
        mand  = texts[COL_MAND ].strip() if len(texts) > COL_MAND  >= 0 else ''
        note  = (texts[COL_NOTE].strip() if COL_NOTE >= 0 and len(texts) > COL_NOTE
                 else texts[-1].strip() if texts else '')
        default_value = (texts[COL_DEFVAL].strip()
                         if COL_DEFVAL >= 0 and len(texts) > COL_DEFVAL else '')
        description = (texts[COL_DESC].strip()
                       if COL_DESC >= 0 and len(texts) > COL_DESC else '')

        # Struck = check name cell AND entire row (tr + any cell)
        struck = _is_row_struck(tr, tds, name_col_idx)

        enum_values = extract_enum_values_from_note(note)
        # If no enum found in note, try the default_value column too
        if not enum_values and default_value:
            enum_values = extract_enum_values_from_note(default_value)
        fields.append({'name': name, 'level': level, 'type': typ,
                       'mandatory': mand, 'note': note,
                       'default_value': default_value,
                       'description': description,
                       'enum_values': enum_values, 'struck': struck})

    active = [f for f in fields if not f['struck']]
    struck_out = [f for f in fields if f['struck']]
    return active, struck_out


def parse_request_table(soup: BeautifulSoup):
    """Parse request fields from ALL matching tables, merging results.
    After parsing, resolves cross-reference enum values from lookup tables."""
    tables = _find_all_request_tables(soup)
    if not tables:
        return [], []
    all_active, all_struck = [], []
    seen_names = set()
    for tbl in tables:
        active, struck = _parse_single_request_table(tbl)
        for f in active:
            if f['name'] not in seen_names:
                seen_names.add(f['name'])
                all_active.append(f)
        for f in struck:
            if f['name'] not in seen_names:
                seen_names.add(f['name'])
                all_struck.append(f)

    # --- Resolve enum cross-references from lookup tables -----------------
    _resolve_enum_cross_refs(all_active, soup, tables)

    return all_active, all_struck


# ---------------------------------------------------------------------------
# 5b-pre. Enum cross-reference resolution from lookup tables
# ---------------------------------------------------------------------------

# Regex: "See ... at N.N" / "see ... section N.N" / "xem ... tại N.N"
_CROSS_REF_RE = re.compile(
    r'(?:see|xem|tham\s*kh[aả]o|danh\s*s[aá]ch)\b.*?(?:at|t[aạ]i|mục|section)\s*'
    r'(\d+(?:\.\d+)*)',
    re.IGNORECASE
)


def _find_enum_lookup_tables(soup: BeautifulSoup,
                             exclude_tables: list) -> list:
    """Find small lookup/reference tables in the document.

    Lookup tables are identified as:
      - NOT one of the request/error tables
      - 2-6 columns
      - 3+ data rows
      - Cells contain short values (< 60 chars on average)

    Returns list of tuples: (table_element, header_texts, data_rows_texts)
    """
    exclude_ids = {id(t) for t in exclude_tables}
    lookup_tables = []
    for table in soup.find_all('table'):
        if id(table) in exclude_ids:
            continue
        rows = table.find_all('tr')
        if len(rows) < 3:
            continue
        # Get header
        first_cells = rows[0].find_all(['td', 'th'])
        ncols = len(first_cells)
        if ncols < 2 or ncols > 6:
            continue
        header = [cell_text(c).strip().lower() for c in first_cells]
        # Get data rows
        data = []
        for tr in rows[1:]:
            cells = tr.find_all(['td', 'th'])
            texts = [cell_text(c).strip() for c in cells]
            if any(t for t in texts):  # at least one non-empty cell
                data.append(texts)
        if len(data) < 2:
            continue
        # Check average cell length (lookup tables have short values)
        all_vals = [t for row in data for t in row if t]
        if not all_vals:
            continue
        avg_len = sum(len(v) for v in all_vals) / len(all_vals)
        if avg_len > 40:
            continue
        lookup_tables.append((table, header, data))
    return lookup_tables


def _extract_enums_from_lookup_table(header: list, data: list,
                                     field_name: str) -> list:
    """Extract enum values from a lookup table.

    Strategy:
      1. If a column header matches the field name → extract that column's values
      2. Otherwise extract values from columns with code-like values
         (headers containing 'product', 'code', 'value', 'type', etc.)
    Only collects from columns whose values look like identifiers/codes
    (alphanumeric + dots/dashes, no spaces).
    """
    field_lower = field_name.lower().replace('.', '').replace('_', '')

    # Identify which columns are "code columns" (have code-like values)
    code_cols = []
    for col_idx in range(len(header)):
        col_vals = [row[col_idx] for row in data
                    if col_idx < len(row) and row[col_idx].strip()]
        if col_vals and all(len(v) <= 30 and
                            re.match(r'^[A-Za-z0-9._\-]+$', v.strip())
                            for v in col_vals):
            code_cols.append(col_idx)

    # Strategy 1: find column matching field name
    target_col = -1
    for i, h in enumerate(header):
        h_clean = h.replace(' ', '').replace('_', '')
        if h_clean and (h_clean == field_lower or field_lower in h_clean
                        or h_clean in field_lower):
            target_col = i
            break

    # Strategy 2: use first code column
    if target_col == -1 and code_cols:
        target_col = code_cols[0]

    if target_col == -1:
        return []

    # Collect unique non-empty values from code columns only
    # Filter: reject title-case English words (e.g. "Monthly", "Quarterly")
    # Accept: ALL-CAPS (PRODUCT_CODE), dotted codes (PROD.TYPE.AR), mixed (PRODUCT_CODE.ML)
    def _is_code_value(v: str) -> bool:
        if not v or len(v) > 30:
            return False
        if not re.match(r'^[A-Za-z0-9._\-]+$', v):
            return False
        # Reject single title-case words (e.g. "Monthly", "Quarterly")
        if re.match(r'^[A-Z][a-z]{2,}$', v):
            return False
        return True

    values = []
    seen = set()
    # Ordered: target column first, then other code columns
    ordered_cols = [target_col] + [c for c in code_cols if c != target_col]
    for row in data:
        for ci in ordered_cols:
            if ci >= len(row):
                continue
            v = row[ci].strip()
            if v and v not in seen and _is_code_value(v):
                seen.add(v)
                values.append(v)
    return values


def _resolve_enum_cross_refs(fields: list, soup: BeautifulSoup,
                             request_tables: list) -> None:
    """For fields with missing/incomplete enum_values, check if note/default_value
    contains a cross-reference to a lookup table (e.g. 'See the list at 1.2').
    If found, extract enum values from the lookup table and attach them.

    Also applies prose-based enum detection for natural language descriptions
    like 'Có những type sau X Y Z' found in the description column.

    Priority: cross-ref lookup > prose from description > prose from note.
    Cross-ref overrides single-example enum values.
    """
    # Collect error tables to exclude
    error_tbl = _find_error_table(soup)
    exclude = list(request_tables)
    if error_tbl:
        exclude.append(error_tbl)
    lookup_tables = _find_enum_lookup_tables(soup, exclude)

    for field in fields:
        existing = field.get('enum_values', [])

        # --- Phase 1: Cross-reference resolution (overrides single examples) ---
        if lookup_tables:
            texts_to_check = [field.get('default_value', ''),
                              field.get('note', ''),
                              field.get('description', '')]
            for text in texts_to_check:
                if not text:
                    continue
                m = _CROSS_REF_RE.search(text)
                if m:
                    # Cross-reference found → try to resolve from lookup table
                    for (tbl, header, data) in lookup_tables:
                        enums = _extract_enums_from_lookup_table(
                            header, data, field['name'])
                        if enums:
                            field['enum_values'] = enums
                            print(f'    ℹ️  Cross-ref enum resolved for '
                                  f'"{field["name"]}": {len(enums)} values '
                                  f'from lookup table ({text[:50]})')
                            break
                    if len(field.get('enum_values', [])) > len(existing):
                        break  # found better enum values

        # --- Phase 2: Prose enum from description column ---
        if len(field.get('enum_values', [])) <= 1:
            desc = field.get('description', '')
            if desc:
                prose_enums = _extract_prose_enum_values(desc)
                if prose_enums and len(prose_enums) >= 2:
                    field['enum_values'] = prose_enums
                    print(f'    ℹ️  Prose enum (description) for '
                          f'"{field["name"]}": {prose_enums}')

        # --- Phase 3: Prose enum from note (last resort) ---
        if len(field.get('enum_values', [])) <= 1:
            note = field.get('note', '')
            if note:
                prose_enums = _extract_prose_enum_values(note)
                if prose_enums and len(prose_enums) >= 2:
                    field['enum_values'] = prose_enums
                    print(f'    ℹ️  Prose enum (note) for '
                          f'"{field["name"]}": {prose_enums}')


def _extract_prose_enum_values(text: str) -> list:
    """Detect enum values from natural language descriptions.

    Patterns detected:
      - "Có những type sau COMMITMENT RENEWAL SCHEDULE SETTLEMENT"
      - "There are N types: X, Y, Z"
      - "bao gồm X, Y, Z"
      - "Nếu X = A => ..., Nếu X = B => ..."  (conditional description)
      - "if X = A then ..., if X = B then ..."
      - "X: A → ...; B → ..."
      - "= PI=> ..., = N=> ..., = PR=> ..."

    Conservative: only extracts when there's a clear pattern followed
    by code-like tokens. Limits to 15 values max. Each token must be at
    least 1 char and not truncated.
    """
    if not text or len(text) < 10:
        return []

    # ── Pattern 0: Conditional enum (highest priority) ────────────────
    # "Nếu maturityInstruction = PI => Xoay vòng... Nếu = N => ..."
    # "if channel = MB then ... if channel = IB then ..."
    # "= PI=> ...; = N=> ...; = PR=> ..."
    cond_re = re.compile(
        r'(?:if|if)\s+(?:\w+\s*)?=\s*'       # "Nếu X =" or "if X ="
        r'["\']?\s*([A-Za-z0-9_.+\-]+)\s*["\']?'  # capture the value
        r'\s*(?:=>|→|thì|then|:)',
        re.IGNORECASE
    )
    cond_vals = cond_re.findall(text)
    # Also catch shorthand "= PI=> ..., = N=> ..."
    short_re = re.compile(
        r'=\s*["\']?\s*([A-Za-z0-9_.+\-]{1,20})\s*["\']?\s*(?:=>|→)',
        re.IGNORECASE
    )
    short_vals = short_re.findall(text)
    # Merge all conditional values
    all_cond = []
    seen_cond = set()
    for v in cond_vals + short_vals:
        v_clean = v.strip().strip("'\"")
        if v_clean and v_clean.upper() not in seen_cond:
            seen_cond.add(v_clean.upper())
            all_cond.append(v_clean)
    if len(all_cond) >= 2:
        return all_cond[:15]

    # ── Pattern 1: Explicit listing phrases ───────────────────────────
    # "Có những X sau A B C" / "bao gồm A B C" / "There are N: A B C"
    # "gồm các giá trị: A, B, C"
    intro_re = re.compile(
        r'(?:có\s+(?:những|các)\s+\w+\s+sau|'
        r'bao\s+gồm|gồm\s+các|'
        r'there\s+are\s+\d+\s*\w*|'
        r'includes?\s*:?\s*)'
        r'\s*:?\s*',
        re.IGNORECASE
    )
    m = intro_re.search(text)
    if m:
        after = text[m.end():].strip()
        # Extract consecutive ALL-CAPS / dotted-code tokens from the text after intro
        tokens = re.findall(r'\b[A-Z][A-Z0-9.]*(?:\.[A-Z0-9]+)*\b', after)
        noise = {'NA', 'OR', 'AND', 'THE', 'FOR', 'NOT', 'NULL', 'NOTE',
                 'TYPE', 'IF', 'API', 'WITH', 'GET', 'SET', 'PUT',
                 'STRING', 'NUMBER', 'BOOLEAN', 'OBJECT', 'ARRAY'}
        filtered = [t for t in tokens if t not in noise and len(t) >= 2
                    and not t.endswith('N')]  # avoid truncated tokens
        # Deduplicate
        seen = set()
        deduped = []
        for t in filtered:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        if 2 <= len(deduped) <= 15:
            return deduped

    # ── Pattern 2: Value list with descriptions ───────────────────────
    # "A: description; B: description; C: description"
    # "A - description, B - description"
    val_desc_re = re.compile(
        r'\b([A-Z][A-Z0-9_.]{0,19})\s*[-:]\s*[A-Za-zÀ-ỹ]',
    )
    val_desc_vals = val_desc_re.findall(text)
    if val_desc_vals:
        noise2 = {'NA', 'OR', 'AND', 'THE', 'FOR', 'NOT', 'NULL', 'NOTE',
                  'TYPE', 'IF', 'API', 'GET', 'SET', 'PUT', 'STRING',
                  'NUMBER', 'BOOLEAN', 'OBJECT', 'ARRAY', 'Y', 'N'}
        cleaned = []
        seen2 = set()
        for v in val_desc_vals:
            if v not in noise2 and v not in seen2 and len(v) >= 2:
                seen2.add(v)
                cleaned.append(v)
        if 2 <= len(cleaned) <= 15:
            return cleaned

    return []


# ---------------------------------------------------------------------------
# 5b. Extract enum values from Note/Example cell
# ---------------------------------------------------------------------------

def extract_enum_values_from_note(note: str) -> list:
    """
    Phát hiện enum / valid case values từ nội dung cột Note/Example.

    Các pattern được nhận diện:
      - 'A/B/C'            → ['A', 'B', 'C']
      - 'A, B or C'        → ['A', 'B', 'C']
      - 'null if ... not null if ...' → ['null_cif_level', 'not_null_account_level']
      - Short single value (< 30 chars, not a sentence) → [value]
    Returns [] if không detect được enum.
    """
    if not note:
        return []
    n = note.strip()

    # Pattern: null / not null conditional (2 modes)
    if re.search(r'\bnull\b.*\bnot null\b', n, re.IGNORECASE):
        return ['null', 'not_null']

    # Pattern: slash-separated values e.g. "P/P+I/N" or "MB/IB/TB"
    slash_match = re.match(r'^([A-Za-z0-9+_\-]{1,20})(?:/([A-Za-z0-9+_\-]{1,20}))+$', n)
    if slash_match:
        return [v.strip() for v in n.split('/') if v.strip()]

    # Pattern: bracket-listed values e.g. "[A, B, C]" or "(X|Y|Z)"
    bracket_match = re.match(r'^[\[\(]\s*(.+?)\s*[\]\)]$', n)
    if bracket_match:
        inner = bracket_match.group(1)
        inner_parts = re.split(r'\s*[,|;]\s*', inner)
        if 2 <= len(inner_parts) <= 15 and all(len(p.strip()) <= 30 for p in inner_parts):
            clean = [p.strip() for p in inner_parts if p.strip()]
            if len(clean) >= 2:
                return clean

    # Pattern: colon-separated definition  e.g. "type: A, B, C" or "values: X/Y/Z"
    colon_match = re.match(r'^[A-Za-z\s]{1,20}:\s*(.+)$', n)
    if colon_match:
        after_colon = colon_match.group(1).strip()
        # Recurse on the part after colon
        sub = extract_enum_values_from_note(after_colon)
        if sub:
            return sub

    # Pattern: comma or 'or' separated short tokens
    # e.g. "monthly, quarterly, at_maturity" or "DEBIT or CREDIT"
    parts = re.split(r'\s*,\s*|\s+or\s+|\s*/\s*', n)
    if 2 <= len(parts) <= 10 and all(len(p) <= 30 and not re.search(r'\s{2,}', p) for p in parts):
        # Make sure they're all short tokens (not sentences)
        if all(re.match(r'^[A-Za-z0-9+_\-\.\s]{1,30}$', p) for p in parts):
            clean = [p.strip() for p in parts if p.strip()]
            if len(clean) >= 2:
                return clean

    # Pattern: numeric range  e.g. "1-12" or "0~100"
    range_match = re.match(r'^(\d+)\s*[-~]\s*(\d+)$', n)
    if range_match:
        lo, hi = int(range_match.group(1)), int(range_match.group(2))
        if hi - lo <= 30:  # Only expand small ranges
            return [str(v) for v in range(lo, hi + 1)]
        # Large range: return boundaries as representative values
        return [str(lo), str(hi)]

    # Pattern: single short literal value (likely an example that IS the only valid value)
    # Skip if it contains prose keywords (loaded from baseline/table_detection.json)
    if len(n) <= 30 and not any(kw in n.lower() for kw in _PROSE_KEYWORDS):
        if re.match(r'^[A-Za-z0-9+_\-\.]{1,30}$', n):
            return [n]

    return []


# ---------------------------------------------------------------------------
# 6. Parse response data fields
# ---------------------------------------------------------------------------

def _doc_type_to_js_type(raw: str) -> str:
    """Map T24/doc type string to JS type for assertion generation."""
    t = raw.strip().lower()
    if t in ('number', 'integer', 'long', 'float', 'double', 'bigdecimal', 'int'):
        return 'Number'
    if t in ('boolean', 'bool'):
        return 'Boolean'
    if t in ('array', 'list', 'listobject'):
        return 'Array'
    if t in ('object', 'map', 'json'):
        return 'Object'
    return 'String'


def _parse_response_pattern_a(soup: BeautifulSoup) -> dict:
    """
    Pattern A: Table contains a row with 'Successful Respond' in first or second cell,
    followed by individual field rows. Rows after a 'data' Object row are data sub-fields.
    Returns {fieldName: jsType} for data-level fields only.
    """
    for tbl in soup.find_all('table'):
        rows = tbl.find_all('tr')
        in_response = False
        in_data_section = False
        data_fields: dict = {}

        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
            if not cells:
                continue
            c0_lower = cells[0].strip().lower()
            c1       = cells[1].strip() if len(cells) > 1 else ''
            c1_lower = c1.lower()

            # Detect section start
            if 'successful respond' in c0_lower or 'successful respond' in c1_lower:
                # If we already collected data_fields from a previous Successful Respond
                # section in this same table, stop — don't let a second section overwrite.
                if data_fields:
                    break
                in_response = True
                in_data_section = False
                continue

            # Stop at unsuccessful section
            if in_response and ('unsuccessful respond' in c0_lower or 'unsuccessful respond' in c1_lower):
                break

            if not in_response:
                continue

            # Detect "data" Object row → following rows are nested in data
            if c1_lower == 'data' and any('object' in c.lower() for c in cells):
                in_data_section = True
                continue

            # Skip header / label rows
            if not c1 or c1_lower in ('no.', '#', 'business name', 'field name', 'parameter'):
                continue

            # Find type value from remaining cells
            ftype_raw = ''
            for c in cells[2:]:
                cl = c.strip().lower()
                if cl in ('string', 'number', 'integer', 'long', 'float', 'double',
                          'boolean', 'bool', 'array', 'list', 'object', 'map', 'json'):
                    ftype_raw = cl
                    break

            if in_data_section:
                data_fields[c1] = _doc_type_to_js_type(ftype_raw)

        if data_fields:
            return data_fields

    return {}


def _extract_data_fields_from_json_text(text: str) -> dict:
    """
    Extract {fieldName: jsType} from the 'data' object inside a JSON response sample text.
    Handles inline // comments, double-brace template artifacts, trailing commas, and NBSP.
    """
    # Keep only the success part (before --fail-- or Fail{)
    success_part = text.split('--fail--')[0].split('Fail{')[0]
    # Normalize non-breaking spaces
    success_part = success_part.replace('\xa0', ' ')
    # Fix double-brace template artifacts: {{ → {
    # Only replace opening {{ (e.g. "data":{{ in doc samples).
    # Do NOT replace }}: the extra }} at end are legitimate JSON closing braces
    # needed to balance the outer object.
    if '{{' in success_part:
        success_part = success_part.replace('{{', '{')
    # Strip trailing commas before } or ] (invalid JSON but common in doc samples)
    success_part = re.sub(r',\s*([}\]])', r'\1', success_part)
    # Strip inline // comments: stop before next " or } or newline
    success_part = re.sub(r'//[^"\}\n]*', '', success_part)

    # Find the first { to start brace-walking the outermost JSON object
    brace_start = success_part.find('{')
    if brace_start == -1:
        return {}

    json_str = success_part[brace_start:]
    parsed = None
    depth, end_pos = 0, -1
    for idx, ch in enumerate(json_str):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end_pos = idx + 1
                break
    if end_pos > 0:
        try:
            parsed = json.loads(json_str[:end_pos])
        except json.JSONDecodeError:
            pass

    if not parsed or not isinstance(parsed, dict):
        return {}

    # Try standard response envelope: response.data contains the API-specific fields
    data_obj = parsed.get('data', {})

    # If no 'data' key or data is not a dict, check if the parsed object itself
    # looks like the data sub-object (e.g. when brace-walk found inner {} directly)
    if not isinstance(data_obj, dict) or not data_obj:
        envelope_keys = {'status', 'code', 'message', 'messageid', 'messagekey',
                         'timestamp', 'traceid', 'data'}
        has_envelope = bool(set(k.lower() for k in parsed) & envelope_keys)
        if not has_envelope:
            data_obj = parsed
        else:
            return {}
    else:
        # Merge any non-envelope sibling keys from the outer object into data_obj.
        # This handles doc samples where "data":{{"field":value, ...}} artifacts cause
        # some data-level fields to be parsed at the outer envelope level instead.
        envelope_keys = {'status', 'code', 'message', 'messageid', 'messagekey',
                         'timestamp', 'traceid', 'data'}
        for k, v in parsed.items():
            if k.lower() not in envelope_keys and k not in data_obj:
                data_obj[k] = v

    result = {}
    for fname, fval in data_obj.items():
        if isinstance(fval, dict):
            result[fname] = 'Object'
        elif isinstance(fval, list):
            result[fname] = 'Array'
        elif isinstance(fval, bool):
            result[fname] = 'Boolean'
        elif isinstance(fval, (int, float)):
            result[fname] = 'Number'
        else:
            result[fname] = 'String'
    return result


def _parse_response_pattern_b(soup: BeautifulSoup) -> dict:
    """
    Pattern B: Table with 'Request | Response' (or 'Sample request | Sample response') header.
    Parses JSON sample from the Response cell to extract data-level fields.
    """
    for tbl in soup.find_all('table'):
        rows = tbl.find_all('tr')
        for i, row in enumerate(rows):
            cells = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
            if len(cells) < 2:
                continue
            c0_lower = cells[0].strip().lower()
            c1_lower = cells[1].strip().lower()
            is_req_resp_header = (
                c0_lower in ('request', 'sample request') and
                any(kw in c1_lower for kw in ('response', 'sample response'))
            )
            if not is_req_resp_header:
                continue

            # Try next data row (cells[1] = response JSON)
            resp_text = ''
            if i + 1 < len(rows):
                next_cells = [td.get_text(strip=True) for td in rows[i + 1].find_all(['td', 'th'])]
                if len(next_cells) > 1:
                    resp_text = next_cells[1]
            # Also try inline in cells[1] if it contains JSON
            if len(cells[1]) > 100 and '{' in cells[1]:
                resp_text = cells[1]

            data_fields = _extract_data_fields_from_json_text(resp_text)
            if data_fields:
                return data_fields

    return {}


def parse_response_fields(soup: BeautifulSoup) -> dict:
    """
    Parse response data-level fields from HTML doc.
    Returns {fieldName: jsType} for fields inside response.data object.
    Tries Pattern A (row-by-row field table after 'Successful Respond') first,
    then Pattern B (JSON sample in Request/Response table).
    """
    result = _parse_response_pattern_a(soup)
    if result:
        return result
    return _parse_response_pattern_b(soup)


# ---------------------------------------------------------------------------
# 7. Parse error codes table
# ---------------------------------------------------------------------------

def normalize_code(s: str) -> str:
    """Normalise 'DT. 005.01.000' → 'DT.005.01.000' (remove stray spaces/NBSP)."""
    return re.sub(r'[\s\xa0]+', '', s)


def _find_error_table(soup: BeautifulSoup):
    """Find the error-codes table using configurable keyword groups from baseline."""
    for kw_group in _ERR_TABLE_KW_GROUPS:
        tbl = find_table_containing(soup, kw_group)
        if tbl:
            return tbl
    # Ultimate fallback: any table with 'error' and 'code'
    return find_table_containing(soup, ['error', 'code'])


def parse_error_table(soup: BeautifulSoup):
    tbl = _find_error_table(soup)
    if not tbl:
        return [], []

    # ── Smart header row detection ─────────────────────────────────────────
    all_rows = tbl.find_all('tr')
    data_rows_err = [tr for tr in all_rows if tr.find_all(['td', 'th'])]
    header_idx = _find_header_row(data_rows_err, _ERR_COL_ALIASES) if _ERR_COL_ALIASES and data_rows_err else 0
    header_row = data_rows_err[header_idx] if data_rows_err else None
    header_cells = header_row.find_all(['td', 'th']) if header_row else []
    col_map = _detect_columns(header_cells, _ERR_COL_ALIASES) if _ERR_COL_ALIASES else {}

    COL_CODE   = col_map.get('code', -1)
    COL_KEY    = col_map.get('key', -1)
    COL_REASON = col_map.get('reason', -1)

    # Fallback: legacy positional logic (col0=STT, col1=code, col2=key, col3=reason)
    if COL_CODE == -1:
        COL_CODE   = 1 if _ERR_COL0_IS_SEQ else 0
        COL_KEY    = COL_CODE + 1
        COL_REASON = COL_CODE + 2

    errors = []
    for i, tr in enumerate(data_rows_err):
        if i <= header_idx:
            continue
        tds = tr.find_all(['td', 'th'])
        if not tds:
            continue
        texts = [cell_text(td) for td in tds]
        raw_code = texts[COL_CODE] if len(texts) > COL_CODE >= 0 else ''
        code = normalize_code(raw_code)
        # Accept any structured error code matching the configurable pattern
        if not code or not re.match(_ERROR_CODE_PATTERN, code):
            continue
        key    = texts[COL_KEY]    if COL_KEY >= 0 and len(texts) > COL_KEY    else ''
        reason = texts[COL_REASON] if COL_REASON >= 0 and len(texts) > COL_REASON else ''
        # Struck = check code cell + entire row
        struck = _is_row_struck(tr, tds, COL_CODE)
        errors.append({'code': code, 'key': key, 'reason': reason, 'struck': struck})

    # Deduplicate
    seen, deduped = set(), []
    for e in errors:
        k = (e['code'], e['key'])
        if k not in seen:
            seen.add(k)
            deduped.append(e)

    active      = [e for e in deduped if not e['struck']]
    struck_list = [e for e in deduped if e['struck']]
    return active, struck_list


# ---------------------------------------------------------------------------
# 7. Load sampler collection (DP/mock requests)
# ---------------------------------------------------------------------------

def _url_path_to_slug(url_path: str) -> str:
    """Convert URL path like '/encrypt/v1/get-account-transactions' → 'getAccountTransactions'.
    Takes the last path segment and converts kebab-case to camelCase."""
    segment = url_path.rstrip('/').rsplit('/', 1)[-1]
    return _kebab_to_slug(segment)


def _collect_all_requests(items: list) -> list:
    """Recursively collect all leaf requests from a Postman item tree."""
    reqs = []
    for item in items:
        if 'request' in item:
            reqs.append(item)
        if 'item' in item:
            reqs.extend(_collect_all_requests(item['item']))
    return reqs


# ---------------------------------------------------------------------------
#  Variable dependency resolver for sampler requests
# ---------------------------------------------------------------------------
_VAR_CONSUME_RE = re.compile(r'\{\{(\w+)\}\}')
_VAR_PRODUCE_RE = re.compile(r'pm\.(?:collectionVariables|environment|variables|globals)\.set\(\s*["\'](\w+)["\']')


def _extract_var_consumes(req_item: dict) -> set:
    """Return set of {{variable}} names consumed by a request (body, URL, headers, scripts)."""
    r = req_item.get('request', {})
    url_obj = r.get('url', {})
    raw_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
    body_raw = r.get('body', {}).get('raw', '') if isinstance(r.get('body'), dict) else ''
    headers_str = json.dumps(r.get('header', []))
    text = raw_url + '\n' + body_raw + '\n' + headers_str
    return set(_VAR_CONSUME_RE.findall(text))


def _extract_var_produces(req_item: dict) -> set:
    """Return set of variable names set/produced by a request's test/post-response scripts."""
    produces = set()
    for ev in req_item.get('event', []):
        # Only test scripts produce variables (prerequest typically only consumes)
        if ev.get('listen') in ('test', 'postresponse'):
            script_text = '\n'.join(ev.get('script', {}).get('exec', []))
            produces.update(_VAR_PRODUCE_RE.findall(script_text))
    return produces


def _build_setup_item_from_req(req_item: dict) -> dict:
    """Convert a raw Postman request item into the setup_items format
    used by generate_outputs.py."""
    r = req_item.get('request', {})
    url_obj = r.get('url', {})
    raw_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
    body_raw = r.get('body', {}).get('raw', '{}') if isinstance(r.get('body'), dict) else '{}'
    body_format = 'json'
    body_raw_xml = None
    if detect_soap_body(body_raw):
        body_format = 'xml'
        body_raw_xml = body_raw
        body = soap_body_to_flat_dict(body_raw)
    else:
        try:
            body = json.loads(body_raw)
        except (json.JSONDecodeError, ValueError):
            fixed = re.sub(r'(?<!")(\{\{[^}]+\}\})(?!")', r'"\1"', body_raw)
            try:
                body = json.loads(fixed)
            except (json.JSONDecodeError, ValueError):
                body = {}
    headers = {h['key']: h['value'] for h in r.get('header', [])}
    method = r.get('method', 'POST')

    # Extract prerequest script
    prerequest_script = None
    test_script = None
    req_name = req_item.get('name', 'Setup Request')
    for ev in req_item.get('event', []):
        lines = [l for l in ev.get('script', {}).get('exec', []) if l.strip()]
        if ev.get('listen') == 'prerequest' and lines:
            prerequest_script = _fix_postman_script_lines(lines, f'"{req_name}" prerequest')
        elif ev.get('listen') == 'test' and lines:
            test_script = _fix_postman_script_lines(lines, f'"{req_name}" test')

    setup = {
        "name": req_name,
        "prerequest_script": prerequest_script,
        "test_script": test_script,
        "request": {
            "method": method,
            "url": raw_url,
            "headers": headers,
            "body": body,
        },
    }
    if body_format == 'xml' and body_raw_xml:
        setup["request"]["body_format"] = "xml"
        setup["request"]["body_raw_xml"] = body_raw_xml
    return setup


def _extract_endpoint(raw_url: str) -> str:
    """Extract URL path (endpoint) from a raw Postman URL.
    Handles both full URLs and {{baseURL}}-style templates."""
    if not raw_url:
        return ''
    from urllib.parse import urlparse
    # Strip Postman variable prefix like {{baseURL}}
    cleaned = re.sub(r'\{\{[^}]+\}\}', '', raw_url).lstrip('/')
    if cleaned:
        cleaned = '/' + cleaned
    parsed = urlparse(raw_url)
    if parsed.scheme and parsed.path:
        return parsed.path
    return cleaned or raw_url


def _resolve_setup_chain(target_req_item: dict, all_req_items: list,
                          collection_vars: set,
                          forced_endpoints: list = None,
                          same_collection_items: list = None,
                          folder_prereqs: list = None) -> list:
    """Resolve the full dependency chain of setup requests for a target request.

    Returns an ordered list of setup_item dicts.

    When forced_endpoints is provided (from manual_prerequisites.json):
      - Those requests form the backbone **in the exact user-specified order**.
      - Matched by URL endpoint path.
      - Auto-detected prerequisites NOT already in the manual list are
        appended at the end.
    When forced_endpoints is empty / None:
      - Fully automatic BFS topological sort (same as before).

    Skips variables defined as collection-level variables.
    Avoids self-references and circular dependencies.
    """
    # Build maps: var → producer request item
    # Priority: same-collection producers first, then other collections (fallback)
    var_producer = {}  # var_name → req_item
    endpoint_to_req = {}  # endpoint_path → req_item (first-match)
    priority_items = list(same_collection_items or [])
    for ri in priority_items + [x for x in all_req_items if x not in priority_items]:
        for var in _extract_var_produces(ri):
            if var not in var_producer:  # first-match wins → same-collection has priority
                var_producer[var] = ri
        r = ri.get('request', {})
        url_obj = r.get('url', {})
        raw_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
        ep = _extract_endpoint(raw_url)
        if ep and ep not in endpoint_to_req:
            endpoint_to_req[ep] = ri

    # Identity key for dedup (use request name + url)
    def _req_key(ri):
        r = ri.get('request', {})
        url_obj = r.get('url', {})
        raw_url = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
        return (ri.get('name', ''), raw_url)

    target_key = _req_key(target_req_item)

    # ── Manual ordering mode ─────────────────────────────────────────────
    if forced_endpoints:
        # 1) Place manual items in user-specified order (backbone)
        manual_ordered = []
        manual_keys = set()
        for ep in forced_endpoints:
            forced_ri = endpoint_to_req.get(ep)
            if not forced_ri:
                print(f'    ⚠️  Manual prereq "{ep}" not found in sampler — skipped')
                continue
            fk = _req_key(forced_ri)
            if fk != target_key and fk not in manual_keys:
                manual_ordered.append(forced_ri)
                manual_keys.add(fk)
                print(f'    ℹ️  Manual prereq #{len(manual_ordered)}: {ep}')

        # 2) Auto-detect additional prereqs (from variable consumption)
        #    that are NOT already in the manual list → append at end
        auto_extras = []
        target_consumes = _extract_var_consumes(target_req_item)
        for var in sorted(target_consumes):
            if var in collection_vars:
                continue
            if var in var_producer:
                producer = var_producer[var]
                pk = _req_key(producer)
                if pk != target_key and pk not in manual_keys:
                    if pk not in {_req_key(r) for r in auto_extras}:
                        auto_extras.append(producer)
                        r = producer.get('request', {})
                        url_obj = r.get('url', {})
                        raw = url_obj.get('raw', '') if isinstance(url_obj, dict) else str(url_obj)
                        print(f'    ℹ️  Auto-detected extra prereq: {_extract_endpoint(raw)}')

        final = manual_ordered + auto_extras
        return [_build_setup_item_from_req(ri) for ri in final]

    # ── Full auto mode (no manual list) ──────────────────────────────────
    resolved_keys = set()
    ordered = []
    queue = []

    # Start with what the target consumes (auto-detected)
    target_consumes = _extract_var_consumes(target_req_item)
    for var in sorted(target_consumes):
        if var in collection_vars:
            continue  # supplied by collection variable, no setup needed
        if var in var_producer:
            producer = var_producer[var]
            pk = _req_key(producer)
            if pk != target_key and pk not in resolved_keys:
                queue.append(producer)
                resolved_keys.add(pk)

    # Process queue: for each producer, also resolve ITS dependencies
    max_depth = 20  # safety guard
    depth = 0
    while queue and depth < max_depth:
        depth += 1
        next_queue = []
        for producer in queue:
            pk = _req_key(producer)
            # Find this producer's dependencies
            producer_consumes = _extract_var_consumes(producer)
            deps = []
            for var in sorted(producer_consumes):
                if var in collection_vars:
                    continue
                if var in var_producer:
                    dep = var_producer[var]
                    dk = _req_key(dep)
                    if dk != pk and dk != target_key and dk not in resolved_keys:
                        deps.append(dep)
                        resolved_keys.add(dk)
                        next_queue.append(dep)
            # Insert deps BEFORE this producer
            for dep in deps:
                ordered.append(dep)
            ordered.append(producer)
        queue = next_queue

    # Prepend explicitly demoted folder prereqs (same-URL earlier requests)
    # that may not be captured by variable-dependency BFS
    if folder_prereqs:
        fp_keys = set()
        fp_ordered = []
        for ri in folder_prereqs:
            k = _req_key(ri)
            if k != target_key and k not in fp_keys and k not in resolved_keys:
                fp_ordered.append(ri)
                fp_keys.add(k)
                resolved_keys.add(k)
        ordered = fp_ordered + ordered

    # Deduplicate while preserving order (a request may appear multiple times)
    seen = set()
    deduped = []
    for ri in ordered:
        k = _req_key(ri)
        if k not in seen:
            seen.add(k)
            deduped.append(ri)

    # Re-sort by original collection order (same_collection_items preserves sampler order).
    # This ensures correct execution order without relying on BFS/DFS traversal order.
    if same_collection_items:
        col_order = {_req_key(ri): i for i, ri in enumerate(same_collection_items)}
        target_pos = col_order.get(target_key, len(col_order))
        # Only sort items that exist in same_collection_items (may include cross-collection ones)
        def _sort_key(ri):
            pos = col_order.get(_req_key(ri), len(col_order))
            return pos if pos < target_pos else len(col_order)
        deduped.sort(key=_sort_key)

    # Convert to setup_item format
    return [_build_setup_item_from_req(ri) for ri in deduped]


# ---------------------------------------------------------------------------
#  Auto-fix malformed Postman script syntax
# ---------------------------------------------------------------------------
_PM_SCRIPT_FIXES = [
    # pm.coll("key", val)  →  pm.collectionVariables.set("key", val)
    (re.compile(r'\bpm\.coll\('), 'pm.collectionVariables.set('),
    # pm.env("key", val)   →  pm.environment.set("key", val)
    (re.compile(r'\bpm\.env\('),  'pm.environment.set('),
    # pm.var("key", val)   →  pm.variables.set("key", val)
    (re.compile(r'\bpm\.var\('),  'pm.variables.set('),
]


def _fix_postman_script_lines(lines: list, context: str = '') -> list:
    """Auto-fix common Postman script syntax mistakes.
    Returns corrected list of script lines."""
    fixed = []
    for line in lines:
        original = line
        for pattern, replacement in _PM_SCRIPT_FIXES:
            line = pattern.sub(replacement, line)
        if line != original and context:
            print(f'    ℹ️  Fixed Postman script syntax in {context}: '
                  f'{original.strip()[:60]} → {line.strip()[:60]}')
        fixed.append(line)
    return fixed


def _get_collection_variable_names(sampler_paths: list) -> set:
    """Extract all collection-level variable names from sampler files.
    Excludes variables that are dynamically produced by API test scripts
    (those need setup even though they have a default collection value)."""
    static_names = set()
    all_col_vars = set()
    # First pass: collect all collection variable names
    for p in sampler_paths:
        if not p.exists():
            continue
        with open(p) as f:
            col = json.load(f)
        for v in col.get('variable', []):
            k = v.get('key', '')
            if k:
                all_col_vars.add(k)

    # Second pass: find which vars are dynamically set by API test scripts
    dynamically_produced = set()
    for p in sampler_paths:
        if not p.exists():
            continue
        with open(p) as f:
            col = json.load(f)
        for ri in _collect_all_requests(col.get('item', [])):
            dynamically_produced.update(_extract_var_produces(ri))

    # Only truly static vars (not produced by any API) are safe to skip
    static_names = all_col_vars - dynamically_produced
    return static_names


def _normalize_slug(s: str) -> str:
    """Normalise a slug for comparison: lowercase, strip (), spaces, hyphens."""
    return re.sub(r'[()\s\-_]+', '', s).lower()


def _best_contains_match(norm: str, norm_lookup: dict) -> str:
    """Find the best contains-match among known slugs.
    When multiple candidates match, pick the one with closest length (most specific).
    Returns the original slug string or '' if no match."""
    if not norm:
        return ''
    candidates = [(nk, orig) for nk, orig in norm_lookup.items()
                  if norm in nk or nk in norm]
    if not candidates:
        return ''
    if len(candidates) == 1:
        return candidates[0][1]
    # Multiple matches: pick closest length (higher = better specificity)
    candidates.sort(key=lambda pair: abs(len(pair[0]) - len(norm)))
    best = candidates[0][1]
    print(f'  ⚠️  Multiple contains matches for "{norm}": {[c[1] for c in candidates]} → picking "{best}"')
    return best


def load_sampler():
    """Load ALL sampler collection files and merge requests.
    First-match wins for slug conflicts (New Collection has priority).
    Also resolves variable dependency chains → setup_items."""
    known_slugs = set(HTML_FILES.keys())
    norm_lookup = {_normalize_slug(s): s for s in known_slugs}
    result = {}

    # Collect ALL raw request items from every sampler (for dependency resolution)
    all_raw_req_items = []
    # slug → raw req_item mapping (for dependency resolver to identify target)
    slug_to_req_item = {}
    # slug → all requests from the SAME sampler file (for same-collection priority)
    slug_to_same_collection_items = {}
    # slug → sampler file path (to detect same-collection duplicates)
    slug_to_sampler_path = {}
    # slug → list of demoted "folder prereq" req_items (same-URL earlier requests)
    slug_to_folder_prereqs = {}
    # Collection-level variable names (these don't need setup)
    collection_var_names = _get_collection_variable_names(SAMPLERS)

    for sampler_path in SAMPLERS:
        if not sampler_path.exists():
            continue
        with open(sampler_path) as f:
            col = json.load(f)

        all_reqs = _collect_all_requests(col.get('item', []))
        if not all_reqs:
            print(f"  ⚠️  Sampler '{sampler_path.name}': no requests found — skipping.")
            continue

        # Accumulate ALL requests (for dep resolution across all samplers)
        all_raw_req_items.extend(all_reqs)
        # Track collection membership for same-collection priority (keyed by id)
        _col_req_ids = set(id(ri) for ri in all_reqs)

        print(f"  ℹ️  Sampler: {len(all_reqs)} requests found in '{sampler_path.name}'")

        for req_item in all_reqs:
            name = req_item.get('name', '')
            r = req_item.get('request', {})
            raw_url = ''
            if isinstance(r.get('url'), dict):
                raw_url = r['url'].get('raw', '')
            elif isinstance(r.get('url'), str):
                raw_url = r['url']

            # Explicit URL-path → slug override (when sampler URL differs from doc filename slug)
            url_last_seg = raw_url.rstrip('/').rsplit('/', 1)[-1] if raw_url else ''
            override = SAMPLER_URL_TO_SLUG.get(url_last_seg)
            if override == '__SKIP__':
                continue  # Explicitly skip this sampler request

            # Multi-scenario: match by sampler request name → virtual slug
            virtual_slug = _SAMPLER_NAME_TO_VIRTUAL.get(name)
            if virtual_slug and virtual_slug in known_slugs:
                slug = virtual_slug
            else:
                # Try slug from URL path (most reliable), with explicit override first
                slug = override or (_url_path_to_slug(raw_url) if raw_url else '')

            # SOAP body-based matching: extract operation name from XML body
            # (high priority for SOAP APIs sharing the same URL endpoint)
            if slug not in known_slugs:
                body_raw_tmp = r.get('body', {}).get('raw', '')
                soap_op = extract_soap_operation(body_raw_tmp)
                if soap_op:
                    # Check explicit SOAP operation override first
                    soap_override = SOAP_OP_TO_SLUG.get(soap_op)
                    if soap_override and soap_override in known_slugs:
                        slug = soap_override
                    else:
                        norm_op = _normalize_slug(soap_op)
                        if norm_op in norm_lookup:
                            slug = norm_lookup[norm_op]
                        else:
                            contained = _best_contains_match(norm_op, norm_lookup)
                            if contained:
                                slug = contained

            if slug not in known_slugs:
                # Try normalised exact match (handles () etc.)
                norm = _normalize_slug(slug)
                if norm in norm_lookup:
                    slug = norm_lookup[norm]
                else:
                    # Try contains: normalised slug ⊂ known or known ⊂ slug
                    contained = _best_contains_match(norm, norm_lookup)
                    if contained:
                        slug = contained
            if slug not in known_slugs:
                # Try from request name
                name_slug = _url_path_to_slug(name)
                norm_name = _normalize_slug(name_slug)
                if norm_name in norm_lookup:
                    slug = norm_lookup[norm_name]
                else:
                    contained = _best_contains_match(norm_name, norm_lookup)
                    if contained:
                        slug = contained
            if slug not in known_slugs:
                # Last resort: concat name+url, check contains
                candidate = _normalize_slug(name + raw_url)
                best = _best_contains_match(candidate, norm_lookup)
                if best:
                    slug = best
            if slug not in known_slugs:
                continue  # no match

            # First-match wins across collections; within same collection,
            # a later item for the same slug becomes the new target (last-match-wins)
            # and the old target is demoted to a folder-level setup prereq.
            if slug in result:
                if slug_to_sampler_path.get(slug) == sampler_path:
                    # Same collection: demote old target → folder prereq, use this one
                    old_req = slug_to_req_item.get(slug)
                    if old_req is not None:
                        slug_to_folder_prereqs.setdefault(slug, []).append(old_req)
                    # Fall through to overwrite result[slug] below
                else:
                    continue  # Different collection: keep first-match wins

            body_raw = r.get('body', {}).get('raw', '{}')
            body_format = 'json'
            body_raw_xml = None
            # Detect SOAP/XML body
            if detect_soap_body(body_raw):
                body_format = 'xml'
                body_raw_xml = body_raw
                body = soap_body_to_flat_dict(body_raw)
                print(f'    ℹ️  Detected SOAP/XML body for "{slug}" ({len(body)} fields)')
            else:
                try:
                    body = json.loads(body_raw)
                except (json.JSONDecodeError, ValueError):
                    # Postman variables like {{var}} without quotes are invalid JSON.
                    # Fix: wrap unquoted {{var}} in double-quotes so json.loads succeeds.
                    fixed = re.sub(r'(?<!")(\{\{[^}]+\}\})(?!")', r'"\1"', body_raw)
                    try:
                        body = json.loads(fixed)
                        print(f'    ℹ️  Fixed unquoted Postman variables in body for "{slug}"')
                    except (json.JSONDecodeError, ValueError):
                        body = {}
            # Extract pre-request script lines (non-empty) from item-level events
            prerequest_lines = []
            for ev in req_item.get('event', []):
                if ev.get('listen') == 'prerequest':
                    raw_lines = [l for l in ev.get('script', {}).get('exec', []) if l.strip()]
                    prerequest_lines = _fix_postman_script_lines(raw_lines, f'"{slug}" prerequest')
            result[slug] = {
                'method':     r.get('method', 'GET'),
                'url':        raw_url,
                'headers':    {h['key']: h['value'] for h in r.get('header', [])},
                'body':       body,
                'body_format': body_format,
                'body_raw_xml': body_raw_xml,
                'prerequest': prerequest_lines,
            }
            slug_to_req_item[slug] = req_item
            slug_to_sampler_path[slug] = sampler_path
            # Record same-collection items for this slug
            if id(req_item) in _col_req_ids:
                slug_to_same_collection_items[slug] = all_reqs

    # ── Resolve variable dependency chains → setup_items ──────────────────
    if all_raw_req_items:
        print(f'\n  [DEPENDENCY RESOLVER] {len(all_raw_req_items)} total sampler requests, '
              f'{len(collection_var_names)} collection variables')
        for slug, data in result.items():
            req_item = slug_to_req_item.get(slug)
            if not req_item:
                continue
            manual_prereqs = MANUAL_PREREQUISITES.get(slug, [])
            # Merge folder prereqs (demoted same-URL earlier items) with auto-resolved chain
            folder_prereqs = slug_to_folder_prereqs.get(slug, [])
            setup_chain = _resolve_setup_chain(
                req_item, all_raw_req_items, collection_var_names,
                forced_endpoints=manual_prereqs,
                same_collection_items=slug_to_same_collection_items.get(slug, []),
                folder_prereqs=folder_prereqs
            )
            if setup_chain:
                data['setup_items'] = setup_chain
                # Collect extra collection variables needed by the chain
                all_vars_needed = set()
                for si in setup_chain:
                    si_body_str = json.dumps(si['request'].get('body', {}))
                    si_url = si['request'].get('url', '')
                    si_headers_str = json.dumps(si['request'].get('headers', {}))
                    text = si_url + '\n' + si_body_str + '\n' + si_headers_str
                    all_vars_needed.update(_VAR_CONSUME_RE.findall(text))
                # Also add vars consumed by the target itself
                all_vars_needed.update(_extract_var_consumes(req_item))
                # Filter to only collection-level vars (the ones not produced by APIs)
                extra_vars = {}
                for sampler_path in SAMPLERS:
                    if not sampler_path.exists():
                        continue
                    with open(sampler_path) as f:
                        col = json.load(f)
                    for v in col.get('variable', []):
                        k = v.get('key', '')
                        if k in all_vars_needed and k not in extra_vars:
                            extra_vars[k] = v.get('value', '')
                if extra_vars:
                    data['extra_variables'] = extra_vars
                setup_names = [si['name'] for si in setup_chain]
                print(f'    {slug}: {len(setup_chain)} setup request(s) → {setup_names}')
            else:
                consumes = _extract_var_consumes(req_item)
                if consumes:
                    # All consumed vars are covered by collection variables
                    extra_vars = {}
                    for sampler_path in SAMPLERS:
                        if not sampler_path.exists():
                            continue
                        with open(sampler_path) as f:
                            col = json.load(f)
                        for v in col.get('variable', []):
                            k = v.get('key', '')
                            if k in consumes and k not in extra_vars:
                                extra_vars[k] = v.get('value', '')
                    if extra_vars:
                        data['extra_variables'] = extra_vars

    return result


# ---------------------------------------------------------------------------
# 8. Main: compare doc contracts vs sampler
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if SAMPLERS:
        sampler = load_sampler()
    else:
        print(f"⚠️  Không tìm thấy collection mẫu nào trong postman/")
        print("   → Bỏ qua so sánh sampler (chỉ parse doc).")
        sampler = {}

    if not HTML_FILES:
        print("\n⚠️  Không tìm thấy file tài liệu nào trong input/")
        print("   → Đặt file .html / .docx / .doc vào thư mục input/ rồi chạy lại.")
        sys.exit(1)

    contracts = {}

    for slug, doc_file_path in HTML_FILES.items():
        print(f'\n{"="*72}')
        print(f'  {slug}  ←  {doc_file_path.name}')
        print(f'{"="*72}')

        is_docx = doc_file_path.suffix.lower() == '.docx'
        response_data_fields: dict = {}  # populated below for HTML

        # ── Parse doc (HTML or DOCX) ─────────────────────────────────────────
        if is_docx:
            docx_contract = parse_docx_file(doc_file_path)
            if docx_contract is None:
                print(f'  ❌  Bỏ qua {doc_file_path.name}')
                continue
            doc_path      = docx_contract['doc_path']
            doc_method    = docx_contract['doc_method']
            active_fields = docx_contract['active_request_fields']
            struck_fields = [{'name': n, 'note': ''} for n in docx_contract['struck_request_fields']]
            active_errors = docx_contract['active_errors']
            struck_errors = [{'code': c, 'key': ''} for c in docx_contract['struck_errors']]
            response_data_fields = docx_contract.get('response_data_fields', {})

            # ── Multi-scenario filtering ─────────────────────────────────────
            if slug in _VIRTUAL_SLUG_MAP:
                _vs = _VIRTUAL_SLUG_MAP[slug]
                _scen = _vs['scenario']
                _label = _scen.get('scenario_label', slug)
                print(f'  ℹ️  Multi-scenario filter: "{_label}"')

                # Filter request fields: keep only this scenario's fields
                _mand_set = set(_scen.get('mandatory_fields', []))
                _opt_set = set(_scen.get('optional_fields', []))
                _excl_set = set(_scen.get('exclude_fields', []))
                _keep_set = _mand_set | _opt_set
                if _keep_set:
                    active_fields = [f for f in active_fields if f['name'] not in _excl_set]
                    # Override mandatory flag per scenario
                    for f in active_fields:
                        if f['name'] in _mand_set:
                            f['mandatory'] = 'Y'
                        elif f['name'] in _opt_set:
                            f['mandatory'] = 'N'

                # Apply fixed field values (e.g. LD.TYPE = EARLY.MATURITY)
                _fixed = _scen.get('fixed_fields', {})
                for f in active_fields:
                    if f['name'] in _fixed:
                        f['note'] = (f.get('note') or '') + f' [fixed: {_fixed[f["name"]]}]'

                # Filter errors: keep only this scenario's errors
                _scen_errors = _scen.get('scenario_errors', [])
                if _scen_errors:
                    _scen_err_set = set(_scen_errors)
                    active_errors = [e for e in active_errors
                                     if e.get('key', '') in _scen_err_set
                                     or e.get('code', '') in _scen_err_set]

                # Update SOAP XML template from scenario's sampler request
                # (will be done via sampler body_raw_xml below)
        else:
            real_html = _cached_decode(doc_file_path)
            soup      = BeautifulSoup(real_html, 'lxml')
            doc_path  = extract_api_name_from_html(soup).rstrip('/')
            doc_method = extract_method(soup) or 'NOT FOUND'
            active_fields, struck_fields = parse_request_table(soup)
            active_errors, struck_errors = parse_error_table(soup)
            response_data_fields = parse_response_fields(soup)

        sam          = sampler.get(slug, {})
        sam_method   = sam.get('method', '?')
        sam_url      = sam.get('url',    '?')
        sam_body     = sam.get('body',   {})
        sam_headers  = sam.get('headers', {})

        # ── Method ──────────────────────────────────────────────────────────
        # doc_method already set above (from HTML soup or DOCX contract)
        print(f'\n  [METHOD]')
        if doc_method != sam_method:
            print(f'  ❌ [DOC ERROR]  doc="{doc_method}"  sampler/thực tế="{sam_method}"')
            print(f'     → Tài liệu khai báo sai. Server thực nhận {sam_method}.')
            print(f'     → Gen dùng {sam_method} (đúng). Doc phải sửa thành POST.')
        else:
            print(f'  ✅  "{doc_method}"  (doc và sampler khớp)')

        # ── Request fields ───────────────────────────────────────────────────
        # active_fields / struck_fields already set above (from HTML or DOCX)
        print(f'\n  [REQUEST FIELDS FROM DOC]')
        if struck_fields:
            print(f'  🗑  Gạch bỏ trong doc — KHÔNG đưa vào gen:')
            for f in struck_fields:
                print(f'       ✗ {f["name"]:28s}  note: {f["note"] or "-"}')
        if active_fields:
            print(f'  ✅  Active — sẽ đưa vào gen:')
            for f in active_fields:
                print(f'       + {f["name"]:28s}  type={f["type"]:12s}  mandatory={f["mandatory"]}')
        else:
            print('  ⚠️   Không parse được request fields từ doc')

        # ── Compare body vs sampler ──────────────────────────────────────────
        active_names = {f['name'] for f in active_fields}
        struck_names  = {f['name'] for f in struck_fields}
        sam_keys      = set(sam_body.keys())

        outdated     = sam_keys & struck_names
        unknown      = sam_keys - active_names - struck_names
        extra_in_doc = active_names - sam_keys

        print(f'\n  [SO SÁNH REQUEST BODY vs SAMPLER]')
        if outdated:
            print(f'  ⚠️  [SAMPLER OUTDATED] Sampler dùng field đã gạch bỏ:')
            for k in sorted(outdated):
                print(f'       - "{k}"')
        if unknown:
            print(f'  ❓  Sampler has fields not found in doc:')
            for k in sorted(unknown):
                print(f'       - "{k}"')
        if extra_in_doc:
            print(f'  ℹ️   Doc có field, sampler bỏ qua (optional):')
            for k in sorted(extra_in_doc):
                print(f'       - "{k}"')
        if not outdated and not unknown and not extra_in_doc:
            print(f'  ✅  Body fields khớp hoàn toàn')

        # ── Error codes ──────────────────────────────────────────────────────
        # active_errors / struck_errors already set above (from HTML or DOCX)
        print(f'\n  [ERROR CODES FROM DOC]')
        if struck_errors:
            print(f'  🗑  Gạch bỏ — KHÔNG gen:')
            for e in struck_errors:
                print(f'       ✗ {e["code"]:22s}  {e["key"]}')
        if active_errors:
            print(f'  ✅  Active ({len(active_errors)}):')
            for e in active_errors:
                print(f'       + {e["code"]:22s}  {e["key"]:35s}  {e["reason"]}')
        else:
            print('  ⚠️   Không parse được error codes')

        # ── Enrich from sampler when doc fields are empty or garbage ────────
        # Heuristic: if all field names are identical (e.g. all "String"), treat
        # as garbage from bad .doc parsing and replace with sampler body.
        _field_names_set = set(f['name'] for f in active_fields)
        _fields_are_garbage = (len(active_fields) > 1 and len(_field_names_set) == 1)
        if (not active_fields or _fields_are_garbage) and sam_body:
            reason = 'Doc fields garbage (all same name)' if _fields_are_garbage else 'Doc fields could not be parsed'
            print(f'\n  [SAMPLER→GEN] {reason} → import {len(sam_body)} field(s) từ sampler body')
            active_fields.clear()
            for field_name, field_val in sam_body.items():
                ftype = 'String'
                if isinstance(field_val, dict):
                    ftype = 'Object'
                elif isinstance(field_val, list):
                    ftype = 'Array'
                elif isinstance(field_val, bool):
                    ftype = 'Boolean'
                elif isinstance(field_val, (int, float)):
                    ftype = 'Number'
                # For Object/Array, store the actual value as JSON string
                # so downstream can reconstruct it
                if ftype in ('Object', 'Array'):
                    note_val = json.dumps(field_val, ensure_ascii=False)
                else:
                    note_val = str(field_val) if field_val else ''
                active_fields.append({
                    'name': field_name,
                    'level': '1',
                    'type': ftype,
                    'mandatory': 'Y',
                    'note': note_val,
                    'struck': False,
                })
                print(f'       + {field_name:28s}  type={ftype:12s}  value={field_val}')

        # ── Store contract ───────────────────────────────────────────────────
        # For docx-parsed APIs, fall back to docx_contract body fields if sampler has none
        _docx_body_format = docx_contract.get('sampler_body_format') if is_docx else None
        _docx_body_raw_xml = docx_contract.get('sampler_body_raw_xml') if is_docx else None
        # Multi-scenario metadata
        _scenario_meta = {}
        if slug in _VIRTUAL_SLUG_MAP:
            _vs_cfg = _VIRTUAL_SLUG_MAP[slug]
            _scenario_meta = {
                'scenario_label': _vs_cfg['scenario'].get('scenario_label', ''),
                'fixed_fields': _vs_cfg['scenario'].get('fixed_fields', {}),
                'base_slug': _vs_cfg['base_slug'],
            }
        contracts[slug] = {
            'method':              sam_method,
            'doc_method':          doc_method,
            'method_is_doc_error': doc_method != sam_method,
            'url':                 sam_url,
            'doc_path':            doc_path,          # e.g. /get-account-transactions
            'is_soap':             docx_contract.get('is_soap', False) if is_docx else False,
            'soap_error_samples':  docx_contract.get('soap_error_samples', []) if is_docx else [],
            'sampler_headers':     sam_headers,        # actual headers from working request
            'sampler_body':        sam_body,           # actual body values from working request
            'sampler_body_format': sam.get('body_format') or _docx_body_format or 'json',
            'sampler_body_raw_xml': sam.get('body_raw_xml') or _docx_body_raw_xml,
            'sampler_prerequest':  sam.get('prerequest', []),  # pre-request script lines from sampler
            'sampler_setup_items': sam.get('setup_items', []),  # auto-resolved dependency chain
            'sampler_extra_variables': sam.get('extra_variables', {}),  # collection vars needed
            'active_request_fields': active_fields,
            'struck_request_fields': [f['name'] for f in struck_fields],
            'active_errors':          active_errors,
            'struck_errors':          [e['code'] for e in struck_errors],
            'response_data_fields':   response_data_fields,  # parsed from doc response section
            'business_conditions':    docx_contract.get('business_conditions', []) if is_docx else [],
            **_scenario_meta,
        }

    # ---------------------------------------------------------------------------
    # 9. Save contracts JSON
    # ---------------------------------------------------------------------------
    out = ROOT / 'scripts' / 'contracts_from_html.json'
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(contracts, f, ensure_ascii=False, indent=2)

    print(f'\n\n{"="*72}')
    print(f'  contracts_from_html.json  →  {out}')
    print('  Chạy tiếp:  python3 scripts/regen_from_contracts.py')
    print(f'{"="*72}')
