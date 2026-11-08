# Emburse Receipts — a Claude Code plugin

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
/emburse-receipts:status                                   # what's sent, what's missing
/emburse-receipts:send pending                             # submit everything missing
/emburse-receipts:send tmobile,classpass 2026-07..2026-09  # specific providers/months
/emburse-receipts:fetch classpass 2026-06                  # fetch only, no email
```

Or just ask: *"submit my T-Mobile and ClassPass receipts for July–September"*, or
*"which receipts haven't I submitted?"*.

Fetching never sends anything; sending always shows you the plan (recipient,
subjects, amounts, attachments) and asks for confirmation once per batch.

Guard rails:

- **Send history** is logged in `~/.claude-receipts/sent.json` and backfilled
  from your Gmail Sent folder, so earlier sends count too. A repeat send for the
  same provider/month is skipped; pass `--force` to deliberately resend.
- **Right receipt, right month.** Each ClassPass receipt's month is read from its
  "Date paid", so a link for the wrong charge is refused instead of filed under
  the wrong month. Expired Stripe links are caught before they become a PDF. And
  a receipt already sent under one month is blocked from being sent as another.

## Providers

| Provider | How it works |
|---|---|
| T-Mobile | Fully automatic (macOS): uses your logged-in Chrome session's cookies to call T-Mobile's bill API and download the PDF. Log in at t-mobile.com in Chrome first. |
| ClassPass | ClassPass blocks bots, so each charge needs its `pay.stripe.com` receipt URL from https://classpass.com/settings/charges (Claude in Chrome can grab them for you; links expire after a few weeks). Pass several `--url`s at once — each is filed under the month it was paid. |
| Verizon | No stable API — download the bill PDF from https://www.verizon.com/my-bill/ (Claude in Chrome can do it) and the plugin imports it. |

Any provider also accepts an already-downloaded PDF via `--pdf`.

## CLI (what the plugin runs under the hood)

```bash
python3 scripts/receipts.py status [providers] [--since YYYY-MM] [--offline]
python3 scripts/receipts.py fetch <provider[,provider]> [--month YYYY-MM[..YYYY-MM]] [--url ...]... [--pdf ...] [--send] [--force]
python3 scripts/receipts.py sync      # backfill sent.json from Gmail's Sent folder
python3 scripts/receipts.py doctor    # dependencies, config, and a Gmail login check (sends nothing)
python3 scripts/receipts.py config [--send-to X] [--gmail-address Y] [--set-app-password [--from-env]] [--auto-since YYYY-MM] [--auto-providers tmobile,classpass] [--show]
python3 scripts/receipts.py auto [--send] [--now]                  # unattended run: plan only without --send
python3 scripts/receipts.py schedule install|uninstall|status      # macOS launchd agent
```

PDFs and config live in `~/.claude-receipts/` (override with `RECEIPTS_HOME`).

## Scheduled runs (macOS)

`schedule install` sets up a launchd agent that runs `auto --send` at login and daily at 10:00. Each run is
skipped unless the last *successful* run was 14+ days ago (`--now` overrides). It sends with no confirmation;
safety comes from the guard rails above, and nothing before `auto_since` is ever submitted.

```bash
python3 scripts/receipts.py config --auto-since 2026-08   # earliest month to submit (required)
python3 scripts/receipts.py auto                          # dry run: shows what's pending and what would be fetched
python3 scripts/receipts.py schedule install              # runs once right away (RunAtLoad), then on schedule
python3 scripts/receipts.py schedule status
python3 scripts/receipts.py schedule uninstall
```

- Automated providers: T-Mobile and ClassPass (`config --auto-providers` to change; Verizon needs a manually
  downloaded PDF). Add `"auto_skip": ["tmobile/2026-09"]` to `config.json` to never send specific months.
- It covers every month from `auto_since` through last month that isn't in the send log.
- T-Mobile: if the Chrome session was rejected, it opens T-Mobile in Chrome, waits 45s, and retries once.
- ClassPass: with no cached receipt, it runs `claude -p --chrome` to click the charges in your logged-in Chrome
  and read the Stripe receipt URLs (Claude in Chrome required; it never enters credentials). Each receipt's
  month is verified before sending.
- Notifications report what was sent or what needs attention. Logs: `~/.claude-receipts/runs.log`.
- `install` creates `~/.claude-receipts/venv` with Playwright; the agent needs the `claude` CLI on your machine.
- The agent runs a copy at `~/.claude-receipts/bin/receipts.py` (launchd can't read `~/Documents`); rerun `schedule install` after updating the script.

## Security notes, read before installing

- The T-Mobile fetch **reads your Chrome cookies** (macOS only): it copies the
  cookie DB, decrypts values locally using the Chrome Safe Storage key from
  your keychain, and sends them only to `t-mobile.com`. Cookie values are
  never printed or stored.
- Your Gmail app password lives in your macOS keychain (service
  `claude-receipts-gmail`) or the `GMAIL_APP_PASSWORD` env var — never in this
  repo's files. Prefer the keychain: the Claude desktop app doesn't load
  `~/.zshrc`, so an env var set there is invisible to it (`doctor` detects this
  and prints a one-line command to copy it into the keychain).
- `status`/`sync` use the same app password to read the **Sent** folder over
  IMAP (read-only; only subjects and dates of mail sent to your expense inbox).
- Sending transmits a financial document to the configured address. Review the
  send plan before confirming.

## Requirements

macOS (T-Mobile fetch + keychain; other providers work anywhere), Python 3.9+,
Google Chrome with a logged-in session for the provider, Gmail with 2-Step
Verification (for app passwords).
