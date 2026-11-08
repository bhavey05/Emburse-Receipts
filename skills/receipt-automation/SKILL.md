---
name: receipt-automation
description: Fetch monthly receipt/bill PDFs (T-Mobile, ClassPass, Verizon) and email them to an expense system like Emburse. Use when the user asks to get, download, or submit a monthly receipt or bill for expenses, or asks which receipts they haven't submitted yet.
---

# Receipt automation

Everything runs through one script:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" status                     # sent vs. missing, per month
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" fetch <providers> [--month <months>] [--send]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" doctor                     # dependency + config check
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" config                     # show/update config (--auto-since, --auto-providers)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" auto [--send] [--now]       # unattended send of all pending months (plan only without --send)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" schedule install|uninstall|status  # macOS launchd agent: auto --send at login + daily 10:00, gated to 14 days
```

- `<providers>` is one or more of `tmobile,classpass,verizon`; `--month` takes `YYYY-MM`, a comma list, or a
  range `2026-07..2026-09` (default: previous month). One failing item doesn't stop the batch; all problems
  are listed at the end (exit code 1).
- "What haven't I submitted?" / "submit everything pending" → run `status` (it backfills history from
  Gmail's Sent folder) and use its "Not sent yet" list.
- Without `--send`, fetch only saves PDFs (under `~/.claude-receipts/receipts/`) and prints the send plan with
  each receipt's amount and date. Always show the plan and get the user's confirmation (once per batch)
  before rerunning with `--send` — it emails financial documents externally.
- If anything fails from missing dependencies or config, run `doctor` and fix what it reports (or point the
  user at `/emburse-receipts:setup`).
- Every send is recorded in `~/.claude-receipts/sent.json`; repeat sends for the same provider/month are
  skipped. Never add `--force` on your own — only when the user explicitly confirms a resend. `BLOCKED` means
  that exact receipt was already sent under another month: it's the wrong receipt, don't work around it.

## Providers

**T-Mobile** — automatic on macOS. The script reads the logged-in Chrome session's cookies (decrypted locally
via the keychain; never printed) and calls T-Mobile's bill APIs. A month = the bill whose cycle ends in it.
On a session error (401), the user needs to log in at https://www.t-mobile.com/bill/summary in Chrome; wait
~30s after login (Chrome flushes cookies to disk periodically) before retrying.

**ClassPass** — automatic on macOS, like T-Mobile: the script uses the logged-in Chrome session's cookies to
read the charges and their Stripe receipt links from ClassPass's API, then renders each receipt. A month = the
month the charge was paid; each receipt's "Date paid" is checked. On an expired session, the user needs to log
in at https://classpass.com/settings/charges in Chrome (never submit a login for them) and wait ~30s. Only if
the automatic method fails for another reason: collect the `pay.stripe.com/receipts/...` links by clicking
each charge on that page (Claude in Chrome if available, otherwise ask the user) and pass them with
`fetch classpass --url '<u1>' --url '<u2>'`.

**Verizon** — no stable API (Verizon shuts down reverse-engineered endpoints). Download the bill PDF from
https://www.verizon.com/my-bill/ in a real Chrome session (via Claude in Chrome if available, otherwise ask
the user), then import it: `fetch verizon --month <M> --pdf <path>`.

Any provider also accepts `--pdf <path>` to import an already-downloaded PDF.

## Adding a provider

Add a `<name>_pdf(month) -> (Path, info)` function in `scripts/receipts.py` (raise `ReceiptError` with a
what-to-do message when it can't fetch; record metadata with `record_receipt`), then register it in
`PROVIDERS` and `FETCHERS`. Prefer, in order: a public receipt URL rendered via Playwright (like ClassPass),
an API call using the Chrome session cookies (like T-Mobile), or a browser-download + `--pdf` import (like
Verizon).

## Security rules

- Never print, log, or store cookie values, tokens, or the Gmail app password.
- The app password must not pass through chat: the user sets it via `config --set-app-password` (hidden
  prompt, macOS keychain), or — with their OK — copy it from their shell profile with
  `zsh -ic '... config --set-app-password --from-env'` (the value is never displayed).
- Always get explicit confirmation before `--send`. `auto --send` and `schedule install` also email receipts
  immediately — run them only with the user's explicit OK (`/emburse-receipts:setup` covers this).

## Troubleshooting

- A sent receipt never shows up in the expense system: the sending Gmail address most likely isn't registered
  on the user's expense account — Emburse/Chrome River silently drops email from unrecognized senders. It must
  match the profile email or be an alternative email (Preferences/Settings → Personal Settings → Alternative
  Emails in Chrome River).
- `doctor` says the app password is in `~/.zshrc` but unset: the Claude desktop app doesn't load shell rc files;
  store it in the keychain as above.
