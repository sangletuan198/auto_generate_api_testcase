import json

with open(
    "output/bundles/20260310-183426/deposit/deposit_-_truyen_thong/newman-report.json",
    encoding="utf-8",
) as f:
    data = json.load(f)

for i, ex in enumerate(data["run"]["executions"][:5]):
    resp = ex.get("response", {})
    stream = resp.get("stream", {})
    body = bytes(stream["data"]).decode("utf-8") if isinstance(stream, dict) and "data" in stream else ""
    parsed = json.loads(body)
    body_res = parsed.get("createFDSavingRes", {}).get("bodyRes", {})
    print(f"--- exec {i}: {ex['item']['name']} ---")
    print("  keys in bodyRes:", list(body_res.keys()))
    t24 = body_res.get("t24StatusDetails") or body_res.get("t24Status")
    print("  field found:", "t24StatusDetails" if body_res.get("t24StatusDetails") else "t24Status")
    print("  type:", type(t24).__name__)
    if isinstance(t24, dict):
        print("  application:", t24.get("application"))
        print("  transactionId:", t24.get("transactionId"))
    elif isinstance(t24, list):
        for s in t24:
            print("  - application:", s.get("application"), "| transactionId:", s.get("transactionId"))
    print()
