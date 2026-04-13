import json

with open(
    "output/bundles/20260310-183426/deposit/deposit_-_an_phu/newman-report.json",
    encoding="utf-8",
) as f:
    data = json.load(f)

ex = data["run"]["executions"][0]
stream = ex.get("response", {}).get("stream", {})
body = bytes(stream["data"]).decode("utf-8") if isinstance(stream, dict) and "data" in stream else ""
parsed = json.loads(body)
print("TOP KEYS:", list(parsed.keys()))
top_key = list(parsed.keys())[0]
print("TOP KEY:", top_key)
inner = parsed[top_key]
print("INNER KEYS:", list(inner.keys()))
body_res = inner.get("bodyRes", {})
print("bodyRes KEYS:", list(body_res.keys()))
t24d = body_res.get("t24StatusDetails")
t24 = body_res.get("t24Status")
print("t24StatusDetails:", t24d)
print("t24Status:", t24)
