# API Test Case Generator

> Automated pipeline that generates test cases, Postman collections, Excel reports, coverage summaries, and traceability matrices from API specification documents (Confluence HTML, Word `.docx`/`.doc`).

---

## Overview

Built to replace a fully manual QA documentation process across **40+ banking APIs**, this tool parses API specification documents and produces **production-ready test assets** in a single pipeline run.

### Key Metrics

- **~2,900 test cases** generated across 29 REST + 13 SOAP APIs in one run
- **6 deliverables per API**: Postman collection, CSV, Excel (bank template format), coverage report, traceability matrix, diff report
- **~95% reduction** in test case preparation time (from 2–3 days per API to under 5 minutes)
- **Zero code changes** required to onboard a new API — fully config-driven (11 JSON baseline files)

---

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │            run_pipeline.py               │
                    │         (orchestrator / CLI)              │
                    └────────────┬────────────────┬────────────┘
                                 │                │
                 ┌───────────────▼──┐    ┌────────▼───────────┐
                 │   PARSE STAGE    │    │  GENERATE STAGE    │
                 ├──────────────────┤    ├────────────────────┤
                 │ parse_html_docs  │    │ generate_outputs   │
                 │ parse_docx       │──▶ │ regen_from_contracts│
                 │ pull_confluence  │    │ merge_all_collections│
                 │ soapui_parser    │    │ enrich_contracts    │
                 └──────────────────┘    └────────┬───────────┘
                                                  │
                                    ┌─────────────▼──────────────┐
                                    │      VERIFY STAGE          │
                                    ├────────────────────────────┤
                                    │ verify_test_results        │
                                    │ verify_contract_isolation  │
                                    │ compare_sampler            │
                                    │ fill_expected_results      │
                                    └────────────────────────────┘
```

### Core Components

| Script | LOC | Purpose |
|--------|-----|---------|
| `run_pipeline.py` | 449 | Main orchestrator — CLI entry point, stages, flags |
| `parse_html_docs.py` | 2,114 | Confluence HTML → contract JSON (colspan-aware tables, strikethrough detection, 7-priority enum inference) |
| `generate_outputs.py` | 2,262 | Contract → Postman collection + CSV + Excel + coverage report |
| `regen_from_contracts.py` | 1,144 | Batch regeneration orchestrator |
| `parse_docx.py` | 892 | Word `.docx`/`.doc` parser with table extraction |
| `verify_test_results.py` | 653 | Classify Newman results into 4-quadrant analysis |
| `soapui_parser.py` | 429 | SoapUI XML project parser |
| `pull_confluence.py` | 384 | Confluence REST API integration (CQL search, pagination) |
| `compare_sampler.py` | 372 | Diff generated vs sampler Postman collections |
| `fill_expected_results.py` | 354 | Map actual Newman responses back to Excel |
| `soap_body_utils.py` | 332 | SOAP XML body construction and field extraction |
| `merge_all_collections.py` | 330 | Merge per-API collections into master collection |
| `refresh_prerequisites.py` | 205 | BFS-based dependency resolver for API prerequisite chains |

**Total: 23 scripts · ~10,800 LOC**

---

## Technical Highlights

### 1. Multi-Format Document Parser
Handles **4 input formats** (Confluence HTML with storage markup, exported HTML, Word `.docx`, SoapUI XML) with:
- Colspan-aware table extraction
- Strikethrough field detection (deprecated fields)
- 7-priority enum inference from specification text
- Vietnamese keyword filtering for noise reduction

### 2. BFS Dependency Resolver
Automatically determines prerequisite API call chains (e.g., "create account" before "get balance") and generates pre-request scripts that execute dependencies in correct order — eliminating manual setup configuration.

### 3. Auto-Generated Test Assertions
Embeds JavaScript assertions directly into each Postman request:
- HTTP status code validation
- Response structure verification (envelope + data fields)
- Field type checking (string, number, array, object)
- Enum value validation against known sets
- Response timing constraints

### 4. Config-Driven Architecture
All behavior controlled via **11 JSON baseline files** — adding a new API requires only dropping its spec document into `input/` and running the pipeline. No code changes needed.

### 5. Six Deliverables Per API

| Output | Format | Description |
|--------|--------|-------------|
| Postman Collection | `.json` (v2.1) | Ready-to-run via Newman with embedded assertions |
| CSV Test Cases | `.csv` | Flat test case list for import into test management tools |
| Excel Report | `.xlsx` | Bank template format with test case details |
| Coverage Report | `.json` | Field coverage analysis against specification |
| Traceability Matrix | `.xlsx` | Requirement → test case mapping |
| Diff Report | `.json` | Delta between generated and existing sampler collections |

---

## Quick Start

### Prerequisites

- Python 3.10+
- Newman (optional, for running generated collections)

### Installation

```bash
pip install -r requirements.txt
npm install -g newman  # optional
```

### Usage

```bash
# 1. Place API spec documents in input/
#    Supported: .html (Confluence), .docx/.doc (Word)

# 2. (Optional) Place existing Postman samplers in postman/

# 3. Run the pipeline
python3 run_pipeline.py

# 4. Find outputs in output/
#    output/<api-slug>/
#    ├── collection.json     (Postman v2.1)
#    ├── testcases.csv
#    ├── testcases.xlsx      (bank template)
#    ├── coverage.json
#    ├── traceability.xlsx
#    └── diff.json
```

### Pipeline Flags

```bash
python3 run_pipeline.py --help

# Common options:
#   --parse-only          Only parse docs, don't generate outputs
#   --regen               Regenerate from existing contracts
#   --api <slug>          Process a single API only
#   --skip-verify         Skip verification stage
#   --merge               Merge all collections into master
```

---

## Configuration

All configuration lives in `baseline/`:

| File | Purpose |
|------|---------|
| `project_config.json` | Base URL, standard headers, error codes, response envelope |
| `confluence_config.json` | Confluence connection settings (URL, auth, CQL filters) |
| `categories.json` | Test case category definitions and classification rules |
| `common_test_templates.json` | Reusable test templates (auth, headers, methods, edge cases) |
| `coverage_requirements.json` | KPI thresholds and minimum coverage targets |
| `excel_template.json` | Excel output column mapping and formatting |
| `known_enums.json` | Known enum values for enrichment |
| `table_detection.json` | HTML table detection heuristics |
| `multi_scenario_soap.json` | Multi-scenario SOAP operation configurations |
| `sampler_url_overrides.json` | URL slug overrides for sampler matching |

---

## Project Structure

```
.
├── run_pipeline.py              # Main orchestrator
├── requirements.txt             # Python dependencies
├── baseline/                    # Configuration files (11 JSON)
├── scripts/                     # Pipeline modules (23 Python files)
├── input/                       # API spec documents (not committed)
├── postman/                     # Existing Postman samplers (not committed)
├── output/                      # Generated outputs (not committed)
└── docs/                        # Confluence HTML exports (not committed)
```

---

## Tech Stack

- **Python 3** — core pipeline language
- **BeautifulSoup4 + lxml** — HTML parsing
- **python-docx** — Word document parsing
- **openpyxl** — Excel generation
- **Postman v2.1** — collection output format
- **Newman** — automated collection execution
- **Confluence REST API** — document ingestion

---

## License

This project is for portfolio demonstration purposes. The pipeline architecture and code are original work; API specification documents and test data are not included.
