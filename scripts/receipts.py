#!/usr/bin/env python3
"""Fetch monthly receipt PDFs and email them to your expense system.

  python3 receipts.py status                                        # what's been sent, what's missing
  python3 receipts.py fetch tmobile --month 2026-05                 # downloads via Chrome session
  python3 receipts.py fetch tmobile,classpass --month 2026-07..2026-09
  python3 receipts.py fetch classpass --url <stripe_url> [--url ...]  # month is read off each receipt
  python3 receipts.py fetch verizon   --month 2026-05 --pdf ~/Downloads/bill.pdf
  python3 receipts.py fetch <providers> --month <months> --send     # actually email the receipts
  python3 receipts.py sync                                          # backfill send history from Gmail Sent
  python3 receipts.py doctor                                        # check dependencies + config
  python3 receipts.py config --send-to receipt@ca1.chromeriver.com
  python3 receipts.py config --set-app-password --dialog            # Gmail app password via a hidden macOS dialog
  python3 receipts.py config --auto-since 2026-08                   # earliest month `auto` may submit
  python3 receipts.py auto [--send] [--now]                         # unattended: send every receipt that's due
  python3 receipts.py schedule install|uninstall|status             # macOS launchd agent running `auto --send`

Without --send, fetch only saves PDFs and prints the send plan.
Standard library only; receipts are rendered with the installed Google Chrome.
"""

from __future__ import annotations

import argparse
import calendar
import email
import fcntl
import functools
import getpass
import hashlib
import html
import imaplib
import json
import os
import platform
import plistlib
import re
import shutil
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from email import encoders
from email.header import decode_header, make_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE_DIR = Path(os.environ.get("RECEIPTS_HOME", "~/.claude-receipts")).expanduser()
CONFIG_PATH = BASE_DIR / "config.json"
RECEIPTS_DIR = BASE_DIR / "receipts"
MANIFEST_PATH = RECEIPTS_DIR / "manifest.json"
CLASSPASS_URL_CACHE = BASE_DIR / "stripe-urls.json"
SENT_LOG = BASE_DIR / "sent.json"
AUTO_STATE_PATH = BASE_DIR / "auto-state.json"
LOCK_PATH = BASE_DIR / ".lock"
RUN_LOG = BASE_DIR / "runs.log"
INSTALLED_SCRIPT = BASE_DIR / "bin" / "receipts.py"
CLAUDE_PLUGINS_JSON = Path("~/.claude/plugins/installed_plugins.json").expanduser()
KEYCHAIN_SERVICE = "claude-receipts-gmail"
SHELL_RC_FILES = ("~/.zshrc", "~/.zprofile", "~/.zshenv", "~/.bash_profile", "~/.bashrc", "~/.profile")

CHROME_DIR = Path("~/Library/Application Support/Google/Chrome").expanduser()
CHROME_PROFILE = os.environ.get("RECEIPTS_CHROME_PROFILE")
CHROME_BINARY = Path(
    os.environ.get("RECEIPTS_CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
).expanduser()

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_IMAP_HOST = "imap.gmail.com"
APP_PASSWORD_URL = "https://myaccount.google.com/apppasswords"
TMOBILE_BILL_SUMMARY_URL = "https://www.t-mobile.com/self-service-pub/v1/bill-summary"
TMOBILE_PDF_URL = "https://www.t-mobile.com/self-service-britebill/billing/v2/billdetails"
CLASSPASS_CHARGES_URL = "https://classpass.com/settings/charges"
CLASSPASS_API = "https://classpass.com/_api"
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

LAUNCH_LABEL = "com.emburse-receipts.auto"
LAUNCH_PLIST = Path(f"~/Library/LaunchAgents/{LAUNCH_LABEL}.plist").expanduser()
ALERT_EMAIL_INTERVAL = timedelta(days=7)
DEFAULT_AUTO_PROVIDERS = ["tmobile", "classpass"]
# Providers that can tell "not issued yet" apart from a failure, so `auto` may try the current month.
CURRENT_MONTH_PROVIDERS = ("tmobile", "classpass")
TMOBILE_REJECTED = "rejected the Chrome session"
TMOBILE_NO_BILL = "no bill for"
TMOBILE_LOGIN_URL = "https://www.t-mobile.com/bill/summary"
CLASSPASS_REJECTED = "ClassPass session in Chrome has expired"
CLASSPASS_NO_CHARGE = "no ClassPass membership charge"
CLASSPASS_MULTIPLE = "ClassPass membership charges in"
# ClassPass's bot protection (Cloudflare) turns away some TLS libraries outright; seen with macOS's
# system Python (LibreSSL), while Pythons built on OpenSSL 3 get through.
CLASSPASS_BLOCKED = "ClassPass's bot protection blocked"
TLS_SUSPECT = "LibreSSL" in ssl.OPENSSL_VERSION
# Failures the Claude in Chrome fallback can't fix either.
CLASSPASS_NO_FALLBACK = (CLASSPASS_NO_CHARGE, CLASSPASS_REJECTED, CLASSPASS_MULTIPLE)
STRIPE_LINE_RE = re.compile(r"^(\d{4}-\d{2})\s+(https://pay\.stripe\.com/receipts/\S{20,})$")

PROVIDERS = {"classpass": "ClassPass", "tmobile": "T-Mobile", "verizon": "Verizon"}
SUBJECT_RE = re.compile(r"^(?P<label>.+?) Receipt - (?P<month>[A-Z][a-z]+ \d{4})")


class ReceiptError(Exception):
    """A receipt couldn't be fetched or sent; the message says what to do about it."""


class ClassPassNoCharge(ReceiptError):
    """ClassPass has no membership charge for that month (yet)."""


class LockHeld(ReceiptError):
    """Another receipts run holds the lock."""


def month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def previous_month() -> str:
    last = datetime.today().replace(day=1) - timedelta(days=1)
    return last.strftime("%Y-%m")


def this_month() -> str:
    return datetime.today().strftime("%Y-%m")


def next_month(month: str) -> str:
    return (datetime.strptime(month, "%Y-%m").replace(day=28) + timedelta(days=4)).strftime("%Y-%m")


def parse_months(spec: str) -> list[str]:
    """'2026-07', '2026-07,2026-09' or an inclusive range '2026-07..2026-09'."""
    months = []
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        start, _, end = part.partition("..")
        try:
            start = datetime.strptime(start.strip(), "%Y-%m").strftime("%Y-%m")
            end = datetime.strptime(end.strip(), "%Y-%m").strftime("%Y-%m") if end else start
        except ValueError:
            raise argparse.ArgumentTypeError(f"bad month {part!r} (use YYYY-MM or YYYY-MM..YYYY-MM)")
        if end < start:
            raise argparse.ArgumentTypeError(f"empty month range {part!r}")
        while start <= end:
            months.append(start)
            start = next_month(start)
    return list(dict.fromkeys(months))


def parse_month(spec: str) -> str:
    try:
        return datetime.strptime(spec.strip(), "%Y-%m").strftime("%Y-%m")
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad month {spec!r} (use YYYY-MM)")


def parse_providers(spec: str) -> list[str]:
    providers = [p.strip().lower() for p in spec.split(",") if p.strip()]
    unknown = [p for p in providers if p not in PROVIDERS]
    if unknown or not providers:
        raise argparse.ArgumentTypeError(
            f"unknown provider {', '.join(unknown) or spec!r}; choose from {', '.join(sorted(PROVIDERS))}"
        )
    return list(dict.fromkeys(providers))


def receipt_path(provider: str, month: str) -> Path:
    return RECEIPTS_DIR / f"{provider}-receipt-{month}.pdf"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as tmp:
            tmp.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


@contextmanager
def run_lock():
    """Exclusive lock so scheduled and manual sends can't run (and double-send) at once."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockHeld("another receipts run is in progress")
        yield


# --- Config (env vars override the config file) ---

def load_config() -> dict:
    return read_json(CONFIG_PATH)


def save_config(config: dict) -> None:
    write_json(CONFIG_PATH, config)


def get_send_to() -> str | None:
    return os.environ.get("RECEIPTS_SEND_TO") or load_config().get("send_to")


def get_auto_since() -> str | None:
    return load_config().get("auto_since")


def get_auto_providers() -> list[str]:
    return load_config().get("auto_providers") or DEFAULT_AUTO_PROVIDERS


def get_auto_skip() -> set[str]:
    return set(load_config().get("auto_skip") or [])


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


def gmail_credentials() -> tuple[str, str]:
    address, password = get_gmail_address(), get_gmail_app_password()
    if not address or not password:
        raise ReceiptError("Gmail address or app password missing. Run 'receipts.py doctor' to see what's missing.")
    return address, password


def shell_rc_defining(var: str) -> Path | None:
    """Shell startup file that exports `var` — invisible to apps not launched from a terminal."""
    for rc in SHELL_RC_FILES:
        path = Path(rc).expanduser()
        if path.exists() and re.search(rf"^\s*(export\s+)?{var}=", path.read_text(errors="ignore"), re.M):
            return path
    return None


def verify_gmail_login(address: str, password: str) -> str | None:
    """Log in to Gmail SMTP without sending anything. Returns an error message, or None if it worked."""
    try:
        with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, 465, timeout=20) as server:
            server.login(address, password)
    except smtplib.SMTPAuthenticationError:
        return "Gmail rejected the address/app password"
    except OSError as error:
        return f"couldn't reach Gmail ({error})"
    return None


# --- Receipt manifest (what each fetched PDF is: receipt id, date, amount) ---

def record_receipt(provider: str, month: str, pdf_path: Path, **info: str | None) -> dict:
    manifest = read_json(MANIFEST_PATH)
    entry = {key: value for key, value in info.items() if value}
    entry["sha256"] = sha256_file(pdf_path)
    manifest[f"{provider}/{month}"] = entry
    write_json(MANIFEST_PATH, manifest)
    return entry


def receipt_info(provider: str, month: str, pdf_path: Path) -> dict:
    entry = read_json(MANIFEST_PATH).get(f"{provider}/{month}", {})
    sha256 = sha256_file(pdf_path)
    # Metadata only applies if the PDF hasn't been replaced since it was recorded.
    return entry if entry.get("sha256") == sha256 else {"sha256": sha256}


def describe(info: dict) -> str:
    return ", ".join(info[key] for key in ("amount", "date", "detail") if info.get(key))


# --- Sent log (dedupe: one send per provider/month unless --force) ---

def load_sent_log() -> dict:
    return read_json(SENT_LOG)


def record_sent(provider: str, month: str, pdf_path: Path, send_to: str, info: dict) -> None:
    log = load_sent_log()
    key = f"{provider}/{month}"
    entry = {
        "sent_at": datetime.now().isoformat(timespec="seconds"),
        "to": send_to,
        "pdf": pdf_path.name,
        **{field: info[field] for field in ("sha256", "id", "date", "amount") if info.get(field)},
    }
    prior = log.get(key)
    if prior:
        entry["previous"] = prior.pop("previous", []) + [prior]
    log[key] = entry
    write_json(SENT_LOG, log)


def sent_elsewhere(provider: str, month: str, info: dict, log: dict) -> str | None:
    """The provider/month this exact receipt was already sent as, if it isn't `month`."""
    for key, entry in log.items():
        if key == f"{provider}/{month}":
            continue
        for sent in (entry, *entry.get("previous", [])):
            same_file = sent.get("sha256") == info.get("sha256")
            same_id = info.get("id") and sent.get("id") == info["id"] and key.startswith(f"{provider}/")
            if same_file or same_id:
                return key
    return None


def _sent_mailbox(imap: imaplib.IMAP4_SSL) -> str:
    _, boxes = imap.list()
    for line in boxes:
        line = line.decode()
        if "\\Sent" in line:
            return line.rsplit(' "/" ', 1)[-1]
    return '"[Gmail]/Sent Mail"'


def sync_sent_log() -> int:
    """Backfill sent.json from Gmail's Sent folder (catches sends made before the log existed)."""
    send_to = get_send_to()
    if not send_to:
        raise ReceiptError("destination email unset")
    address, password = gmail_credentials()
    labels = {label: provider for provider, label in PROVIDERS.items()}

    found: dict[str, list[dict]] = {}
    with imaplib.IMAP4_SSL(GMAIL_IMAP_HOST) as imap:
        imap.login(address, password)
        imap.select(_sent_mailbox(imap), readonly=True)
        _, data = imap.search(None, "TO", f'"{send_to}"', "SUBJECT", '"Receipt - "')
        for msg_id in data[0].split():
            _, parts = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT)])")
            headers = email.message_from_bytes(parts[0][1])
            match = SUBJECT_RE.match(str(make_header(decode_header(headers.get("Subject", "")))))
            if not match or match.group("label") not in labels:
                continue
            month = datetime.strptime(match.group("month"), "%B %Y").strftime("%Y-%m")
            sent_at = parsedate_to_datetime(headers["Date"]).astimezone().replace(tzinfo=None)
            found.setdefault(f"{labels[match.group('label')]}/{month}", []).append(
                {"sent_at": sent_at.isoformat(timespec="seconds"), "to": send_to, "source": "gmail"}
            )

    log = load_sent_log()
    added = 0
    for key, sends in found.items():
        if key in log:
            continue
        sends.sort(key=lambda send: send["sent_at"])
        log[key] = {**sends[-1], **({"previous": sends[:-1]} if len(sends) > 1 else {})}
        added += 1
    if added:
        write_json(SENT_LOG, log)
    return added


# --- PDF import ---

def import_pdf(provider: str, month: str, source: str) -> tuple[Path, dict]:
    source_path = Path(source).expanduser()
    if not source_path.exists():
        raise ReceiptError(f"--pdf file not found: {source_path}")
    if source_path.read_bytes()[:4] != b"%PDF":
        raise ReceiptError(f"{source_path} is not a PDF")
    pdf_path = receipt_path(provider, month)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source_path, pdf_path)
    print(f"Imported {source_path} -> {pdf_path}")
    return pdf_path, record_receipt(provider, month, pdf_path, detail=f"imported from {source_path.name}")


# --- ClassPass (Stripe receipt URLs are public; the installed Chrome renders them headless) ---

STRIPE_RECEIPT_NO_RE = re.compile(r"Receipt\s*#\s*([\d-]+)", re.I)
STRIPE_DATE_PAID_RE = re.compile(r"Date paid\s*([A-Za-z]{3,9}\.? \d{1,2}, \d{4})", re.I)
STRIPE_AMOUNT_PAID_RE = re.compile(r"Amount paid\s*([$€£][\d,]+\.\d{2})", re.I)


def _parse_receipt_date(text: str) -> datetime | None:
    text = text.replace(".", "").replace("Sept ", "Sep ")
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def headless_chrome(*args: str) -> bytes:
    """Run the installed Chrome headless in a throwaway profile, separate from the user's running Chrome."""
    if not CHROME_BINARY.exists():
        raise ReceiptError(
            f"Google Chrome not found at {CHROME_BINARY} (set RECEIPTS_CHROME_BIN if it's installed elsewhere)"
        )
    with tempfile.TemporaryDirectory() as profile:
        command = [
            str(CHROME_BINARY), "--headless=new", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={profile}", "--virtual-time-budget=15000", *args,
        ]
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90)
        except subprocess.TimeoutExpired:
            raise ReceiptError("Chrome timed out (90s) loading the Stripe receipt")
    if result.returncode != 0:
        raise ReceiptError(f"headless Chrome exited {result.returncode} loading the Stripe receipt")
    return result.stdout


def _html_text(dom: str) -> str:
    dom = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", dom)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", dom)))


def render_stripe_receipt(url: str, months: list[str] | None) -> tuple[str, Path, dict]:
    """Render a ClassPass Stripe receipt; its month comes from the receipt's own "Date paid".

    If `months` is given, the receipt must have been paid in one of them — this is what stops
    a stale or wrong link from being filed under the wrong month.
    """
    if not url.startswith("https://pay.stripe.com/receipts/"):
        raise ReceiptError(f"not a Stripe receipt URL (expected https://pay.stripe.com/receipts/...): {url[:60]}")
    print("Loading Stripe receipt...")
    text = _html_text(headless_chrome("--dump-dom", url).decode("utf-8", "replace"))

    receipt_no = STRIPE_RECEIPT_NO_RE.search(text)
    if not receipt_no and "expired" in text.lower():
        raise ReceiptError(
            f"this Stripe receipt link has expired. Click the charge again on {CLASSPASS_CHARGES_URL}"
            " to get a fresh link."
        )
    date_match = STRIPE_DATE_PAID_RE.search(text)
    paid = _parse_receipt_date(date_match.group(1)) if date_match else None
    amount = STRIPE_AMOUNT_PAID_RE.search(text)
    if not (receipt_no or date_match or amount):
        raise ReceiptError("the page didn't load as a Stripe receipt (no receipt number, date or amount on it)")

    if paid:
        month = paid.strftime("%Y-%m")
        if months and month not in months:
            raise ReceiptError(
                f"receipt #{receipt_no.group(1) if receipt_no else '?'} was paid {paid:%b %d, %Y}, so it belongs"
                f" to {month_label(month)}, not {' / '.join(month_label(m) for m in months)}."
                " Pick the charge for the right month."
            )
    elif months and len(months) == 1:
        month = months[0]
        print(f"  WARNING: couldn't read the payment date off the receipt; trusting --month {month}")
    else:
        raise ReceiptError("couldn't read the payment date off the receipt; pass a single --month for it")

    pdf_path = receipt_path("classpass", month)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = pdf_path.with_name(pdf_path.name + ".tmp")
    try:
        headless_chrome("--no-pdf-header-footer", f"--print-to-pdf={rendered}", url)
        if not rendered.exists() or rendered.read_bytes()[:4] != b"%PDF":
            raise ReceiptError(f"rendered PDF is empty or missing: {pdf_path}")
        os.replace(rendered, pdf_path)
    finally:
        rendered.unlink(missing_ok=True)

    cache = read_json(CLASSPASS_URL_CACHE)
    cache[month] = url
    write_json(CLASSPASS_URL_CACHE, cache)

    info = record_receipt(
        "classpass",
        month,
        pdf_path,
        id=receipt_no.group(1) if receipt_no else None,
        date=paid.strftime("%Y-%m-%d") if paid else None,
        amount=amount.group(1) if amount else None,
        detail=f"receipt #{receipt_no.group(1)}" if receipt_no else None,
    )
    print(f"  Saved {month_label(month)} receipt: {pdf_path} ({describe(info)})")
    return month, pdf_path, info


def _classpass_expired() -> ReceiptError:
    return ReceiptError(
        f"{CLASSPASS_REJECTED}. Log in at {CLASSPASS_CHARGES_URL} in Chrome, wait ~30s"
        " (Chrome saves cookies to disk periodically), then retry."
    )


@functools.lru_cache(maxsize=None)
def _classpass_session() -> tuple[dict, list[dict]] | ReceiptError:
    """API headers + billing history (or the login failure), fetched once per run however many months are requested."""
    try:
        return _classpass_login()
    except ReceiptError as error:
        return error


def _classpass_login() -> tuple[dict, list[dict]]:
    try:
        cookie_header = _chrome_cookie_header("classpass.com")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise ReceiptError(f"couldn't read the ClassPass session from Chrome: {error}")
    page_headers = {"User-Agent": CHROME_USER_AGENT, "Accept": "text/html", "Cookie": cookie_header}
    status, _, body = _get(CLASSPASS_CHARGES_URL, page_headers)
    if status == 403:
        raise ReceiptError(
            f"{CLASSPASS_BLOCKED} this Python's network library ({ssl.OPENSSL_VERSION}); it isn't a login problem."
            " Run this with a Python built on OpenSSL 3 (e.g. `brew install python`, then use that python3)"
        )
    page = body.decode("utf-8", "replace")
    token = re.search(r'"token"\s*:\s*"([0-9a-fA-F]+)"', page)
    user_id = re.search(r'"userId"\s*:\s*"?(\d+)"?', page)
    if status != 200 or not re.search(r'"loggedIn"\s*:\s*true', page) or not token or not user_id:
        raise _classpass_expired()

    headers = {
        "CP-Authorization": f"Token {token.group(1)}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cookie": cookie_header,
        "User-Agent": CHROME_USER_AGENT,
        "Referer": CLASSPASS_CHARGES_URL,
    }
    status, _, body = _get(f"{CLASSPASS_API}/bff/v1/users/{user_id.group(1)}/billing_history", headers)
    if status == 401:
        raise _classpass_expired()
    if status != 200:
        raise ReceiptError(f"ClassPass billing history request failed ({status})")
    return headers, json.loads(body).get("billing_history", [])


def classpass_receipt_url(month: str) -> str:
    session = _classpass_session()
    if isinstance(session, ReceiptError):
        raise session
    headers, history = session
    charges = [
        entry
        for entry in history
        if entry.get("type") == "membership"
        and not entry.get("credit")
        and entry.get("has_receipt") is not False
        and datetime.fromtimestamp(entry["created_on"]).strftime("%Y-%m") == month
    ]
    if not charges:
        raise ClassPassNoCharge(f"{CLASSPASS_NO_CHARGE} in {month_label(month)} yet")
    if len(charges) > 1:
        raise ReceiptError(
            f"{len(charges)} {CLASSPASS_MULTIPLE} {month_label(month)}; get the right receipt URL from"
            f" {CLASSPASS_CHARGES_URL} and submit it with: fetch classpass --month {month} --url '<url>'"
        )
    status, _, body = _get(f"{CLASSPASS_API}/v1/transactions/purchase/{charges[0]['transactions_id']}/receipt-url", headers)
    if status == 401:
        raise _classpass_expired()
    if status != 200:
        raise ReceiptError(f"ClassPass receipt-url request failed ({status})")
    url = json.loads(body).get("receipt_url") or ""
    if not url.startswith("https://pay.stripe.com/receipts/"):
        raise ReceiptError(f"ClassPass returned no Stripe receipt URL for {month_label(month)}")
    return url


def classpass_pdf(month: str) -> tuple[Path, dict]:
    pdf_path = receipt_path("classpass", month)
    if pdf_path.exists():
        print(f"Using existing PDF: {pdf_path}")
        return pdf_path, receipt_info("classpass", month, pdf_path)
    try:
        url = classpass_receipt_url(month)
    except ReceiptError as error:
        url = read_json(CLASSPASS_URL_CACHE).get(month)
        if not url:
            raise ReceiptError(
                f"{str(error).rstrip('.')}. Or pass the receipt URL from {CLASSPASS_CHARGES_URL} with --url."
            )
        print(f"ClassPass API unavailable ({error}); using cached Stripe URL for {month_label(month)}")
    _, pdf_path, info = render_stripe_receipt(url, [month])
    return pdf_path, info


# --- Verizon (no stable public API; import a PDF downloaded from My Verizon) ---

def verizon_pdf(month: str) -> tuple[Path, dict]:
    pdf_path = receipt_path("verizon", month)
    if pdf_path.exists():
        print(f"Using existing PDF: {pdf_path}")
        return pdf_path, receipt_info("verizon", month, pdf_path)

    raise ReceiptError(
        "no PDF yet. Verizon has no stable API for bill downloads, so grab it from a real Chrome"
        " session: open https://www.verizon.com/my-bill/, open that month's bill, use 'Download"
        f" bill (PDF)', then rerun with: fetch verizon --month {month} --pdf ~/Downloads/<file>.pdf"
    )


# --- T-Mobile (reads the logged-in Chrome session's cookies; never prints them) ---

def _chrome_cookie_dbs() -> list[Path]:
    """Cookie DBs of every Chrome profile (only RECEIPTS_CHROME_PROFILE's, if that's set)."""
    if CHROME_PROFILE:
        profiles = [Path(CHROME_PROFILE).expanduser()]
    else:
        profiles = [p for p in sorted(CHROME_DIR.glob("*")) if p.name == "Default" or p.name.startswith("Profile ")]
    candidates = [db for profile in profiles for db in (profile / "Cookies", profile / "Network" / "Cookies")]
    return [db for db in candidates if db.exists()]


def _chrome_cookies(host: str) -> list[tuple[str, str, bytes, str]]:
    """Cookies for `host` and its subdomains, from the Chrome profile that used that site most recently."""
    cookie_dbs = _chrome_cookie_dbs()
    if not cookie_dbs:
        raise FileNotFoundError(f"No Chrome Cookies DB under {CHROME_PROFILE or CHROME_DIR}")
    latest, best = -1, []
    with tempfile.TemporaryDirectory() as tmpdir:
        for number, cookie_db in enumerate(cookie_dbs):
            copied = Path(tmpdir) / f"Cookies-{number}"
            copied.write_bytes(cookie_db.read_bytes())
            with sqlite3.connect(copied) as conn:
                rows = conn.execute(
                    "SELECT host_key, name, encrypted_value, value, last_access_utc FROM cookies"
                    " WHERE host_key = ? OR host_key LIKE ?",
                    (host, f"%.{host}"),
                ).fetchall()
            if rows and max(row[4] for row in rows) > latest:
                latest, best = max(row[4] for row in rows), [row[:4] for row in rows]
    return best


def _chrome_cookie_header(host_filter: str) -> str:
    if platform.system() != "Darwin":
        raise RuntimeError("Chrome cookie extraction is only supported on macOS.")
    rows = _chrome_cookies(host_filter)

    try:
        password = subprocess.check_output(
            ["security", "find-generic-password", "-w", "-a", "Chrome", "-s", "Chrome Safe Storage"],
            stderr=subprocess.DEVNULL,
        ).rstrip(b"\n")
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "macOS didn't release Chrome's cookie key. When the keychain asks whether 'security' may use"
            " 'Chrome Safe Storage', enter your Mac password and click Always Allow, then retry."
        )
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


def _get(url: str, headers: dict[str, str]) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.status, response.headers.get("content-type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("content-type", ""), error.read()


def _bill_month(bill: dict) -> str:
    return datetime.fromisoformat(bill["endTime"]).strftime("%Y-%m")


@functools.lru_cache(maxsize=None)
def _tmobile_session() -> tuple[dict, list[dict]] | ReceiptError:
    """Auth headers + bill list (or the login failure), fetched once per run however many months are requested."""
    try:
        return _tmobile_login()
    except ReceiptError as error:
        return error


def _tmobile_login() -> tuple[dict, list[dict]]:
    try:
        cookie_header = _chrome_cookie_header("t-mobile.com")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise ReceiptError(f"couldn't read the T-Mobile session from Chrome: {error}")
    headers = {
        "User-Agent": CHROME_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://www.t-mobile.com",
        "Referer": "https://www.t-mobile.com/bill/summary",
        "Cookie": cookie_header,
    }

    status, content_type, body = _post(TMOBILE_BILL_SUMMARY_URL, "action=getSessionData", headers)
    if status != 200:
        raise ReceiptError(
            f"T-Mobile rejected the Chrome session ({status}). Log in at"
            " https://www.t-mobile.com/bill/summary in Chrome, wait ~30s (Chrome saves cookies"
            " to disk periodically), then retry."
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
        raise ReceiptError(f"T-Mobile bill list request failed ({status} {content_type})")
    bills = [bill for group in json.loads(body).get("getBillList", []) for bill in group.get("bills", [])]
    return headers, [bill for bill in bills if bill.get("documentId")]


def _format_amount(value) -> str | None:
    if isinstance(value, (int, float)):
        return f"${value:,.2f}"
    return str(value) if value else None


def tmobile_pdf(month: str) -> tuple[Path, dict]:
    pdf_path = receipt_path("tmobile", month)
    if pdf_path.exists():
        print(f"Using existing PDF: {pdf_path}")
        return pdf_path, receipt_info("tmobile", month, pdf_path)

    session = _tmobile_session()
    if isinstance(session, ReceiptError):
        raise session
    headers, bills = session
    match = next((bill for bill in bills if _bill_month(bill) == month), None)
    if not match:
        available = ", ".join(sorted(_bill_month(bill) for bill in bills))
        raise ReceiptError(
            f"{TMOBILE_NO_BILL} {month_label(month)} yet (bills close on the cycle end date). Available: {available}"
        )

    print(f"Downloading T-Mobile bill for {month_label(month)}")
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
        raise ReceiptError(f"T-Mobile PDF download failed ({status} {content_type})")

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(body)
    info = record_receipt(
        "tmobile",
        month,
        pdf_path,
        id=match["documentId"],
        date=datetime.fromisoformat(match["endTime"]).strftime("%Y-%m-%d"),
        amount=_format_amount(match.get("currentCharges")),
        detail=f"cycle {match['cycleValue']}" if match.get("cycleValue") else None,
    )
    print(f"  Saved: {pdf_path} ({describe(info)})")
    return pdf_path, info


FETCHERS = {"classpass": classpass_pdf, "tmobile": tmobile_pdf, "verizon": verizon_pdf}


# --- Email ---

def send_email(pdf_path: Path, provider_label: str, month: str, info: dict) -> None:
    send_to = get_send_to()
    if not send_to:
        raise ReceiptError("destination email unset. Run 'receipts.py doctor' to see what's missing.")
    gmail_address, gmail_app_password = gmail_credentials()

    label = month_label(month)
    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = send_to
    msg["Subject"] = f"{provider_label} Receipt - {label}"
    body = f"{provider_label} monthly receipt for {label}."
    if describe(info):
        body += f"\n{describe(info)}"
    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("application", "pdf")
    part.set_payload(pdf_path.read_bytes())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{pdf_path.name}"')
    msg.attach(part)

    print(f"Emailing {pdf_path.name} to {send_to}...")
    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)
    print(f"  Sent: {msg['Subject']}")


def send_alert_email(problems: str) -> None:
    """Email yourself what a scheduled run couldn't do; a notification alone is easy to miss."""
    gmail_address, gmail_app_password = gmail_credentials()
    msg = MIMEText(
        f"The scheduled receipt run couldn't finish:\n\n{problems}\n\n"
        "Fix what it describes (usually: log in to the provider in Chrome). The next daily run retries,"
        " or run /emburse-receipts:send pending in Claude Code.\n"
        f"Log: {RUN_LOG}\n",
        "plain",
    )
    msg["From"] = msg["To"] = gmail_address
    msg["Subject"] = "Emburse receipts need attention"
    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, 465, timeout=30) as server:
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)


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

    check(sys.version_info >= (3, 9), f"python {platform.python_version()} ({sys.executable})", "need Python 3.9+")
    check(
        not TLS_SUSPECT,
        f"python network library ({ssl.OPENSSL_VERSION})",
        "ClassPass's bot protection blocks this one (T-Mobile still answers it). For ClassPass, use a Python built"
        " on OpenSSL 3: run `brew install python` (or install from python.org) and rerun with that python3",
        warn=True,
    )

    is_macos = platform.system() == "Darwin"
    check(
        is_macos,
        "macOS (needed for Chrome cookie access, keychain and scheduling)",
        "automatic T-Mobile/ClassPass fetching, keychain password storage and scheduled runs only work on macOS",
        warn=True,
    )
    check(
        CHROME_BINARY.exists(),
        f"Google Chrome at {CHROME_BINARY}",
        "install Google Chrome (https://www.google.com/chrome/), or set RECEIPTS_CHROME_BIN if it's elsewhere",
    )
    check(
        bool(_chrome_cookie_dbs()),
        f"Chrome profile under {CHROME_PROFILE or CHROME_DIR}",
        "open Chrome once and log in to your providers; set RECEIPTS_CHROME_PROFILE if your profile is elsewhere",
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

    password = get_gmail_app_password()
    rc_file = shell_rc_defining("GMAIL_APP_PASSWORD")
    if password is None and rc_file:
        fix = (
            f"it's exported in {rc_file}, but apps not started from a terminal (like the Claude"
            " desktop app) don't load that file. Copy it into the keychain, run from any directory:"
            f" {'zsh' if 'zsh' in rc_file.name else 'bash'} -ic"
            f" 'python3 \"{Path(__file__).resolve()}\" config --set-app-password --from-env'"
        )
    else:
        fix = (
            f"create one at {APP_PASSWORD_URL}, then run: receipts.py config --set-app-password --dialog"
            " (a hidden-input macOS dialog; or omit --dialog in your own terminal)"
        )
    check(password is not None, "gmail app password (env GMAIL_APP_PASSWORD or macOS keychain)", fix)

    if password and get_gmail_address():
        error = verify_gmail_login(get_gmail_address(), password)
        check(
            error is None,
            "gmail login (no email sent)",
            f"{error} — check the address, or create a new app password at {APP_PASSWORD_URL}",
        )

    if failures:
        sys.exit(f"\n{failures} check(s) failed")
    print("\nAll required checks passed.")


# --- config ---

APP_PASSWORD_DIALOG = (
    "activate\n"
    'set answer to display dialog ("Paste your Gmail app password (16 letters)." & return & return'
    ' & "It goes straight into your macOS keychain. Need one? Click Create one'
    ' (your Google account needs 2-Step Verification).")'
    ' default answer "" with hidden answer with title "Emburse receipts"'
    ' buttons {"Cancel", "Create one", "Save"} default button "Save" cancel button "Cancel"\n'
    "return (button returned of answer) & linefeed & (text returned of answer)"
)


def app_password_dialog() -> str:
    """Hidden-input macOS dialog, so the password never passes through a terminal or a chat."""
    while True:
        result = subprocess.run(["osascript", "-e", APP_PASSWORD_DIALOG], capture_output=True, text=True)
        button, _, password = result.stdout.rstrip("\n").partition("\n")
        if result.returncode != 0 or button not in ("Save", "Create one"):
            return ""
        if button == "Save":
            return "".join(password.split())
        subprocess.run(["open", APP_PASSWORD_URL], check=False)


def configure(args: argparse.Namespace) -> None:
    config = load_config()
    if args.send_to:
        config["send_to"] = args.send_to
    if args.gmail_address:
        config["gmail_address"] = args.gmail_address
    if args.auto_since:
        config["auto_since"] = args.auto_since
    if args.auto_providers:
        config["auto_providers"] = args.auto_providers
    if args.claude_fallback:
        config["claude_fallback"] = args.claude_fallback == "on"
    changed = args.send_to or args.gmail_address or args.auto_since or args.auto_providers or args.claude_fallback
    if changed:
        save_config(config)
        print(f"Saved {CONFIG_PATH}")

    if args.set_app_password:
        if platform.system() != "Darwin":
            sys.exit("Keychain storage requires macOS. Set GMAIL_APP_PASSWORD in your shell profile instead.")
        if args.from_env:
            password = os.environ.get("GMAIL_APP_PASSWORD", "")
        elif args.dialog:
            password = app_password_dialog()
        elif sys.stdin.isatty():
            password = getpass.getpass("Gmail app password (input hidden): ")
        else:
            password = sys.stdin.readline().rstrip("\n")
        if not password:
            sys.exit("No password provided." + (" GMAIL_APP_PASSWORD isn't set in this shell." if args.from_env else ""))
        account = config.get("gmail_address") or get_gmail_address() or "gmail"
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", account, "-w", password],
            check=True,
        )
        print(f"Stored app password in the macOS keychain (service: {KEYCHAIN_SERVICE}).")
        if "@" in account:
            error = verify_gmail_login(account, password)
            print(f"WARNING: {error}." if error else "Verified: Gmail accepts it.")

    if args.show or not (changed or args.set_app_password):
        print(json.dumps(load_config(), indent=2))
        print(f"app password: {'set' if get_gmail_app_password() else 'unset'}")


# --- status / sync ---

def sync(args: argparse.Namespace) -> None:
    try:
        added = sync_sent_log()
    except (ReceiptError, imaplib.IMAP4.error, OSError) as error:
        sys.exit(f"Couldn't sync from Gmail: {error}")
    print(f"Added {added} send(s) from Gmail's Sent folder to {SENT_LOG}." if added else "Send log already up to date.")


def status(args: argparse.Namespace) -> None:
    if not args.offline:
        try:
            added = sync_sent_log()
            if added:
                print(f"(Backfilled {added} earlier send(s) from Gmail's Sent folder.)")
        except (ReceiptError, imaplib.IMAP4.error, OSError) as error:
            print(f"(Couldn't check Gmail's Sent folder: {str(error).rstrip('.')}. Showing the local send log only.)")

    log = load_sent_log()
    fetched = {
        (match.group(1), match.group(2))
        for match in (re.match(r"(\w+)-receipt-(\d{4}-\d{2})\.pdf$", p.name) for p in RECEIPTS_DIR.glob("*.pdf"))
        if match
    }
    known = {tuple(key.split("/", 1)) for key in log} | fetched
    providers = args.provider or [p for p in sorted(PROVIDERS) if any(k[0] == p for k in known)]
    if not providers:
        print("Nothing fetched or sent yet.")
        return

    current = this_month()
    first = {p: min([m for k, m in known if k == p] or [current]) for p in providers}
    month = args.since or min(first.values())
    # Months the scheduled job deliberately leaves alone aren't "missing".
    auto_since, auto_skip = (None if args.since else get_auto_since()), get_auto_skip()
    rows, pending, left_alone = [], {p: [] for p in providers}, {p: [] for p in providers}
    while month <= current:
        cells = []
        for provider in providers:
            entry = log.get(f"{provider}/{month}")
            if entry:
                repeat = len(entry.get("previous", []))
                cells.append(f"sent {entry['sent_at'][:10]}" + (f" (x{repeat + 1})" if repeat else ""))
            elif month < first[provider] and not args.since:
                cells.append("-")
            elif f"{provider}/{month}" in auto_skip or (auto_since and month < auto_since):
                cells.append("not sent (left alone)")
                left_alone[provider].append(month)
            else:
                cells.append("fetched, not sent" if (provider, month) in fetched else "NOT SENT")
                pending[provider].append(month)
        rows.append([month, *cells])
        month = next_month(month)

    header = ["Month", *(PROVIDERS[p] for p in providers)]
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip())

    todo = {p: months for p, months in pending.items() if months}
    if todo:
        print("\nNot sent yet (the current month's bill may not be issued yet):")
        for provider, months in todo.items():
            print(f"  {provider}: {','.join(months)}")
    else:
        print("\nEverything is sent.")
    if any(left_alone.values()):
        print(
            f"\nLeft alone (before auto_since {get_auto_since()} or in auto_skip; send them yourself with"
            " fetch ... --send if you want them):"
        )
        for provider, months in left_alone.items():
            if months:
                print(f"  {provider}: {','.join(months)}")


# --- fetch ---

def _fail(errors: dict, provider: str, what: str, problem: object) -> None:
    errors.setdefault((PROVIDERS[provider], str(problem)), []).append(what)


def _try(get):
    """(result, None) on success or (None, problem text); a network/browser hiccup shouldn't sink the batch."""
    try:
        return get(), None
    except ReceiptError as error:
        return None, str(error)
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"


def _attempt(ready: list, errors: dict, provider: str, what: str, get) -> None:
    result, problem = _try(get)
    if problem:
        _fail(errors, provider, what, problem)
    else:
        month, pdf_path, info = result
        ready.append((provider, month, pdf_path, info))


def deliver(ready: list, errors: dict, force: bool = False) -> tuple[list, list]:
    """Email each ready receipt unless it's a dupe; returns ([(provider, month) sent], [(provider, month) skipped])."""
    sent, skipped = [], []
    sent_log = load_sent_log()
    keys = [(provider, month) for provider, month, _, _ in ready]
    for provider, month, pdf_path, info in ready:
        label = PROVIDERS[provider]
        if keys.count((provider, month)) > 1:
            # Both were saved to the same file, so sending either could attach the wrong receipt (twice).
            problem = "more than one receipt for this month in one batch, so none was sent; fetch the right one alone with --url"
            if month not in errors.get((label, problem), []):
                _fail(errors, provider, month, problem)
            continue
        duplicate = sent_elsewhere(provider, month, info, sent_log)
        if duplicate:
            _fail(errors, provider, month, f"BLOCKED — this exact receipt was already sent as {duplicate}")
            continue
        prior = sent_log.get(f"{provider}/{month}")
        if prior and not force:
            print(
                f"SKIPPED {label} {month}: already sent {prior['sent_at']} to {prior['to']}"
                " (rerun with --force to resend)"
            )
            skipped.append((provider, month))
            continue
        try:
            send_email(pdf_path, label, month, info)
        except (ReceiptError, smtplib.SMTPException, OSError) as error:
            _fail(errors, provider, month, f"send failed: {error}")
            continue
        record_sent(provider, month, pdf_path, get_send_to(), info)
        sent.append((provider, month))
    return sent, skipped


def print_send_plan(ready: list) -> None:
    sent_log = load_sent_log()
    send_to = get_send_to() or "<unset — run: receipts.py config --send-to ...>"
    print(f"\nSend plan (rerun with --send to email) — To: {send_to}, From: {get_gmail_address() or '<unset>'}")
    for provider, month, pdf_path, info in ready:
        label = PROVIDERS[provider]
        print(f"  - {label} Receipt - {month_label(month)}" + (f"  [{describe(info)}]" if describe(info) else ""))
        print(f"    Attachment: {pdf_path} ({pdf_path.stat().st_size // 1024:,} KB)")
        duplicate = sent_elsewhere(provider, month, info, sent_log)
        if duplicate:
            print(f"    BLOCKED: this exact receipt was already sent as {duplicate} — --send will refuse it")
        prior = sent_log.get(f"{provider}/{month}")
        if prior:
            print(f"    WARNING: already sent {prior['sent_at']} to {prior['to']} — --send will skip it without --force")


def print_problems(errors: dict) -> None:
    print("\nProblems:")
    for (label, problem), whats in errors.items():
        print(f"  - {label} {', '.join(whats)}: {problem}")


def fetch(args: argparse.Namespace) -> None:
    if not args.send:
        return _fetch(args)
    try:
        with run_lock():
            _fetch(args)
    except LockHeld as error:
        sys.exit(str(error))


def _fetch(args: argparse.Namespace) -> None:
    providers = args.provider
    months = args.month or [previous_month()]
    if args.url and providers != ["classpass"]:
        sys.exit("--url is only supported for classpass")
    if args.pdf and (len(providers) != 1 or len(months) != 1):
        sys.exit("--pdf requires a single provider and a single --month")
    if args.force and (len(providers) != 1 or len(months) != 1 or len(args.url or []) > 1):
        sys.exit("--force requires a single provider and a single --month, so only that one receipt is resent")

    if args.send:
        try:
            sync_sent_log()
        except (ReceiptError, imaplib.IMAP4.error, OSError) as error:
            sys.exit(f"Couldn't sync the send log from Gmail, so nothing was sent: {error}")

    ready: list = []
    errors: dict = {}

    if args.url:
        for number, url in enumerate(args.url, 1):
            _attempt(ready, errors, "classpass", f"URL #{number}", lambda: render_stripe_receipt(url, args.month))
    elif args.pdf:
        provider, month = providers[0], months[0]
        _attempt(ready, errors, provider, month, lambda: (month, *import_pdf(provider, month, args.pdf)))
    else:
        for provider in providers:
            for month in months:
                _attempt(ready, errors, provider, month, lambda: (month, *FETCHERS[provider](month)))

    ready.sort(key=lambda item: (item[0], item[1]))
    if args.send:
        deliver(ready, errors, args.force)
    elif ready:
        print_send_plan(ready)

    if errors:
        print_problems(errors)
        sys.exit(1)


# --- auto (unattended runs) ---

CLASSPASS_PROMPT = (
    "Use only the Claude in Chrome browser tools. Open https://classpass.com/settings/charges in a new tab."
    " If ClassPass shows a login page, do not enter any credentials; reply with exactly NEEDS_LOGIN and stop."
    " Find the subscription charges paid in these months: {months}."
    " For each one, click the charge row; it opens a Stripe receipt in a new tab."
    " If clicking the row by reference doesn't open it, click the row's chevron by coordinates."
    " Read the new tab's URL from the tab list; it starts with https://pay.stripe.com/receipts/."
    " Do not open or click anything else, and do not change any account settings."
    " When done, reply with one line per month in the form `YYYY-MM <url>` and nothing else."
    " If a month has no charge, write `YYYY-MM NONE`."
)


def claude_binary() -> str | None:
    return os.environ.get("RECEIPTS_CLAUDE_BIN") or shutil.which("claude")


def claude_fallback_enabled() -> bool:
    """Off unless switched on with `config --claude-fallback on`: it lets an AI agent drive the user's Chrome unattended."""
    return bool(load_config().get("claude_fallback")) and claude_binary() is not None


def collect_stripe_urls(months: list[str]) -> list[str]:
    """Ask Claude in Chrome (headless `claude -p`) for the Stripe receipt URLs of ClassPass charges."""
    claude = claude_binary()
    if not claude:
        raise ReceiptError(
            "the claude CLI wasn't found (needed to read ClassPass charges). Install Claude Code or set RECEIPTS_CLAUDE_BIN."
        )
    subprocess.run(["open", "-g", "-a", "Google Chrome"], check=False)
    time.sleep(5)
    prompt = CLASSPASS_PROMPT.format(months=", ".join(month_label(m) for m in months))
    command = [
        claude, "-p", "--chrome", "--model", "sonnet", "--permission-mode", "bypassPermissions",
        "--disallowedTools", "Bash,Edit,Write,NotebookEdit",
    ]
    try:
        result = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=900, cwd=BASE_DIR)
    except subprocess.TimeoutExpired:
        raise ReceiptError("Claude in Chrome timed out (15 min) reading the ClassPass charges")
    except OSError as error:
        raise ReceiptError(f"couldn't run claude: {error}")

    stdout, stderr = result.stdout or "", result.stderr or ""
    # Stripe receipt links work for anyone who has them, so they stay out of the log.
    redacted = re.sub(r"(https://pay\.stripe\.com/receipts/)\S+", r"\1<redacted>", stdout.strip())
    print(f"Claude in Chrome output: {redacted[:2000]}", flush=True)
    combined = stdout + stderr
    if "Failed to authenticate" in combined or "OAuth" in combined:
        raise ReceiptError(
            "Claude Code isn't logged in (session expired). Run `claude` in a terminal and use /login, then rerun."
        )
    if "NEEDS_LOGIN" in stdout:
        raise ReceiptError("ClassPass needs a login. Log in at https://classpass.com/settings/charges in Chrome, then rerun.")
    if result.returncode != 0:
        raise ReceiptError(f"claude exited {result.returncode}: ...{combined.strip()[-300:]}")
    urls = []
    for line in stdout.splitlines():
        match = STRIPE_LINE_RE.match(line.strip().strip("`").strip())
        if match:
            urls.append(match.group(2))
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise ReceiptError(f"Claude in Chrome returned no Stripe receipt URLs. Output ended: ...{combined.strip()[-300:]}")
    return urls


def notify(message: str) -> None:
    script = f"display notification {json.dumps(message, ensure_ascii=False)} with title \"Emburse receipts\""
    try:
        subprocess.run(["osascript", "-e", script], check=False)
    except OSError:
        pass


def alert(state: dict, problems: str) -> None:
    """Notify, and email the details too — at most once a week while the same problems persist."""
    notify(f"Needs attention: {problems.splitlines()[0]} (see {RUN_LOG})"[:250])
    digest = hashlib.sha256(problems.encode()).hexdigest()[:16]
    last = state.get("last_alert") or {}
    if last.get("digest") == digest and datetime.now() - datetime.fromisoformat(last["at"]) < ALERT_EMAIL_INTERVAL:
        return
    try:
        send_alert_email(problems)
    except (ReceiptError, smtplib.SMTPException, OSError) as error:
        print(f"(Couldn't email the alert: {error})")
        return
    print(f"Emailed the problem list to {get_gmail_address()}.")
    state["last_alert"] = {"digest": digest, "at": datetime.now().isoformat(timespec="seconds")}


def expected_day(provider: str) -> int | None:
    """Day of the month this provider's receipt usually appears, going by the latest one fetched."""
    dated = {
        key: entry["date"]
        for key, entry in read_json(MANIFEST_PATH).items()
        if key.startswith(f"{provider}/") and entry.get("date")
    }
    return int(dated[max(dated)][8:10]) if dated else None


def pending_receipts(log: dict, today: datetime | None = None) -> tuple[list[tuple[str, str]], list[tuple[str, str, int]]]:
    """Unsent receipts since auto_since: ([(provider, month)] due now, [(provider, month, day)] not expected yet).

    The current month is due once the provider's usual issue day has come (right away if that's unknown).
    """
    today = today or datetime.today()
    since, current = get_auto_since(), today.strftime("%Y-%m")
    last_day = calendar.monthrange(today.year, today.month)[1]
    skip = get_auto_skip()
    due, waiting = [], []
    for provider in get_auto_providers():
        day = expected_day(provider)
        for month in parse_months(f"{since}..{current}") if since <= current else []:
            if f"{provider}/{month}" in log or f"{provider}/{month}" in skip:
                continue
            if month < current:
                due.append((provider, month))
            elif provider not in CURRENT_MONTH_PROVIDERS:
                continue
            elif day and today.day < min(day, last_day):
                waiting.append((provider, month, day))
            else:
                due.append((provider, month))
    return due, waiting


def _describe_waiting(waiting: list) -> str:
    return "; ".join(f"{PROVIDERS[p]} {month_label(m)} (usually issued around day {day})" for p, m, day in waiting)


def _report(errors: dict, provider: str, month: str, problem: str) -> None:
    """Record a failed fetch — unless the current month's bill/charge just doesn't exist yet; the next run retries."""
    if month == this_month() and (TMOBILE_NO_BILL in problem or CLASSPASS_NO_CHARGE in problem):
        print(f"{PROVIDERS[provider]} {month_label(month)}: not issued yet; the next run retries.")
    else:
        _fail(errors, provider, month, problem)


def _fetch_with_retry(
    provider: str, months: list[str], send: bool, ready: list, fetcher, rejected_marker: str, login_url: str, session
) -> dict:
    """Fetch each month; if the browser session was rejected, open the login page, wait, and retry once.

    Returns {month: problem} for what still failed.
    """
    def get(month: str):
        return _try(lambda: (month, *fetcher(month)))

    outcomes = {month: get(month) for month in months}
    if send and any(rejected_marker in (problem or "") for _, problem in outcomes.values()):
        subprocess.run(["open", "-g", "-a", "Google Chrome", login_url], check=False)
        time.sleep(45)
        session.cache_clear()
        outcomes = {
            month: get(month) if rejected_marker in (problem or "") else (result, problem)
            for month, (result, problem) in outcomes.items()
        }
    failures = {}
    for month, (result, problem) in outcomes.items():
        if problem:
            failures[month] = problem
        else:
            ready.append((provider, *result))
    return failures


def _fetch_tmobile(months: list[str], send: bool, ready: list, errors: dict) -> None:
    failures = _fetch_with_retry(
        "tmobile", months, send, ready, tmobile_pdf, TMOBILE_REJECTED, TMOBILE_LOGIN_URL, _tmobile_session
    )
    for month, problem in failures.items():
        _report(errors, "tmobile", month, problem)


def _fetch_classpass(months: list[str], send: bool, ready: list, errors: dict) -> list[str]:
    """Fetches via the Chrome session's API; returns the months that need (or would need) the Claude-in-Chrome fallback."""
    failures = _fetch_with_retry(
        "classpass", months, send, ready, classpass_pdf, CLASSPASS_REJECTED, CLASSPASS_CHARGES_URL, _classpass_session
    )
    fallback = [
        month
        for month, problem in failures.items()
        if claude_fallback_enabled() and not any(marker in problem for marker in CLASSPASS_NO_FALLBACK)
    ]
    for month, problem in failures.items():
        if month not in fallback or not send:
            _report(errors, "classpass", month, problem)
    if not (send and fallback):
        return fallback

    try:
        urls = collect_stripe_urls(fallback)
    except ReceiptError as error:
        for month in fallback:
            _fail(errors, "classpass", month, f"{failures[month]} Claude in Chrome fallback also failed: {error}")
        return fallback
    before = len(ready)
    for number, url in enumerate(urls, 1):
        _attempt(ready, errors, "classpass", f"URL #{number}", lambda: render_stripe_receipt(url, fallback))
    got = {item[1] for item in ready[before:]}
    for month in fallback:
        if month not in got:
            _fail(errors, "classpass", month, f"{failures[month]} (the Claude in Chrome fallback didn't produce it)")
    return fallback


def _describe_sent(sent: list) -> str:
    by_provider: dict[str, list[str]] = {}
    for provider, month in sent:
        by_provider.setdefault(PROVIDERS[provider], []).append(datetime.strptime(month, "%Y-%m").strftime("%b %Y"))
    return "; ".join(f"{label} {', '.join(months)}" for label, months in by_provider.items())


def wait_for_network(host: str = GMAIL_IMAP_HOST, port: int = 993, timeout: int = 120) -> bool:
    """A launchd run on wake can fire before Wi-Fi is back."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.create_connection((host, port), timeout=5).close()
            return True
        except OSError:
            if time.monotonic() + 10 > deadline:
                print(f"Network still unreachable ({host}:{port}) after {timeout}s; continuing anyway.", flush=True)
                return False
            time.sleep(10)


def plugin_script() -> Path | None:
    """receipts.py inside the installed Claude Code plugin, if there is one."""
    try:
        plugins = json.loads(CLAUDE_PLUGINS_JSON.read_text()).get("plugins", {})
        entries = next(v for k, v in plugins.items() if k.startswith("emburse-receipts@") and v)
        script = Path(entries[0]["installPath"]).expanduser() / "scripts" / "receipts.py"
    except (OSError, ValueError, StopIteration, KeyError, IndexError, TypeError, AttributeError):
        return None
    return script if script.exists() else None


def refresh_from_plugin(state: dict) -> None:
    """Pick up a plugin update: copy the plugin's script over the scheduled copy and restart on it."""
    if Path(__file__).resolve() != INSTALLED_SCRIPT.resolve():
        return
    script = plugin_script()
    if not script or sha256_file(script) == state.get("plugin_script_sha256"):
        return
    install_script(script)
    state["plugin_script_sha256"] = sha256_file(script)
    write_json(AUTO_STATE_PATH, state)
    print(f"Updated the scheduled script from the plugin at {script}", flush=True)
    os.execv(sys.executable, [sys.executable, str(INSTALLED_SCRIPT), *sys.argv[1:]])


def secure_log() -> None:
    """The log names accounts and receipts; keep it readable by the user only."""
    try:
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        RUN_LOG.touch(exist_ok=True)
        os.chmod(RUN_LOG, 0o600)
    except OSError:
        pass


def auto(args: argparse.Namespace) -> None:
    state = read_json(AUTO_STATE_PATH)
    if args.send:
        secure_log()
        print(f"=== auto --send {datetime.now():%Y-%m-%d %H:%M} ===", flush=True)
        refresh_from_plugin(state)
    if not get_auto_since():
        sys.exit("auto_since isn't set. Run: receipts.py config --auto-since YYYY-MM (earliest month to submit)")
    if args.send and not args.now:
        # Daily runs are cheap: going by the local send log, most days there's nothing to do (and no network use).
        due, waiting = pending_receipts(load_sent_log())
        if not due:
            print("Nothing due." + (f" Not expected yet: {_describe_waiting(waiting)}." if waiting else ""))
            state["last_run"] = state["last_success"] = datetime.now().isoformat(timespec="seconds")
            write_json(AUTO_STATE_PATH, state)
            return
    if args.send:
        wait_for_network()

    try:
        with run_lock():
            failed = _auto_run(args, state)
    except LockHeld as error:
        print(error)
        return
    if failed:
        sys.exit(1)


def _auto_run(args: argparse.Namespace, state: dict) -> bool:
    """Returns True if anything went wrong."""
    errors: dict = {}

    def finish(success: bool) -> bool:
        if args.send:
            state["last_run"] = datetime.now().isoformat(timespec="seconds")
            if success:
                state["last_success"] = state["last_run"]
            write_json(AUTO_STATE_PATH, state)
        return not success

    try:
        sync_sent_log()
    except (ReceiptError, imaplib.IMAP4.error, OSError) as error:
        problem = f"couldn't sync the send log from Gmail, so nothing was sent: {error}"
        print(problem)
        if args.send:
            notify(f"Needs attention: {problem} (see {RUN_LOG})")
        return finish(False)

    due, waiting = pending_receipts(load_sent_log())
    if args.now:
        due, waiting = due + [(provider, month) for provider, month, _ in waiting], []
    print(f"Auto providers: {', '.join(get_auto_providers())}; months {get_auto_since()} through {this_month()}.")
    if waiting:
        print(f"Not expected yet (--now tries anyway): {_describe_waiting(waiting)}")
    if not due:
        print("Nothing due.")
        return finish(True)
    print("Due: " + "; ".join(f"{PROVIDERS[p]} {m}" for p, m in due))

    ready: list = []
    need_browser: list[str] = []
    for provider in get_auto_providers():
        months = [m for p, m in due if p == provider]
        if not months:
            continue
        if provider == "tmobile":
            _fetch_tmobile(months, args.send, ready, errors)
        elif provider == "classpass":
            need_browser = _fetch_classpass(months, args.send, ready, errors)
        else:
            for month in months:
                _attempt(ready, errors, provider, month, lambda: (month, *FETCHERS[provider](month)))
    ready.sort(key=lambda item: (item[0], item[1]))

    if not args.send:
        if ready:
            print_send_plan(ready)
        if need_browser:
            print(
                "\nClassPass months that would need the Claude in Chrome fallback (only with --send): "
                + ", ".join(need_browser)
            )
        if errors:
            print_problems(errors)
        return bool(errors)

    sent, _ = deliver(ready, errors)
    if errors:
        print_problems(errors)
    if sent:
        notify(f"Sent: {_describe_sent(sent)}")
    if errors:
        alert(state, "\n".join(f"{label} {', '.join(whats)}: {problem}" for (label, problem), whats in errors.items()))
    return finish(not errors)


# --- schedule (macOS launchd) ---

def scheduler_python() -> str:
    """The Python for scheduled runs: this one, unless ClassPass would block it and a better one is installed."""
    if not TLS_SUSPECT:
        return sys.executable
    probe = "import ssl, sys; sys.exit('LibreSSL' in ssl.OPENSSL_VERSION or sys.version_info < (3, 9))"
    for candidate in (shutil.which("python3"), "/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        # /usr/bin/python3 is the system LibreSSL build (or, without the Command Line Tools, an installer stub).
        if candidate and candidate != "/usr/bin/python3" and Path(candidate).exists():
            if subprocess.run([candidate, "-c", probe], capture_output=True).returncode == 0:
                return candidate
    return sys.executable


def install_script(source: Path, destination: Path = INSTALLED_SCRIPT) -> Path:
    """launchd jobs can't read ~/Documents (macOS privacy), so the agent runs a copy kept under BASE_DIR."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
    except shutil.SameFileError:
        pass
    return destination


def build_plist(python: str, script: Path, claude: str | None, log_path: Path, receipts_home: str | None) -> bytes:
    environment = {
        "PATH": ":".join(([str(Path(claude).parent)] if claude else []) + ["/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
        "PYTHONUNBUFFERED": "1",
    }
    if claude:
        environment["RECEIPTS_CLAUDE_BIN"] = claude
    if receipts_home:
        environment["RECEIPTS_HOME"] = receipts_home
    return plistlib.dumps(
        {
            "Label": LAUNCH_LABEL,
            "ProgramArguments": [python, str(script), "auto", "--send"],
            "StartCalendarInterval": {"Hour": 10, "Minute": 0},
            "RunAtLoad": True,
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
            "EnvironmentVariables": environment,
        }
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def schedule(args: argparse.Namespace) -> None:
    if platform.system() != "Darwin":
        sys.exit("schedule uses launchd and only works on macOS")
    target = f"gui/{os.getuid()}/{LAUNCH_LABEL}"

    if args.action == "install":
        claude = claude_binary()
        if load_config().get("claude_fallback") and not claude:
            print("claude CLI not found: the Claude in Chrome fallback for ClassPass is on in the config but can't run")
        LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
        script = install_script(Path(__file__).resolve())
        plugin = plugin_script()
        if plugin:
            state = read_json(AUTO_STATE_PATH)
            state["plugin_script_sha256"] = sha256_file(plugin)
            write_json(AUTO_STATE_PATH, state)
        secure_log()
        LAUNCH_PLIST.write_bytes(
            build_plist(scheduler_python(), script, claude, RUN_LOG, os.environ.get("RECEIPTS_HOME"))
        )
        _launchctl("bootout", target)
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(LAUNCH_PLIST))
        if result.returncode != 0:
            sys.exit(f"launchctl bootstrap failed: {result.stderr.strip()}")
        print(f"Installed {LAUNCH_PLIST}")
        print(
            "It runs now (RunAtLoad), then daily at 10:00 and at each login. A run sends whatever is due: unsent"
            " past months, plus this month's receipt once its usual issue day has come. Problems are reported by"
            f" notification and by email to yourself. Logs: {RUN_LOG}"
        )
    elif args.action == "uninstall":
        _launchctl("bootout", target)
        LAUNCH_PLIST.unlink(missing_ok=True)
        print(f"Uninstalled {LAUNCH_LABEL}")
    else:
        loaded = _launchctl("print", target).returncode == 0
        state = read_json(AUTO_STATE_PATH)
        print(f"Agent: {'loaded' if loaded else 'not loaded'} ({LAUNCH_LABEL})")
        print(f"Plist: {LAUNCH_PLIST} ({'exists' if LAUNCH_PLIST.exists() else 'missing'})")
        print(f"Script: {INSTALLED_SCRIPT} ({'exists' if INSTALLED_SCRIPT.exists() else 'missing'})")
        if LAUNCH_PLIST.exists():
            python = plistlib.loads(LAUNCH_PLIST.read_bytes())["ProgramArguments"][0]
            print(f"Python: {python} ({'exists' if Path(python).exists() else 'MISSING: rerun schedule install'})")
        plugin = plugin_script()
        if not plugin:
            print("Plugin script: not found")
        else:
            same = INSTALLED_SCRIPT.exists() and sha256_file(plugin) == sha256_file(INSTALLED_SCRIPT)
            print(f"Plugin script: {plugin} ({'matches' if same else 'differs from'} the scheduled copy)")
        print(f"Last run: {state.get('last_run', 'never')}")
        print(f"Last success: {state.get('last_success', 'never')}")
        print("Checks: daily at 10:00 and at login")
        if get_auto_since():
            due, waiting = pending_receipts(load_sent_log())
            print("Due now: " + ("; ".join(f"{PROVIDERS[p]} {m}" for p, m in due) or "nothing"))
            if waiting:
                print(f"Not expected yet: {_describe_waiting(waiting)}")
        else:
            print("auto_since: unset (run: receipts.py config --auto-since YYYY-MM)")
        print(f"Claude in Chrome fallback for ClassPass: {'on' if claude_fallback_enabled() else 'off'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_parser = sub.add_parser("fetch", help="fetch receipt PDF(s), optionally email them")
    fetch_parser.add_argument(
        "provider", type=parse_providers, help=f"one or more of {', '.join(sorted(PROVIDERS))}, comma-separated"
    )
    fetch_parser.add_argument(
        "--month",
        type=parse_months,
        help="YYYY-MM, comma-separated, or a range YYYY-MM..YYYY-MM (default: previous month)",
    )
    fetch_parser.add_argument(
        "--url",
        action="append",
        help="ClassPass: Stripe receipt URL (repeatable); its month is read from the receipt's payment date",
    )
    fetch_parser.add_argument("--pdf", help="Import an already-downloaded PDF for --month")
    fetch_parser.add_argument("--send", action="store_true", help="Email the PDF(s)")
    fetch_parser.add_argument(
        "--force", action="store_true", help="Send even if this provider/month was already sent"
    )
    fetch_parser.set_defaults(func=fetch)

    status_parser = sub.add_parser("status", help="show which months were sent and which are missing")
    status_parser.add_argument("provider", nargs="?", type=parse_providers, help="limit to these providers")
    status_parser.add_argument("--since", type=lambda s: parse_months(s)[0], help="first month to show (YYYY-MM)")
    status_parser.add_argument("--offline", action="store_true", help="don't check Gmail's Sent folder")
    status_parser.set_defaults(func=status)

    sync_parser = sub.add_parser("sync", help="backfill the send log from Gmail's Sent folder")
    sync_parser.set_defaults(func=sync)

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
    config_parser.add_argument(
        "--from-env",
        action="store_true",
        help="with --set-app-password: take it from $GMAIL_APP_PASSWORD instead of prompting",
    )
    config_parser.add_argument(
        "--dialog",
        action="store_true",
        help="with --set-app-password: ask in a hidden-input macOS dialog (works without a terminal)",
    )
    config_parser.add_argument(
        "--claude-fallback",
        choices=["on", "off"],
        help="scheduled runs: if the ClassPass API method breaks, let `claude -p --chrome` drive your Chrome"
        " to find the receipt links (default: off)",
    )
    config_parser.add_argument("--auto-since", type=parse_month, help="earliest month `auto` may submit (YYYY-MM)")
    config_parser.add_argument(
        "--auto-providers",
        type=parse_providers,
        help=f"providers `auto` handles, comma-separated (default: {','.join(DEFAULT_AUTO_PROVIDERS)})",
    )
    config_parser.add_argument("--show", action="store_true", help="print current config")
    config_parser.set_defaults(func=configure)

    auto_parser = sub.add_parser("auto", help="unattended: fetch and send every receipt that's due (see config --auto-since)")
    auto_parser.add_argument("--send", action="store_true", help="actually email what's due (default: print the plan)")
    auto_parser.add_argument("--now", action="store_true", help="also try this month's receipts before their usual issue day")
    auto_parser.set_defaults(func=auto)

    schedule_parser = sub.add_parser("schedule", help="install/remove the macOS launchd agent that runs `auto --send`")
    schedule_parser.add_argument("action", choices=["install", "uninstall", "status"])
    schedule_parser.set_defaults(func=schedule)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
