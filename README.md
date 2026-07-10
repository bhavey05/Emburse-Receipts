# Receipts — a Claude Code plugin

Fetches monthly receipt/bill PDFs (**T-Mobile**, **ClassPass**, **Verizon**) and
emails them to your expense system (e.g. Emburse/Chrome River) via Gmail.

## Install

```
/plugin marketplace add bhavey05/Emburse-Receipts
/plugin install emburse-receipts@emburse
```

Then run `/emburse-receipts:setup` — it checks dependencies (Python 3.9+, Playwright,
Chromium) and walks you through configuration:

- **Destination email** — where receipts get sent (for Emburse this is your
  company's receipt inbox, e.g. `receipt@ca1.chromeriver.com`).
- **Gmail address + app password** — used to send. Create an app password at
  https://myaccount.google.com/apppasswords. It's stored in your macOS
  keychain via a hidden prompt (or set `GMAIL_APP_PASSWORD` in your shell
  profile) — it never passes through the chat.

> [!IMPORTANT]
> Your expense system only accepts receipts emailed from addresses registered
> on **your** account — mail from an unrecognized sender is silently dropped,
> and the receipt never appears. For Emburse/Chrome River, the Gmail address
> you configure here must match your profile email, or be added under
> Preferences/Settings → Personal Settings → **Alternative Emails**.

## Use

```
/emburse-receipts:fetch tmobile            # previous month
/emburse-receipts:fetch classpass 2026-06
/emburse-receipts:send verizon 2026-06     # fetch + confirm + email
```

Or just ask: *"submit my T-Mobile bill for June to Emburse"*.

Fetching never sends anything; sending always shows you the plan (recipient,
subject, attachment) and asks for confirmation first. Every send is logged in
`~/.claude-receipts/sent.json`, and a repeat send for the same provider/month
is skipped with a warning — pass `--force` to deliberately resend.

## Providers

| Provider | How it works |
|---|---|
| T-Mobile | Fully automatic (macOS): uses your logged-in Chrome session's cookies to call T-Mobile's bill API and download the PDF. Log in at t-mobile.com in Chrome first. |
| ClassPass | ClassPass blocks bots, so the first fetch of a month needs the `pay.stripe.com` receipt URL from https://classpass.com/settings/charges (Claude in Chrome can grab it for you). Cached afterwards. |
| Verizon | No stable API — download the bill PDF from https://www.verizon.com/my-bill/ (Claude in Chrome can do it) and the plugin imports it. |

Any provider also accepts an already-downloaded PDF via `--pdf`.

## CLI (what the plugin runs under the hood)

```bash
python3 scripts/receipts.py fetch <provider> [--month YYYY-MM] [--url ...] [--pdf ...] [--send]
python3 scripts/receipts.py doctor
python3 scripts/receipts.py config [--send-to X] [--gmail-address Y] [--set-app-password] [--show]
```

PDFs and config live in `~/.claude-receipts/` (override with `RECEIPTS_HOME`).

## Security notes, read before installing

- The T-Mobile fetch **reads your Chrome cookies** (macOS only): it copies the
  cookie DB, decrypts values locally using the Chrome Safe Storage key from
  your keychain, and sends them only to `t-mobile.com`. Cookie values are
  never printed or stored.
- Your Gmail app password lives in your macOS keychain (service
  `claude-receipts-gmail`) or the `GMAIL_APP_PASSWORD` env var — never in this
  repo's files.
- Sending transmits a financial document to the configured address. Review the
  send plan before confirming.

## Requirements

macOS (T-Mobile fetch + keychain; other providers work anywhere), Python 3.9+,
Google Chrome with a logged-in session for the provider, Gmail with 2-Step
Verification (for app passwords).
