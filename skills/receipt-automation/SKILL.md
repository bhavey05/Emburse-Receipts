---
name: receipt-automation
description: Fetch monthly receipt/bill PDFs (T-Mobile, ClassPass, Verizon) and email them to an expense system like Emburse. Use when the user asks to get, download, or submit a monthly receipt or bill for expenses.
---

# Receipt automation

Everything runs through one script:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" fetch <tmobile|classpass|verizon> [--month YYYY-MM] [--send]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" doctor   # dependency + config check
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" config   # show/update config
```

- `--month` defaults to the previous month; accepts comma-separated months.
- Without `--send`, fetch only saves the PDF (under `~/.claude-receipts/receipts/`) and prints the send plan. Always show the user the send plan and get their confirmation before rerunning with `--send` — it emails a financial document externally.
- If anything fails from missing dependencies or config, run `doctor` and fix what it reports (or point the user at `/emburse-receipts:setup`).
- Every send is recorded in `~/.claude-receipts/sent.json` and repeat sends for the same provider/month are skipped with a message. Never add `--force` on your own — only when the user explicitly confirms they want to send a duplicate.

## Providers

**T-Mobile** — fully automatic on macOS. The script reads the logged-in Chrome session's cookies (decrypted locally via the keychain; never printed) and calls T-Mobile's bill APIs. On a 401/session error, the user needs to log in at t-mobile.com in Chrome and retry.

**ClassPass** — blocks automated browsers, so the first fetch of a month needs a Stripe receipt URL from a real Chrome session: open https://classpass.com/settings/charges, click the charge for the month, copy the `pay.stripe.com/receipts/...` URL, then `fetch classpass --month <M> --url '<url>'`. If Claude-in-Chrome tools are available, do these steps yourself in the user's browser. URLs are cached, so reruns don't need the URL again.

**Verizon** — no stable API (Verizon shuts down reverse-engineered endpoints). Download the bill PDF from https://www.verizon.com/my-bill/ in a real Chrome session (do it via Claude-in-Chrome if available, otherwise ask the user), then import it: `fetch verizon --month <M> --pdf <path>`.

Any provider also accepts `--pdf <path>` to import an already-downloaded PDF.

## Adding a provider

Add a `<name>_pdf(month) -> Path` function in `scripts/receipts.py`, register it in `PROVIDERS` and the dispatch in `fetch()`. Prefer, in order: a public receipt URL rendered via Playwright (like ClassPass), an API call using the Chrome session cookies (like T-Mobile), or a browser-download + `--pdf` import (like Verizon).

## Security rules

- Never print, log, or store cookie values, tokens, or the Gmail app password.
- The app password must not pass through chat: the user sets it themselves via `config --set-app-password` (hidden prompt, macOS keychain) or `GMAIL_APP_PASSWORD` in their shell profile.
- Always get explicit confirmation before `--send`.

## Troubleshooting

If a sent receipt never shows up in the expense system, the most likely cause
is that the sending Gmail address isn't registered on the user's expense
account — Emburse/Chrome River silently drops email from unrecognized senders.
The Gmail address must match the user's profile email or be added as an
alternative email (Preferences/Settings → Personal Settings → Alternative
Emails in Chrome River).
