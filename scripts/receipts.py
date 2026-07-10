#!/usr/bin/env python3
"""Fetch monthly receipt PDFs and email them to your expense system.

  python3 receipts.py fetch tmobile   --month 2026-05          # downloads via Chrome session
  python3 receipts.py fetch classpass --month 2026-05 --url <stripe_receipt_url>
  python3 receipts.py fetch verizon   --month 2026-05 --pdf ~/Downloads/bill.pdf
  python3 receipts.py fetch <provider> --month 2026-05 --send  # actually email the receipt
  python3 receipts.py doctor                                   # check dependencies + config
  python3 receipts.py config --send-to receipt@ca1.chromeriver.com

Without --send, fetch only saves PDFs and prints the send plan.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib.util
import json
import os
import platform
import shutil
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

BASE_DIR = Path(os.environ.get("RECEIPTS_HOME", "~/.claude-receipts")).expanduser()
CONFIG_PATH = BASE_DIR / "config.json"
RECEIPTS_DIR = BASE_DIR / "receipts"
CLASSPASS_URL_CACHE = BASE_DIR / "stripe-urls.json"
KEYCHAIN_SERVICE = "claude-receipts-gmail"

CHROME_PROFILE = Path(
    os.environ.get("RECEIPTS_CHROME_PROFILE", "~/Library/Application Support/Google/Chrome/Default")
).expanduser()

TMOBILE_BILL_SUMMARY_URL = "https://www.t-mobile.com/self-service-pub/v1/bill-summary"
TMOBILE_PDF_URL = "https://www.t-mobile.com/self-service-britebill/billing/v2/billdetails"

PROVIDERS = {"classpass": "ClassPass", "tmobile": "T-Mobile", "verizon": "Verizon"}


def month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def previous_month() -> str:
    last = datetime.today().replace(day=1) - timedelta(days=1)
    return last.strftime("%Y-%m")


def receipt_path(provider: str, month: str) -> Path:
    return RECEIPTS_DIR / f"{provider}-receipt-{month}.pdf"


# --- Config (env vars override the config file) ---

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


def get_send_to() -> str | None:
    return os.environ.get("RECEIPTS_SEND_TO") or load_config().get("send_to")


def get_gmail_address() -> str | None:
    return os.environ.get("GMAIL_ADDRESS") or load_config().get("gmail_address")


def get_gmail_app_password() -> str | None:
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if password:
        return password
    if platform.system() != "Darwin":
        return None
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.stdout.decode().rstrip("\n") if result.returncode == 0 else None


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


def import_pdf(provider: str, month: str, source: str) -> Path:
    source_path = Path(source).expanduser()
    if not source_path.exists():
        sys.exit(f"ERROR: --pdf file not found: {source_path}")
    if source_path.read_bytes()[:4] != b"%PDF":
        sys.exit(f"ERROR: {source_path} is not a PDF")
    pdf_path = receipt_path(provider, month)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source_path, pdf_path)
    print(f"Imported {source_path} -> {pdf_path}")
    return pdf_path


# --- ClassPass ---

def classpass_pdf(month: str, url: str | None) -> Path:
    pdf_path = receipt_path("classpass", month)
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


# --- Verizon (no stable public API; import a PDF downloaded from My Verizon) ---

def verizon_pdf(month: str) -> Path:
    pdf_path = receipt_path("verizon", month)
    if pdf_path.exists():
        print(f"Using existing PDF: {pdf_path}")
        return pdf_path

    sys.exit(
        f"No Verizon PDF for {month_label(month)}.\n"
        "Verizon has no stable API for bill downloads, so grab the PDF from a real\n"
        "Chrome session:\n"
        "  1. Open https://www.verizon.com/my-bill/ (log in if needed).\n"
        "  2. Open the bill for the target month and use 'Download bill (PDF)'.\n"
        f"  3. Rerun with: fetch verizon --month {month} --pdf ~/Downloads/<file>.pdf"
    )


# --- T-Mobile (reads the logged-in Chrome session's cookies; never prints them) ---

def _chrome_cookie_db() -> Path | None:
    for candidate in (CHROME_PROFILE / "Cookies", CHROME_PROFILE / "Network" / "Cookies"):
        if candidate.exists():
            return candidate
    return None


def _chrome_cookies(host_filter: str) -> list[tuple[str, str, bytes, str]]:
    cookie_db = _chrome_cookie_db()
    if cookie_db is None:
        raise FileNotFoundError(f"No Chrome Cookies DB under {CHROME_PROFILE}")
    with tempfile.TemporaryDirectory() as tmpdir:
        copied = Path(tmpdir) / "Cookies"
        copied.write_bytes(cookie_db.read_bytes())
        with sqlite3.connect(copied) as conn:
            return conn.execute(
                "SELECT host_key, name, encrypted_value, value FROM cookies"
                " WHERE host_key LIKE ?",
                (f"%{host_filter}%",),
            ).fetchall()


def _chrome_cookie_header(host_filter: str) -> str:
    if platform.system() != "Darwin":
        raise RuntimeError("Chrome cookie extraction is only supported on macOS.")
    rows = _chrome_cookies(host_filter)

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
        raise RuntimeError(
            f"No usable {host_filter} cookies in the Chrome profile."
            f" Log in to {host_filter} in Chrome first."
        )
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
    pdf_path = receipt_path("tmobile", month)
    if pdf_path.exists():
        print(f"Using existing PDF: {pdf_path}")
        return pdf_path

    cookie_header = _chrome_cookie_header("t-mobile.com")
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
    send_to = get_send_to()
    gmail_address = get_gmail_address()
    gmail_app_password = get_gmail_app_password()
    if not send_to or not gmail_address or not gmail_app_password:
        sys.exit("ERROR: Missing send config. Run 'receipts.py doctor' to see what's missing.")

    label = month_label(month)
    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = send_to
    msg["Subject"] = f"{provider_label} Receipt - {label}"
    msg.attach(MIMEText(f"{provider_label} monthly receipt for {label}.", "plain"))

    part = MIMEBase("application", "pdf")
    part.set_payload(pdf_path.read_bytes())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{pdf_path.name}"')
    msg.attach(part)

    print(f"Emailing {pdf_path.name} to {send_to}...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)
    print(f"  Sent: {msg['Subject']}")


# --- doctor ---

def doctor() -> None:
    failures = 0

    def check(ok: bool, label: str, fix: str = "", *, warn: bool = False) -> None:
        nonlocal failures
        if ok:
            print(f"[ok]   {label}")
        else:
            print(f"[{'warn' if warn else 'FAIL'}] {label}" + (f" — {fix}" if fix else ""))
            failures += 0 if warn else 1

    check(sys.version_info >= (3, 9), f"python {platform.python_version()}", "need Python 3.9+")

    has_playwright = importlib.util.find_spec("playwright") is not None
    check(has_playwright, "playwright installed", "run: python3 -m pip install playwright")

    if has_playwright:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            chromium_ok = Path(p.chromium.executable_path).exists()
        check(chromium_ok, "playwright chromium browser", "run: python3 -m playwright install chromium")

    is_macos = platform.system() == "Darwin"
    check(
        is_macos,
        "macOS (needed for Chrome cookie access and keychain)",
        "T-Mobile fetch and keychain password storage only work on macOS",
        warn=True,
    )
    check(
        _chrome_cookie_db() is not None,
        f"Chrome profile at {CHROME_PROFILE}",
        "needed for T-Mobile; set RECEIPTS_CHROME_PROFILE if your profile is elsewhere",
        warn=True,
    )

    check(
        bool(get_send_to()),
        f"destination email (send_to = {get_send_to() or 'unset'})",
        "run: receipts.py config --send-to <your expense inbox>",
    )
    check(
        bool(get_gmail_address()),
        f"gmail address ({get_gmail_address() or 'unset'})",
        "run: receipts.py config --gmail-address <you@gmail.com>",
    )
    check(
        get_gmail_app_password() is not None,
        "gmail app password (env GMAIL_APP_PASSWORD or macOS keychain)",
        "create one at https://myaccount.google.com/apppasswords, then run:"
        " receipts.py config --set-app-password (in your own terminal)",
    )

    if failures:
        sys.exit(f"\n{failures} check(s) failed")
    print("\nAll required checks passed.")


# --- config ---

def configure(args: argparse.Namespace) -> None:
    config = load_config()
    if args.send_to:
        config["send_to"] = args.send_to
    if args.gmail_address:
        config["gmail_address"] = args.gmail_address
    if args.send_to or args.gmail_address:
        save_config(config)
        print(f"Saved {CONFIG_PATH}")

    if args.set_app_password:
        if platform.system() != "Darwin":
            sys.exit("Keychain storage requires macOS. Set GMAIL_APP_PASSWORD in your shell profile instead.")
        password = getpass.getpass("Gmail app password (input hidden): ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        if not password:
            sys.exit("No password provided.")
        account = config.get("gmail_address") or get_gmail_address() or "gmail"
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", account, "-w", password],
            check=True,
        )
        print(f"Stored app password in the macOS keychain (service: {KEYCHAIN_SERVICE}).")

    if args.show or not (args.send_to or args.gmail_address or args.set_app_password):
        print(json.dumps(load_config(), indent=2))
        print(f"app password: {'set' if get_gmail_app_password() else 'unset'}")


# --- fetch ---

def fetch(args: argparse.Namespace) -> None:
    months = [m.strip() for m in args.month.split(",") if m.strip()]
    for month in months:
        datetime.strptime(month, "%Y-%m")
    if args.url and len(months) != 1:
        sys.exit("--url requires a single --month")
    if args.url and args.provider != "classpass":
        sys.exit("--url is only supported for classpass")
    if args.pdf and len(months) != 1:
        sys.exit("--pdf requires a single --month")

    label = PROVIDERS[args.provider]
    pdfs = []
    for month in months:
        if args.pdf:
            pdfs.append((month, import_pdf(args.provider, month, args.pdf)))
        elif args.provider == "classpass":
            pdfs.append((month, classpass_pdf(month, args.url)))
        elif args.provider == "verizon":
            pdfs.append((month, verizon_pdf(month)))
        else:
            pdfs.append((month, tmobile_pdf(month)))

    if args.send:
        for month, pdf_path in pdfs:
            send_email(pdf_path, label, month)
    else:
        send_to = get_send_to() or "<unset — run: receipts.py config --send-to ...>"
        print("\nSend plan (rerun with --send to email):")
        for month, pdf_path in pdfs:
            print(f"  - To: {send_to}")
            print(f"    Subject: {label} Receipt - {month_label(month)}")
            print(f"    Attachment: {pdf_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_parser = sub.add_parser("fetch", help="fetch receipt PDF(s), optionally email them")
    fetch_parser.add_argument("provider", choices=sorted(PROVIDERS))
    fetch_parser.add_argument(
        "--month",
        default=previous_month(),
        help="Target month(s), YYYY-MM, comma-separated (default: previous month)",
    )
    fetch_parser.add_argument("--url", help="ClassPass: Stripe receipt URL for --month")
    fetch_parser.add_argument("--pdf", help="Import an already-downloaded PDF for --month")
    fetch_parser.add_argument("--send", action="store_true", help="Email the PDF(s)")
    fetch_parser.set_defaults(func=fetch)

    doctor_parser = sub.add_parser("doctor", help="check dependencies and configuration")
    doctor_parser.set_defaults(func=lambda args: doctor())

    config_parser = sub.add_parser("config", help="view or update configuration")
    config_parser.add_argument("--send-to", help="destination email (e.g. receipt@ca1.chromeriver.com)")
    config_parser.add_argument("--gmail-address", help="Gmail address used to send")
    config_parser.add_argument(
        "--set-app-password",
        action="store_true",
        help="store the Gmail app password in the macOS keychain (prompts; run in your own terminal)",
    )
    config_parser.add_argument("--show", action="store_true", help="print current config")
    config_parser.set_defaults(func=configure)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
