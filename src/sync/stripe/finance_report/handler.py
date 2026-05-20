"""
Stripe finance report Lambda — runs M-F at 10am Central (15:00 UTC).
For each of 3 Stripe accounts, pulls the most recent unprocessed payout's
balance transactions, categorizes all row types, generates per-account CSVs,
and emails them. Marks each payout processed in RDS so it's never re-sent.
"""

import base64
import csv
import io
import json
import os
import ssl
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import boto3
import pg8000
import stripe

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ACCOUNT_NAMES = {
    'acct_1EJ3grGXzfVc86k5': 'Gymlaunch',
    'acct_1HTxq8DwroTQxWiD': 'Gymlaunch Kajabi',
    'acct_1NdhHHFIzHSkCkYd': 'Gym Launch Go High Level',
}

LOOKUP_TABLE = {
    'auto_recharge':          {'gl_code': '40040', 'intacct_sku': 'GLS-MSGCRD-01'},
    'manual_recharge':        {'gl_code': '40040', 'intacct_sku': 'GLS-MSGCRD-01'},
    'fallback':               {'gl_code': '40040', 'intacct_sku': 'GLS-GHLAPPRS-01'},
    '1b_ads_pack':            {'gl_code': '40080', 'intacct_sku': 'GLS-LT-1BADSPACK-00-01'},
    '192_winning_ads':        {'gl_code': '40080', 'intacct_sku': 'GLS-LT-192ADPACK-00-01'},
    'gl_book':                {'gl_code': '40080', 'intacct_sku': 'GLS-LT-BOOK-00-01'},
    'automated_fulfillment':  {'gl_code': '40050', 'intacct_sku': 'GLS-AUTOFUL-01MO-00100'},
    'trainerize_1':           {'gl_code': '40050', 'intacct_sku': 'GLS-TRAINERIZE-01-00'},
    'trainerize_2':           {'gl_code': '40050', 'intacct_sku': 'GLS-TRAINERIZE-01-01'},
}

CSV_COLUMNS = [
    'Account', 'Type', 'ID', 'Created', 'Description', 'Amount', 'Currency',
    'Converted Amount', 'Fees', 'Net', 'Converted Currency',
    'Customer ID', 'Customer Email', 'Customer Name',
    'GL Code', 'Intacct SKU',
]


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------

def categorize(account_id, description, amount_cents):
    """Account-specific charge categorization, ported from the 3 Zapier steps."""
    desc = description or ''
    if account_id == 'acct_1NdhHHFIzHSkCkYd':
        return _categorize_ghl(desc)
    if account_id == 'acct_1HTxq8DwroTQxWiD':
        return _categorize_kajabi(desc, amount_cents)
    if account_id == 'acct_1EJ3grGXzfVc86k5':
        return _categorize_main(desc, amount_cents)
    return 'unexpected'


def _categorize_ghl(description):
    if description.startswith('Auto-Recharge for Sub-Account -'):
        return 'auto_recharge'
    if description.startswith('Manual Recharge :'):
        return 'manual_recharge'
    if description in (
        'PhoneNumberPurchase - 3DS verification',
        'Subscription update',
        'Add new card: 3DS verification',
    ):
        return 'fallback'
    return 'unexpected'


def _categorize_kajabi(description, amount_cents):
    if description.startswith('Subscription update'):
        if amount_cents in (10000, 5000):   # $100 or $50
            return 'automated_fulfillment'
        if amount_cents == 20000:           # $200
            return 'trainerize_1'
        if amount_cents == 25000:           # $250
            return 'trainerize_2'
    return 'unexpected'


def _categorize_main(description, amount_cents):
    if description.startswith('Gym Launch Secrets LLC'):
        if amount_cents == 2700:    # $27.00
            return '1b_ads_pack'
        if amount_cents == 1999:    # $19.99
            return 'gl_book'
        if amount_cents == 19200:   # $192.00
            return '192_winning_ads'
    return 'unexpected'


def _is_stripe_infrastructure_fee(description):
    """
    Returns True for Stripe's own fee line items that all map to gl_code 50080.
    These descriptions include a date in parentheses that changes each period.
    """
    desc = description or ''
    return (
        desc.startswith('Billing - Usage Fee')
        or desc.startswith('Card Account Updater')
        or desc.startswith('Card payments')
        or desc.startswith('Radar (')
        or desc.startswith('Network Tokens')
    )


def _sku_from_charge(charge, account_id):
    """Look up the intacct_sku of an original charge object (used for refunds/disputes)."""
    if charge is None or isinstance(charge, str):
        return ''   # not expanded — can't determine SKU
    description  = getattr(charge, 'description', '') or ''
    amount_cents = getattr(charge, 'amount', 0)
    category = categorize(account_id, description, amount_cents)
    return LOOKUP_TABLE.get(category, {}).get('intacct_sku', '')


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db_connection():
    ctx = ssl.create_default_context()
    return pg8000.connect(
        host=os.environ['DB_HOST'],
        port=5432,
        database=os.environ['DB_NAME'],
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        ssl_context=ctx,
    )


def get_processed_payout_ids(conn):
    cur = conn.cursor()
    cur.execute('SELECT payout_id FROM stripe_payout_export')
    ids = {row[0] for row in cur.fetchall()}
    cur.close()
    return ids


def mark_payout_processed(conn, payout_id, account_id):
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO stripe_payout_export (payout_id, stripe_account_id)'
        ' VALUES (%s, %s) ON CONFLICT (payout_id) DO NOTHING',
        (payout_id, account_id),
    )
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------

def get_api_keys():
    raw = base64.b64decode(os.environ['STRIPE_API_KEYS_B64']).decode('utf-8')
    return json.loads(raw)


def get_latest_unprocessed_payout(account_id, api_key, processed_ids):
    payouts = stripe.Payout.list(
        limit=1,
        status='paid',
        api_key=api_key,
        stripe_account=account_id,
    )
    if payouts.data and payouts.data[0].id not in processed_ids:
        return payouts.data[0]
    return None


def collect_rows_for_payout(payout_id, account_id, api_key):
    rows = []
    for txn in stripe.BalanceTransaction.list(
        payout=payout_id,
        limit=100,
        expand=[
            'data.source',
            'data.source.customer',         # customer on charges
            'data.source.charge',           # original charge on refunds/disputes
            'data.source.charge.customer',  # customer via original charge
        ],
        api_key=api_key,
        stripe_account=account_id,
    ).auto_paging_iter():
        row = _build_row(txn, account_id)
        if row is not None:
            rows.append(row)
    return rows


def _build_row(txn, account_id):
    # Drop the payout transfer row — it's the bank transfer itself, not a transaction.
    if txn.type == 'payout':
        return None

    source = txn.source

    # Refunds use the original charge ID to match Stripe's export convention.
    # Adjustments (chargebacks) keep their own ad_ ID — Stripe's export shows that.
    if txn.type == 'refund' and hasattr(source, 'charge'):
        charge = source.charge
        source_id = charge.id if hasattr(charge, 'id') else (charge if isinstance(charge, str) else '')
    elif hasattr(source, 'id'):
        source_id = source.id
    elif isinstance(source, str):
        source_id = source
    else:
        source_id = ''

    created  = datetime.fromtimestamp(txn.created, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    amount   = txn.amount / 100
    fees     = txn.fee / 100
    net      = txn.net / 100
    currency = txn.currency.upper()
    desc     = txn.description or ''

    # --- Customer info ---
    customer_id = customer_email = customer_name = ''

    def _extract_customer(customer_obj):
        nonlocal customer_id, customer_email, customer_name
        if hasattr(customer_obj, 'id'):
            customer_id    = customer_obj.id or ''
            customer_email = getattr(customer_obj, 'email', '') or ''
            customer_name  = getattr(customer_obj, 'name', '') or ''
        elif isinstance(customer_obj, str):
            customer_id = customer_obj

    if txn.type == 'charge' and hasattr(source, 'customer'):
        _extract_customer(source.customer)
    elif txn.type in ('refund', 'adjustment') and hasattr(source, 'charge'):
        charge = source.charge
        if hasattr(charge, 'customer'):
            _extract_customer(charge.customer)

    # --- GL code + SKU ---
    gl_code = intacct_sku = ''

    if txn.type == 'charge':
        category = categorize(account_id, desc, txn.amount)
        if category != 'unexpected':
            lookup      = LOOKUP_TABLE[category]
            gl_code     = lookup['gl_code']
            intacct_sku = lookup['intacct_sku']
        else:
            print(f"UNMATCHED charge: account={account_id} desc={desc!r} amount_cents={txn.amount}")

    elif txn.type == 'refund':
        gl_code     = '48100'
        charge      = getattr(source, 'charge', None) if hasattr(source, 'charge') else None
        intacct_sku = _sku_from_charge(charge, account_id)

    elif txn.type == 'adjustment' and any(
        desc.lower().startswith(p) for p in ('dispute', 'chargeback')
    ):
        gl_code     = '48000'
        charge      = getattr(source, 'charge', None) if hasattr(source, 'charge') else None
        intacct_sku = _sku_from_charge(charge, account_id)

    elif _is_stripe_infrastructure_fee(desc) or txn.type == 'stripe_fee':
        gl_code     = '50080'
        intacct_sku = ''

    else:
        print(f"UNMATCHED row: account={account_id} type={txn.type!r} desc={desc!r}")

    txn_type = (getattr(txn, 'reporting_category', None) or txn.type or '').replace('_', ' ').capitalize()

    return {
        'Account':            ACCOUNT_NAMES.get(account_id, account_id),
        'Type':               txn_type,
        'ID':                 source_id,
        'Created':            created,
        'Description':        desc,
        'Amount':             f'{amount:.2f}',
        'Currency':           currency,
        'Converted Amount':   f'{amount:.2f}',
        'Fees':               f'{fees:.2f}',
        'Net':                f'{net:.2f}',
        'Converted Currency': currency,
        'Customer ID':        customer_id,
        'Customer Email':     customer_email,
        'Customer Name':      customer_name,
        'GL Code':            gl_code,
        'Intacct SKU':        intacct_sku,
    }


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def generate_csv(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Delivery — SES email with one attachment per account
# ---------------------------------------------------------------------------

def deliver_report(csvs_by_account, skipped_accounts, date_str):
    from_address = os.environ['SES_FROM_ADDRESS']
    to_address   = os.environ['SES_TO_ADDRESS']

    body_lines = ['Attached are the Stripe finance reports for today.\n']
    for name, _, payout_date in csvs_by_account:
        body_lines.append(f'  + {name} (payout date: {payout_date})')
    for name in skipped_accounts:
        body_lines.append(f'  - {name}: no new payout, skipped')
    body_lines.append('')

    msg = MIMEMultipart()
    msg['Subject'] = f'Stripe Finance Report — {date_str}'
    msg['From']    = from_address
    msg['To']      = to_address
    msg.attach(MIMEText('\n'.join(body_lines), 'plain'))

    for account_name, csv_content, payout_date in csvs_by_account:
        safe_name  = account_name.lower().replace(' ', '_')
        filename   = f'stripe_{safe_name}_{payout_date}.csv'
        attachment = MIMEBase('text', 'csv')
        attachment.set_payload(csv_content.encode('utf-8'))
        encoders.encode_base64(attachment)
        attachment.add_header('Content-Disposition', 'attachment', filename=filename)
        msg.attach(attachment)

    boto3.client('ses', region_name='us-east-1').send_raw_email(
        Source=from_address,
        Destinations=[to_address],
        RawMessage={'Data': msg.as_string()},
    )
    print(f'Finance report emailed from {from_address} to {to_address} — {len(csvs_by_account)} attachment(s)')


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    print("Stripe finance report starting")

    api_keys = get_api_keys()
    conn = get_db_connection()
    try:
        processed_ids    = get_processed_payout_ids(conn)
        csvs_by_account  = []
        skipped_accounts = []
        newly_processed  = []

        for account_id, account_name in ACCOUNT_NAMES.items():
            api_key = api_keys.get(account_id)
            if not api_key:
                print(f"No API key configured for {account_name}, skipping")
                skipped_accounts.append(account_name)
                continue

            payout = get_latest_unprocessed_payout(account_id, api_key, processed_ids)
            if not payout:
                print(f"{account_name}: latest payout already processed, skipping")
                skipped_accounts.append(account_name)
                continue

            print(f"{account_name}: processing payout {payout.id}")
            rows = collect_rows_for_payout(payout.id, account_id, api_key)
            csv_content = generate_csv(rows)

            preview = '\n'.join(csv_content.splitlines()[:11])
            print(f"{account_name} CSV preview (header + first 10 rows):\n{preview}")

            payout_date = datetime.fromtimestamp(payout.arrival_date, tz=timezone.utc).strftime('%Y-%m-%d')
            csvs_by_account.append((account_name, csv_content, payout_date))
            newly_processed.append((payout.id, account_id))
            print(f"{account_name}: {len(rows)} transactions")

        if not csvs_by_account:
            print("No new payouts across any account — exiting without sending")
            return {'statusCode': 200, 'body': 'no new payouts'}

        run_date = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')
        deliver_report(csvs_by_account, skipped_accounts, run_date)

        # Mark processed only after successful delivery so a retry is safe on failure.
        for payout_id, account_id in newly_processed:
            mark_payout_processed(conn, payout_id, account_id)

        total_rows = sum(len(c.splitlines()) - 1 for _, c, _ in csvs_by_account)
        print(f"Done — {total_rows} rows across {len(newly_processed)} payout(s)")
        return {'statusCode': 200, 'body': f'{total_rows} rows sent'}

    finally:
        conn.close()
