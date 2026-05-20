"""
One-off test script — verifies a Stripe restricted key can pull the payout
and balance transactions the finance report Lambda needs.
No third-party libraries required.

Usage:
    python3 scripts/test_stripe_pull.py
"""

import base64
import json
import urllib.parse
import urllib.request

# --- Fill these in ---
API_KEY    = ""
ACCOUNT_ID = ""
# ---------------------

AUTH = base64.b64encode(f"{API_KEY}:".encode()).decode()
HEADERS = {
    "Authorization": f"Basic {AUTH}",
    "Stripe-Account": ACCOUNT_ID,
}


def stripe_get(path, params=None):
    url = f"https://api.stripe.com/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}")
        raise


print(f"\nTesting account: {ACCOUNT_ID}")
print("-" * 60)

# 1. Get the most recent paid payout
result = stripe_get("payouts", {"limit": 1, "status": "paid"})
if not result["data"]:
    print("ERROR: No paid payouts found")
    raise SystemExit(1)

payout = result["data"][0]
print(f"Latest payout: {payout['id']}")
print(f"  Amount:  ${payout['amount'] / 100:.2f} {payout['currency'].upper()}")
print(f"  Arrived: {payout['arrival_date']}")
print()

# 2. Pull balance transactions for that payout (first 5 only)
print("Balance transactions (first 5):")
result = stripe_get("balance_transactions", {
    "payout": payout["id"],
    "limit": 5,
    "expand[]": "data.source",
})
for txn in result["data"]:
    source = txn.get("source") or {}
    source_id = source.get("id", str(source)) if isinstance(source, dict) else str(source)
    customer = source.get("customer") or {}
    customer_id    = customer.get("id", customer) if isinstance(customer, dict) else customer
    customer_email = customer.get("email", "") if isinstance(customer, dict) else ""
    print(f"  {source_id:<40} type={txn['type']:<12} amount=${txn['amount'] / 100:.2f}  customer={customer_id or '—'} {customer_email}")

print()
print("OK — key has the access it needs.")
