#!/usr/bin/env python3
"""Enrich contracts_from_html.json with enum values from HTML docs and Postman."""

import json
from pathlib import Path

CONTRACTS_PATH = Path(__file__).parent / "contracts_from_html.json"

with open(CONTRACTS_PATH, encoding="utf-8") as f:
    contracts = json.load(f)


def add_enum_values(slug, field_name, values):
    c = contracts.get(slug)
    if not c:
        print(f"  WARN: {slug} not found")
        return
    for fld in c.get("active_request_fields", []):
        if fld["name"] == field_name:
            existing = set(fld.get("enum_values", []))
            new_vals = [v for v in values if v not in existing]
            fld["enum_values"] = list(set(fld.get("enum_values", [])) | set(values))
            if new_vals:
                print(f"  + {slug}.{field_name}: added {new_vals}")
            else:
                print(f"  = {slug}.{field_name}: already complete")
            return
    print(f"  WARN: {slug}.{field_name} not found")


def add_contract_enums(slug, enums_dict):
    c = contracts.get(slug)
    if not c:
        return
    if "enums" not in c:
        c["enums"] = {}
    for k, v in enums_dict.items():
        existing = set(c["enums"].get(k, []))
        new_vals = [val for val in v if val not in existing]
        c["enums"][k] = list(set(c["enums"].get(k, [])) | set(v))
        if new_vals:
            print(f"  + {slug}.enums.{k}: added {new_vals}")


# 1. openFlexSaving
add_enum_values("openFlexSaving", "maturityInstruction", ["P", "PI", "N"])
add_enum_values("openFlexSaving", "product", [
    "6301", "ANPHU.AR", "ANPHU.ML", "ANPHU.QL",
    "IB.DEPOSIT.AR", "IB.DEPOSIT.ML", "IB.DEPOSIT.QL",
    "IB.DEPOSIT.SL", "IB.DEPOSIT.YL", "IB.DEPOSIT.RGLH.AR",
])
add_enum_values("openFlexSaving", "currency", ["VND"])

# 2. amendMatSaving
add_enum_values("amendMatSaving", "maturityInstructions", ["P", "N", "PI"])

# 3. Generate Saving Plan
add_enum_values("Generate Saving Plan", "channel", ["MB", "IB", "TB"])
add_enum_values("Generate Saving Plan", "tenor", [
    "1M", "2M", "3M", "4M", "5M", "6M", "7M", "8M", "9M",
    "10M", "11M", "12M", "13M", "15M", "18M", "24M", "30M",
    "36M", "48M", "60M",
])
add_enum_values("Generate Saving Plan", "interestPayoutFrequency", [
    "at_maturity", "monthly", "quarterly", "semi_annually", "yearly",
    "at_the_beginning",
])

# 4. closeFDAPSaving
add_enum_values("closeFDAPSaving", "channel", ["T24", "IB", "MB"])
add_enum_values("closeFDAPSaving", "debitCurrency", ["VND", "USD"])
add_enum_values("closeFDAPSaving", "creditCurrency", ["VND", "USD"])

# 5. getDepositConfirmation
add_enum_values("getDepositConfirmation", "productCode", [
    "IB.DEPOSIT.AR", "IB.DEPOSIT.ML", "IB.DEPOSIT.QL", "IB.DEPOSIT.SL",
    "IB.DEPOSIT.YL", "IB.DEPOSIT.RGLH.AR", "ANPHU.AR", "ANPHU.ML", "ANPHU.QL",
])

# 6. getSavingAccountTransactions
add_enum_values("getSavingAccountTransactions", "type", ["incoming", "outgoing", "All"])

# 7. getSavingAccountHistory (response enums)
add_contract_enums("getSavingAccountHistory", {
    "typeEvent": [
        "ACCOUNT_CREATED", "AMEND_PAYOUT_METHOD", "PLEDGED",
        "AUTO_RENEW", "BLOCK", "TRANSFER", "PLEDGE",
    ],
    "createdBy": ["USER", "SYSTEM", "BRANCH"],
})

# 8. getAccountByCif - multiple example CIF numbers
add_enum_values("getAccountByCif", "cifNo", [
    "10567898", "10139367", "11956533", "10767888",
])

# Write back
with open(CONTRACTS_PATH, "w", encoding="utf-8") as f:
    json.dump(contracts, f, indent=2, ensure_ascii=False)

print("\nDone - contracts_from_html.json enriched.")
