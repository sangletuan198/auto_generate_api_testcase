import json

NEW_URL = "https://esb-dev.example-bank.com/esbuat7801/openSavingService/v1/createFLexSaving"

NEW_HEADERS = [
    {"key": "Content-Type", "value": "application/json"}
]

PRE_REQ_SCRIPT = [
    "function randomString(length) {",
    "    let chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';",
    "    let result = '';",
    "    for (let i = 0; i < length; i++) {",
    "        result += chars.charAt(Math.floor(Math.random() * chars.length));",
    "    }",
    "    return result;",
    "}",
    "",
    "let random20 = randomString(20);",
    "pm.variables.set(\"randomValue\", random20);",
]

POST_REQ_SCRIPT = [
    "var responseJSON = pm.response.json();",
    "",
    "// Extract bookNumber from passBookInfo",
    "var bookNumber;",
    "var passBookInfo = responseJSON &&",
    "    responseJSON.createFLexSavingRes &&",
    "    responseJSON.createFLexSavingRes.bodyRes &&",
    "    responseJSON.createFLexSavingRes.bodyRes.passBookInfo;",
    "if (passBookInfo) {",
    "    bookNumber = passBookInfo.bookNumber;",
    "}",
    "",
    "pm.variables.set(\"bookNumber\", bookNumber);",
    "",
    "// Verify bookNumber is not null",
    "pm.test(\"bookNumber is not null\", function() {",
    "    pm.expect(bookNumber).to.not.be.undefined;",
    "    pm.expect(bookNumber).to.not.be.null;",
    "    pm.expect(bookNumber).to.not.equal(\"\");",
    "});",
]

# term values per item name (from Excel-sourced cases)
TERM_MAP = {
    "mở sổ - 03 tháng": "3M",
    "mở sổ - 06 tháng": "6M",
    "mở sổ - 1 năm": "12M",
    "mở sổ - 2 năm": "24M",
    "mở sổ - 3 năm": "36M",
    "mở sổ - 4 năm": "48M",
    "mở sổ - 5 năm": "60M",
}


def make_body(term):
    return json.dumps(
        {
            "createFLexSavingReq": {
                "header": {
                    "common": {
                        "serviceVersion": "1",
                        "messageId": "{{randomValue}}",
                        "transactionId": "{{randomValue}}",
                        "messageTimestamp": "1773045047",
                    },
                    "client": {
                        "sourceAppID": "MB",
                        "targetAppIDs": "T24",
                        "userDetail": {"userID": "MB", "userPassword": "RUJBTksxMjM="},
                    },
                },
                "bodyReq": {
                    "functionCode": "SAVING-CREATEFLEXSAVING-SOAP-T24",
                    "customer": {"cif": "12038423"},
                    "debitAccount": {"acctNo": "103053582684"},
                    "passBook": {
                        "term": term,
                        "amount": "1000000",
                        "marginRate": "0",
                    },
                    "traceNumber": "{{randomValue}}",
                },
            }
        },
        ensure_ascii=False,
        indent=4,
    )


with open("postman/deposit - tích lỹ.postman_collection.json", encoding="utf-8") as f:
    col = json.load(f)

for item in col["item"]:
    name = item["name"]
    if name not in TERM_MAP:
        # keep close/approve/nộp thêm untouched
        continue

    term = TERM_MAP[name]

    # Update URL
    item["request"]["url"] = {
        "raw": NEW_URL,
        "protocol": "https",
        "host": ["esb-dev", "example-bank", "com"],
        "path": ["esbuat7801", "openSavingService", "v1", "createFLexSaving"],
    }

    # Update headers
    item["request"]["header"] = NEW_HEADERS

    # Update body
    item["request"]["body"] = {"mode": "raw", "raw": make_body(term)}

    # Replace pre-request script
    for ev in item.get("event", []):
        if ev["listen"] == "prerequest":
            ev["script"]["exec"] = PRE_REQ_SCRIPT

    # Replace/add post-request test script
    item["event"] = [ev for ev in item.get("event", []) if ev["listen"] != "test"]
    item["event"].append(
        {"listen": "test", "script": {"exec": POST_REQ_SCRIPT, "type": "text/javascript"}}
    )

    print(f"Updated: {name} (term={term})")

with open("postman/deposit - tích lỹ.postman_collection.json", "w", encoding="utf-8") as f:
    json.dump(col, f, ensure_ascii=False, indent=2)

print("Done.")
