import json

post_script = [
    "var responseJSON = pm.response.json();",
    "",
    "// Extract transactionId where application is 'ACCOUNT'",
    "var transactionIdValue;",
    "var details = responseJSON && responseJSON.createFDSavingRes && responseJSON.createFDSavingRes.bodyRes && responseJSON.createFDSavingRes.bodyRes.t24StatusDetails;",
    "if (Array.isArray(details)) {",
    "    details.forEach(function(status) {",
    "        if (status.application === 'ACCOUNT') {",
    "            transactionIdValue = status.transactionId;",
    "        }",
    "    });",
    "}",
    "",
    "// Assign the value to the variable",
    "pm.variables.set(\"aaAccount\", transactionIdValue);",
    "",
    "// Verify aaAccount is not null",
    "pm.test(\"aaAccount (transactionId) is not null\", function() {",
    "    pm.expect(transactionIdValue).to.not.be.undefined;",
    "    pm.expect(transactionIdValue).to.not.be.null;",
    "    pm.expect(transactionIdValue).to.not.equal(\"\");",
    "});",
]

fd_files = [
    "postman/deposit - truyền thống.postman_collection.json",
    "postman/deposit - an phú.postman_collection.json",
    "postman/deposit - rút gốc linh hoạt.postman_collection.json",
]
all_files = fd_files + ["postman/deposit - tích lỹ.postman_collection.json"]


def update_prereq(item):
    for ev in item.get("event", []):
        if ev["listen"] == "prerequest":
            exec_lines = ev.get("script", {}).get("exec", [])
            ev["script"]["exec"] = [
                line.replace("pm.collectionVariables.set", "pm.variables.set")
                for line in exec_lines
            ]


def add_postreq(item):
    item["event"] = [ev for ev in item.get("event", []) if ev["listen"] != "test"]
    item["event"].append(
        {
            "listen": "test",
            "script": {"exec": post_script, "type": "text/javascript"},
        }
    )


for f in all_files:
    with open(f, encoding="utf-8") as fp:
        col = json.load(fp)

    for item in col["item"]:
        update_prereq(item)
        if f in fd_files:
            add_postreq(item)

    with open(f, "w", encoding="utf-8") as fp:
        json.dump(col, fp, ensure_ascii=False, indent=2)

    tag = " + post-req added" if f in fd_files else ""
    print(f"Updated {f.split('/')[-1]}: {len(col['item'])} items prereq changed{tag}")
