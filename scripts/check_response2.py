import json

reports = {
    "truyen_thong": "output/bundles/20260310-183426/deposit/deposit_-_truyen_thong/newman-report.json",
    "an_phu": "output/bundles/20260310-183426/deposit/deposit_-_an_phu/newman-report.json",
    "rut_goc": "output/bundles/20260310-183426/deposit/deposit_-_rut_goc_linh_hoạt/newman-report.json",
}

for name, path in reports.items():
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    ok = fail = 0
    fail_names = []
    for ex in data["run"]["executions"]:
        stream = ex.get("response", {}).get("stream", {})
        body = bytes(stream["data"]).decode("utf-8") if isinstance(stream, dict) and "data" in stream else ""
        try:
            parsed = json.loads(body)
            body_res = parsed.get("createFDSavingRes", {}).get("bodyRes", {})
            t24d = body_res.get("t24StatusDetails")
            found = False
            if isinstance(t24d, list):
                for s in t24d:
                    if s.get("application") == "ACCOUNT":
                        found = True
                        break
            if found:
                ok += 1
            else:
                fail += 1
                fail_names.append(ex["item"]["name"])
        except Exception as e:
            fail += 1
            fail_names.append(ex["item"]["name"] + f" (err: {e})")
    print(f"\n=== {name}: {ok} OK, {fail} FAIL (total {ok+fail}) ===")
    for n in fail_names:
        print(f"  FAIL: {n}")
