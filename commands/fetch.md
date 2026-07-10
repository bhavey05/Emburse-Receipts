---
description: Fetch a monthly receipt PDF (tmobile | classpass | verizon), without sending
argument-hint: <provider> [YYYY-MM]
allowed-tools: Bash(python3:*)
---

Fetch a receipt PDF for provider `$1` and month `$2` (defaults to the previous month if omitted). Do NOT pass `--send` — this command only fetches.

Base command:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" fetch <provider> --month <YYYY-MM>
```

If no provider was given, ask which one (tmobile, classpass, or verizon).

Provider notes:

- **tmobile** — fully automatic on macOS via the logged-in Chrome session. If it fails with a 401/session error, ask the user to log in at https://www.t-mobile.com/my-account/dashboard in Chrome and retry.
- **classpass** — needs a Stripe receipt URL the first time for each month. If the script exits asking for one:
  - If Claude-in-Chrome browser tools are available: open https://classpass.com/settings/charges in the user's Chrome, click the charge row for the target month (it opens a `pay.stripe.com/receipts/...` URL), grab that URL, then rerun with `--url '<stripe_url>'`.
  - Otherwise, give the user those same steps and ask them to paste the URL.
- **verizon** — needs a bill PDF downloaded from a real session (Verizon has no stable API).
  - If Claude-in-Chrome browser tools are available: open https://www.verizon.com/my-bill/ in the user's Chrome, open the bill for the target month, download the PDF, then rerun with `--pdf <downloaded file>`.
  - Otherwise, give the user those steps and ask for the path of the downloaded PDF.

If the script fails because of missing dependencies or config, suggest running `/receipts:setup`.

When done, show the user the saved PDF path and the printed send plan, and mention `/receipts:send` to actually email it. Never print cookie values or credentials.
