---
description: Fetch monthly receipt PDFs (tmobile | classpass | verizon), without sending
argument-hint: "<provider[,provider]> [YYYY-MM | YYYY-MM..YYYY-MM]"
allowed-tools: Bash(python3:*)
---

Fetch receipt PDFs for provider(s) `$1` and month(s) `$2` (defaults to the previous month).
Providers can be comma-separated; months can be a comma list or a range like `2026-07..2026-09`.
Do NOT pass `--send` — this command only fetches.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" fetch <providers> --month <months>
```

If no provider was given, ask which (tmobile, classpass, verizon). The batch continues past
failures and lists every problem at the end:

- **tmobile** — automatic on macOS via the logged-in Chrome session. If T-Mobile rejects the
  session (401), the user must log in at https://www.t-mobile.com/bill/summary in Chrome (open
  it for them with Claude in Chrome if available). Chrome writes cookies to disk periodically,
  so wait ~30 seconds after they log in before retrying. "No bill for <month> yet" means the
  billing cycle hasn't closed.
- **classpass** — automatic on macOS via the logged-in Chrome session: the script reads the
  charges and their Stripe receipt links from ClassPass and renders each receipt. Each receipt's
  month is checked against its "Date paid". If the session has expired, the user must log in at
  https://classpass.com/settings/charges in Chrome (never submit a login form for them), then
  wait ~30 seconds and retry. "No ClassPass membership charge in <month> yet" means it hasn't
  been charged. Only if the automatic method fails for another reason, fall back to collecting
  the links by hand (with Claude in Chrome if available, otherwise ask the user):
  1. Open https://classpass.com/settings/charges; clicking a charge row opens its Stripe
     receipt in a new tab — collect the `pay.stripe.com/receipts/...` URLs.
  2. Pass them all in one run: `fetch classpass --url '<url1>' --url '<url2>'`.
- **verizon** — download the bill PDF from https://www.verizon.com/my-bill/ in a real Chrome
  session (via Claude in Chrome if available, else ask the user), then
  `fetch verizon --month <M> --pdf <downloaded file>`.

If the script fails because of missing dependencies or config, suggest `/emburse-receipts:setup`.

When done, show the saved PDFs and the printed send plan (with amounts/dates), and mention
`/emburse-receipts:send` to email them. Never print cookie values or credentials.
