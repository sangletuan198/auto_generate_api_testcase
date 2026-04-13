#!/usr/bin/env python3
"""
run_pipeline.py — Unified cross-platform pipeline runner.

CÁCH CHẠY DUY NHẤT (chuẩn hóa cho mọi người):

  python3 run_pipeline.py                    # Bước 0-4: parse → gen → compare
  python3 run_pipeline.py --newman           # Bước 0-6: + Newman + merge
  python3 run_pipeline.py --newman --target corrected   # Chỉ chạy bản corrected

Steps:
  0. Validate inputs (input/, postman/, baseline/coverage_requirements.json)
  1. Auto-install missing dependencies (requirements.txt)
  2. Parse input/ → contracts_from_html.json
  3. Generate outputs (doc_literal + corrected)
  4. Compare gen vs sampler (if sampler exists)
  5. Merge collections → master_collection.json  [MỚI]
  6. Run Newman (--newman flag)                  [MỚI]

User only needs to:
  - Place API spec files (.html / .docx / .doc) in input/
  - Place Postman sampler (optional) in postman/
  - Run this script
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
RUNNER = ROOT / "runner"
DOCS_DIR = ROOT / "input"
POSTMAN_DIR = ROOT / "postman"
REQUIREMENTS = ROOT / "requirements.txt"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _banner(msg: str) -> None:
    width = max(len(msg) + 4, 60)
    print(f"\n{'═' * width}")
    print(f"  {msg}")
    print(f"{'═' * width}\n")


def _step(n: int, msg: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  Bước {n} — {msg}")
    print(f"{'─' * 60}")


def _run_script(script_path: Path) -> int:
    """Run a Python script as a subprocess, streaming output in real-time."""
    cmd = [sys.executable, str(script_path)]
    result = subprocess.run(cmd, cwd=str(ROOT))
    return result.returncode


def _check_dependency(pkg_import: str) -> bool:
    """Check if a package is importable."""
    try:
        importlib.import_module(pkg_import)
        return True
    except ImportError:
        return False


def _count_reqs(path: Path) -> int:
    """Return number of leaf requests in a Postman collection file."""
    try:
        import json as _json
        with open(path, encoding='utf-8') as _f:
            _col = _json.load(_f)
        def _c(items):
            return sum(_c(it['item']) if 'item' in it else 1 for it in items)
        return _c(_col.get('item', []))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Step 0: Validate inputs
# ---------------------------------------------------------------------------

def step0_validate(skip_parse: bool = False) -> bool:
    _step(0, "Kiểm tra đầu vào")

    # Check input/ (only required when parsing)
    if skip_parse:
        contracts_file = SCRIPTS / "contracts_from_html.json"
        if contracts_file.exists():
            print(f"  ✅  input/ — bỏ qua (--skip-parse, dùng {contracts_file.name})")
        else:
            print(f"  ❌  --skip-parse nhưng {contracts_file.name} không tồn tại!")
            return False
    else:
        if not DOCS_DIR.is_dir():
            print("  ❌  Thư mục input/ không tồn tại.")
            print("     → Tạo thư mục input/ và đặt file .html / .docx / .doc vào đó.")
            return False

        doc_files = [
            f for f in DOCS_DIR.iterdir()
            if f.suffix.lower() in ('.html', '.docx', '.doc')
        ]
        if not doc_files:
            print("  ❌  Không có file tài liệu nào trong input/")
            print("     → Đặt ít nhất 1 file .html / .docx / .doc vào input/")
            return False

        print(f"  ✅  input/ — {len(doc_files)} file(s):")
        for f in sorted(doc_files):
            print(f"       • {f.name}")

    # Check postman/ (optional) — pick the most comprehensive collection (most requests)
    sampler = None
    if POSTMAN_DIR.is_dir():
        candidates = [p for p in sorted(POSTMAN_DIR.iterdir())
                      if p.name.endswith('.postman_collection.json')]
        if candidates:
            sampler = max(candidates, key=_count_reqs)
    if sampler:
        print(f"  ✅  sampler: {sampler.name}")
    else:
        print("  ⚠️  Không có sampler → chỉ sinh doc_literal (bỏ corrected)")

    # Check baseline/coverage_requirements.json (optional)
    config_json = ROOT / "baseline" / "coverage_requirements.json"
    if config_json.exists():
        print(f"  ✅  baseline/coverage_requirements.json — có")
    else:
        print("  ⚠️  baseline/coverage_requirements.json không có — dùng ngưỡng mặc định")

    # Check baseline/ folder (required for config-driven generation)
    input_dir = ROOT / "baseline"
    if input_dir.is_dir():
        required_files = ["project_config.json", "categories.json", "common_test_templates.json"]
        missing = [f for f in required_files if not (input_dir / f).exists()]
        if missing:
            print(f"  ❌  Thiếu file trong baseline/: {', '.join(missing)}")
            return False
        # Count optional files
        base_defs = list((input_dir / "base_api_defs").glob("*.json")) if (input_dir / "base_api_defs").is_dir() else []
        specific  = list((input_dir / "api_specific_tests").glob("*.json")) if (input_dir / "api_specific_tests").is_dir() else []
        print(f"  ✅  baseline/ — {len(required_files)} core + {len(base_defs)} base API defs + {len(specific)} specific test files")
    else:
        print("  ❌  Thư mục baseline/ không tồn tại.")
        print("     → Tạo thư mục baseline/ với project_config.json, categories.json, common_test_templates.json")
        return False

    return True


# ---------------------------------------------------------------------------
# Step 1: Install dependencies
# ---------------------------------------------------------------------------

def step1_install_deps() -> bool:
    _step(1, "Cài đặt dependencies")

    # Quick check if all packages already importable
    all_ok = all(_check_dependency(m) for m in ['bs4', 'lxml', 'docx'])
    if all_ok:
        print("  ✅  Tất cả dependencies đã sẵn sàng.")
        return True

    if not REQUIREMENTS.exists():
        print("  ⚠️  requirements.txt không tìm thấy — bỏ qua pip install")
        return True

    print("  📦  Đang cài đặt dependencies...")
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS), "-q"]
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("  ❌  pip install thất bại. Hãy cài thủ công:")
        print(f"     {sys.executable} -m pip install -r requirements.txt")
        return False

    print("  ✅  Dependencies đã cài xong.")
    return True


# ---------------------------------------------------------------------------
# Step 2: Parse HTML docs → contracts
# ---------------------------------------------------------------------------

def step2_parse() -> bool:
    _step(2, "Parse input/ → contracts_from_html.json")
    rc = _run_script(SCRIPTS / "parse_html_docs.py")
    if rc != 0:
        print("  ❌  parse_html_docs.py thất bại (exit code {})".format(rc))
        return False
    print("\n  ✅  Parse xong.")
    return True


# ---------------------------------------------------------------------------
# Step 3: Generate outputs
# ---------------------------------------------------------------------------

def step3_generate() -> bool:
    _step(3, "Sinh output (doc_literal + corrected)")
    rc = _run_script(SCRIPTS / "regen_from_contracts.py")
    if rc != 0:
        print("  ❌  regen_from_contracts.py thất bại (exit code {})".format(rc))
        return False
    print("\n  ✅  Generate xong.")
    return True


# ---------------------------------------------------------------------------
# Step 4: Compare sampler
# ---------------------------------------------------------------------------

def step4_compare() -> bool:
    _step(4, "So sánh gen vs sampler")

    # Check if sampler exists (pick largest collection)
    sampler = None
    if POSTMAN_DIR.is_dir():
        candidates = [p for p in sorted(POSTMAN_DIR.iterdir())
                      if p.name.endswith('.postman_collection.json')]
        if candidates:
            sampler = max(candidates, key=_count_reqs)

    if not sampler:
        print("  ⏭️  Không có sampler → bỏ qua so sánh.")
        return True

    rc = _run_script(SCRIPTS / "compare_sampler.py")
    if rc != 0:
        print("  ❌  compare_sampler.py thất bại (exit code {})".format(rc))
        return False
    print("\n  ✅  So sánh xong.")
    return True


# ---------------------------------------------------------------------------
# Step 5: Merge collections → master_collection.json
# ---------------------------------------------------------------------------

def step5_merge() -> bool:
    _step(5, "Merge collections → master_collection.json")
    merge_script = SCRIPTS / "merge_all_collections.py"
    if not merge_script.exists():
        print("  ⏭️  merge_all_collections.py không có → bỏ qua.")
        return True
    rc = _run_script(merge_script)
    if rc != 0:
        print("  ❌  merge_all_collections.py thất bại (exit code {})".format(rc))
        return False
    print("\n  ✅  Merge xong.")
    return True


# ---------------------------------------------------------------------------
# Step 6: Run Newman (optional, requires --newman flag)
# ---------------------------------------------------------------------------

def step6_newman(target: str = "corrected") -> bool:
    _step(6, f"Chạy Newman — target={target}")

    # Check Newman installed
    newman_path = shutil.which("newman")
    if not newman_path:
        print("  ❌  Newman chưa cài. Cài bằng: npm i -g newman")
        print("     (Tùy chọn: npm i -g newman-reporter-htmlextra)")
        return False

    bundle_script = RUNNER / "run_and_bundle.sh"
    if not bundle_script.exists():
        print("  ❌  runner/run_and_bundle.sh không tìm thấy.")
        return False

    cmd = ["bash", str(bundle_script), target]
    print(f"  🔄  bash runner/run_and_bundle.sh {target}")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"  ❌  Newman có test case FAIL (exit code {result.returncode})")
        print("     → Xem chi tiết trong output/bundles/<timestamp>/")
        # Don't return False — failed assertions are expected (testing negative cases)
        # Pipeline continues, user checks report
    print("\n  ✅  Newman chạy xong. Report trong output/bundles/")
    return True


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(elapsed: float) -> None:
    _banner("KẾT QUẢ PIPELINE")

    output_dir = ROOT / "output"
    if not output_dir.exists():
        print("  ⚠️  Thư mục output/ chưa được tạo.")
        return

    for group in ["doc_literal", "corrected"]:
        group_dir = output_dir / group
        if not group_dir.is_dir():
            continue
        apis = sorted(
            d.name for d in group_dir.iterdir()
            if d.is_dir() and not d.name.startswith('.')
        )
        if not apis:
            continue
        # Count total TCs from CSV files
        total_tcs = 0
        for api in apis:
            csv_files = list((group_dir / api).glob("TestCases_*.csv"))
            for csv_f in csv_files:
                # Count non-header lines
                lines = csv_f.read_text(encoding='utf-8').strip().split('\n')
                total_tcs += max(0, len(lines) - 1)

        collections = list(group_dir.rglob("*_Postman_Collection.json"))

        # Count DIFF_REPORTs per API (correct path)
        diff_count = sum(
            1 for api in apis
            if (group_dir / api / "DIFF_REPORT.md").exists()
        )

        print(f"  📁 output/{group}/")
        print(f"     • {len(apis)} API(s): {', '.join(apis)}")
        print(f"     • {total_tcs} test cases")
        print(f"     • {len(collections)} Postman collection(s)")
        if diff_count > 0:
            print(f"     • {diff_count} DIFF_REPORT(s) ✅")

    # Master collection
    master = output_dir / "master_collection.json"
    if master.exists():
        print(f"  📁 output/master_collection.json ✅")

    # Latest bundle
    bundles_dir = output_dir / "bundles"
    if bundles_dir.is_dir():
        latest = sorted(bundles_dir.iterdir())
        if latest:
            print(f"  📁 output/bundles/ — {len(latest)} bundle(s), latest: {latest[-1].name}")

    print(f"\n  ⏱️  Tổng thời gian: {elapsed:.1f}s")
    print(f"  🐍 Python: {sys.executable}")
    print()


# ---------------------------------------------------------------------------
# CLI Argument Parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="API Test Case Generator — Pipeline Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ví dụ:
  python3 run_pipeline.py                          # Bước 0-5 (không chạy Newman)
  python3 run_pipeline.py --newman                 # Bước 0-6 (có chạy Newman)
  python3 run_pipeline.py --newman --target corrected
  python3 run_pipeline.py --skip-parse             # Bỏ qua bước parse (dùng contracts cũ)
        """,
    )
    parser.add_argument(
        "--newman", action="store_true",
        help="Chạy Newman sau khi generate (Bước 6)",
    )
    parser.add_argument(
        "--target", choices=["corrected", "doc_literal"],
        default="corrected",
        help="Target để chạy Newman (default: corrected)",
    )
    parser.add_argument(
        "--skip-parse", action="store_true",
        help="Bỏ qua bước parse — dùng contracts_from_html.json hiện có",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    _banner("API Test Case Generator — Pipeline Runner")

    t0 = time.time()

    # Step 0: Validate
    if not step0_validate(skip_parse=args.skip_parse):
        return 1

    # Step 1: Install deps
    if not step1_install_deps():
        return 1

    # Step 2: Parse
    if args.skip_parse:
        contracts_file = SCRIPTS / "contracts_from_html.json"
        if contracts_file.exists():
            print(f"\n  ⏭️  Bỏ qua parse (--skip-parse). Dùng {contracts_file.name} hiện có.")
        else:
            print(f"\n  ❌  --skip-parse nhưng {contracts_file.name} không tồn tại!")
            return 1
    else:
        if not step2_parse():
            return 1

    # Step 3: Generate
    if not step3_generate():
        return 1

    # Step 4: Compare
    if not step4_compare():
        return 1

    # Step 5: Merge
    if not step5_merge():
        return 1

    # Step 6: Newman (optional)
    if args.newman:
        step6_newman(target=args.target)

    # Summary
    elapsed = time.time() - t0
    print_summary(elapsed)

    _banner("PIPELINE HOÀN TẤT ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
