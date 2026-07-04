#!/usr/bin/env python3
"""Fetch monthly receipts and email them to Emburse (receipt@ca1.chromeriver.com).

  python3 receipts.py classpass --month 2026-05 --url <stripe_receipt_url>
  python3 receipts.py classpass --month 2026-05            # cached URL or existing PDF
  python3 receipts.py tmobile   --month 2026-05            # downloads via Chrome session
  python3 receipts.py <provider> --month 2026-05 --send    # actually email to Emburse

Without --send the script only fetches PDFs and prints the send plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import smtplib
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

EMBURSE_EMAIL = "receipt@ca1.chromeriver.com"

CLASSPASS_DIR = Path.home() / ".classpass-automation"
CLASSPASS_URL_CACHE = CLASSPASS_DIR / "stripe-urls.json"
TMOBILE_DIR = Path.home() / ".tmobile-receipt-automation"
CHROME_PROFILE = Path.home() / "Library/Application Support/Google/Chrome/Default"

TMOBILE_BILL_SUMMARY_URL = "https://www.t-mobile.com/self-service-pub/v1/bill-summary"
TMOBILE_PDF_URL = "https://www.t-mobile.com/self-service-britebill/billing/v2/billdetails"


def month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def previous_month() -> str:
    last = datetime.today().replace(day=1) - timedelta(days=1)
    return last.strftime("%Y-%m")


# --- PDF generation (Stripe receipt URLs are public; headless Playwright works) ---

def render_url_to_pdf(url: str, pdf_path: Path) -> Path:
    from playwright.sync_api import sync_playwright

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Rendering PDF: {pdf_path}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=30_000)
        time.sleep(3)
        page.pdf(
            path=str(pdf_path),
            format="Letter",
            print_background=True,
            margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
        )
        browser.close()
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        sys.exit(f"ERROR: rendered PDF is empty or missing: {pdf_path}")
    print(f"  Saved ({pdf_path.stat().st_size:,} bytes)")
    return pdf_path


# --- ClassPass ---

def classpass_pdf(month: str, url: str | None) -> Path:
    pdf_path = CLASSPASS_DIR / "receipts" / f"classpass-receipt-{month}.pdf"
    cache = json.loads(CLASSPASS_URL_CACHE.read_text()) if CLASSPASS_URL_CACHE.exists() else {}

    if url:
        cache[month] = url
        CLASSPASS_URL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CLASSPASS_URL_CACHE.write_text(json.dumps(cache, indent=2))
        return render_url_to_pdf(url, pdf_path)
    if pdf_path.exists():
        print(f"Using existing PDF: {pdf_path}")
        return pdf_path
    if month in cache:
        print(f"Using cached Stripe URL for {month_label(month)}")
        return render_url_to_pdf(cache[month], pdf_path)

    sys.exit(
        f"No Stripe URL or PDF for {month_label(month)}.\n"
        "Open https://classpass.com/settings/charges in Chrome, click the charge,\n"
        "copy the pay.stripe.com receipt URL, then rerun with --url '<stripe_url>'.\n"
        "(ClassPass blocks automated browsers; use a real Chrome session.)"
    )


# --- T-Mobile (reads the logged-in Chrome session's cookies; never prints them) ---

def _chrome_cookie_header() -> str:
    for candidate in (CHROME_PROFILE / "Cookies", CHROME_PROFILE / "Network" / "Cookies"):
        if candidate.exists():
            cookie_db = candidate
            break
    else:
        raise FileNotFoundError(f"No Chrome Cookies DB under {CHROME_PROFILE}")

    with tempfile.TemporaryDirectory() as tmpdir:
        copied = Path(tmpdir) / "Cookies"
        copied.write_bytes(cookie_db.read_bytes())
        with sqlite3.connect(copied) as conn:
            rows = conn.execute(
                "SELECT host_key, name, encrypted_value, value FROM cookies"
                " WHERE host_key LIKE '%t-mobile.com%'"
            ).fetchall()

    password = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-a", "Chrome", "-s", "Chrome Safe Storage"],
        stderr=subprocess.DEVNULL,
    ).rstrip(b"\n")
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)

    pairs = []
    for host, name, encrypted_value, value in rows:
        if not value and encrypted_value:
            value = _decrypt_chrome_cookie(host, bytes(encrypted_value), key)
        if value:
            pairs.append(f"{name}={value}")
    if not pairs:
        raise RuntimeError("No usable T-Mobile cookies in the Chrome profile. Log in to t-mobile.com in Chrome first.")
    return "; ".join(pairs)


def _decrypt_chrome_cookie(host: str, encrypted_value: bytes, key: bytes) -> str | None:
    blob = encrypted_value[3:] if encrypted_value.startswith(b"v10") else encrypted_value
    process = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", (b" " * 16).hex()],
        input=blob,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.returncode != 0:
        return None
    decrypted = process.stdout
    host_digest = hashlib.sha256(host.encode()).digest()
    if decrypted.startswith(host_digest):
        decrypted = decrypted[32:]
    try:
        value = decrypted.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if any(ord(ch) > 255 for ch in value) else value


def _post(url: str, body: str | dict, headers: dict[str, str]) -> tuple[int, str, bytes]:
    payload = (json.dumps(body) if isinstance(body, dict) else body).encode()
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.status, response.headers.get("content-type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("content-type", ""), error.read()


def tmobile_pdf(month: str) -> Path:
    pdf_path = TMOBILE_DIR / "receipts" / f"tmobile-receipt-{month}.pdf"
    if pdf_path.exists():
        print(f"Using existing PDF: {pdf_path}")
        return pdf_path

    cookie_header = _chrome_cookie_header()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://www.t-mobile.com",
        "Referer": "https://www.t-mobile.com/bill/summary",
        "Cookie": cookie_header,
    }

    status, content_type, body = _post(TMOBILE_BILL_SUMMARY_URL, "action=getSessionData", headers)
    if status != 200:
        raise RuntimeError(
            f"T-Mobile session request failed ({status} {content_type}). "
            "The Chrome session likely expired — log in at t-mobile.com and retry."
        )
    session = json.loads(body)["sessionData"]
    token = session["accessTokenSAAS"]
    authorization = token if token.startswith("Bearer") else f"Bearer {token}"
    headers.update(
        {
            "puid": session["PUID"],
            "ban": session["BAN"],
            "Authorization": authorization,
            "x-auth-originator": authorization.replace("Bearer ", ""),
        }
    )

    status, content_type, body = _post(
        TMOBILE_BILL_SUMMARY_URL, "action=getBillList&isBillCycleGroupByYear=true", headers
    )
    if status != 200:
        raise RuntimeError(f"T-Mobile bill list request failed ({status} {content_type})")
    bills = [bill for group in json.loads(body).get("getBillList", []) for bill in group.get("bills", [])]

    match = next(
        (
            bill
            for bill in bills
            if bill.get("documentId")
            and datetime.fromisoformat(bill["endTime"]).strftime("%Y-%m") == month
        ),
        None,
    )
    if not match:
        available = sorted(
            datetime.fromisoformat(b["endTime"]).strftime("%Y-%m") for b in bills if b.get("documentId")
        )
        sys.exit(f"No T-Mobile bill found for {month_label(month)}. Available: {', '.join(available)}")

    print(f"Downloading T-Mobile bill for {month_label(month)}")
    print(f"  Cycle: {match.get('cycleValue')} | Amount: {match.get('currentCharges')}")
    pdf_headers = {
        **{k: headers[k] for k in ("User-Agent", "Origin", "Cookie", "Authorization", "x-auth-originator")},
        "Accept": "application/pdf",
        "Content-Type": "application/json",
        "Referer": "https://www.t-mobile.com/bill/historical",
        "mode": "summary",
    }
    payload = {"puid": headers["puid"], "ban": headers["ban"], "documentId": match["documentId"]}
    status, content_type, body = _post(TMOBILE_PDF_URL, payload, pdf_headers)
    if status != 200 or not body.startswith(b"%PDF"):
        raise RuntimeError(f"T-Mobile PDF download failed ({status} {content_type})")

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(body)
    print(f"  Saved: {pdf_path} ({pdf_path.stat().st_size:,} bytes)")
    return pdf_path


# --- Email ---

def send_email(pdf_path: Path, provider_label: str, month: str) -> None:
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_address or not gmail_app_password:
        sys.exit("ERROR: Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in the environment (see ~/.zshrc).")

    label = month_label(month)
    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = EMBURSE_EMAIL
    msg["Subject"] = f"{provider_label} Receipt - {label}"
    msg.attach(MIMEText(f"{provider_label} monthly receipt for {label}.", "plain"))

    part = MIMEBase("application", "pdf")
    part.set_payload(pdf_path.read_bytes())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{pdf_path.name}"')
    msg.attach(part)

    print(f"Emailing {pdf_path.name} to {EMBURSE_EMAIL}...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)
    print(f"  Sent: {msg['Subject']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("provider", choices=["classpass", "tmobile"])
    parser.add_argument(
        "--month",
        default=previous_month(),
        help="Target month(s), YYYY-MM, comma-separated (default: previous month)",
    )
    parser.add_argument("--url", help="ClassPass: Stripe receipt URL for --month")
    parser.add_argument("--send", action="store_true", help="Email the PDF(s) to Emburse")
    args = parser.parse_args()

    months = [m.strip() for m in args.month.split(",") if m.strip()]
    if args.url and len(months) != 1:
        parser.error("--url requires a single --month")
    if args.url and args.provider != "classpass":
        parser.error("--url is only supported for classpass")

    label = {"classpass": "ClassPass", "tmobile": "T-Mobile"}[args.provider]
    pdfs = []
    for month in months:
        if args.provider == "classpass":
            pdfs.append((month, classpass_pdf(month, args.url)))
        else:
            pdfs.append((month, tmobile_pdf(month)))

    if args.send:
        for month, pdf_path in pdfs:
            send_email(pdf_path, label, month)
    else:
        print("\nSend plan (rerun with --send to email):")
        for month, pdf_path in pdfs:
            print(f"  - To: {EMBURSE_EMAIL}")
            print(f"    Subject: {label} Receipt - {month_label(month)}")
            print(f"    Attachment: {pdf_path}")


if __name__ == "__main__":
    main()
