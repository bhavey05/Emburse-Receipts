# Receipt Automation

Fetches monthly receipt PDFs and emails them to Emburse
(`receipt@ca1.chromeriver.com`). Everything lives in `receipts.py`.

```bash
python3 receipts.py <classpass|tmobile> [--month YYYY-MM] [--send]
```

- `--month` defaults to the previous month; accepts comma-separated months.
- Without `--send`, the script only fetches the PDF and prints the send plan.
  Add `--send` to actually email Emburse (one email per receipt).

## T-Mobile

Fully automatic, as long as you are logged in to t-mobile.com in Chrome
(the script reads the local Chrome session's cookies; sessions expire after
a few weeks — if you get a 401, log in again at
https://www.t-mobile.com/my-account/dashboard):

```bash
python3 receipts.py tmobile --month 2026-05
python3 receipts.py tmobile --month 2026-05 --send
```

PDFs are stored in `~/.tmobile-receipt-automation/receipts/`.

## ClassPass

ClassPass blocks automated browsers, so the receipt URL must come from a real
Chrome session (e.g. via Claude in Chrome):

1. Open https://classpass.com/settings/charges.
2. Click the charge row for the target month — it opens the Stripe receipt in
   a new tab.
3. Copy the `https://pay.stripe.com/receipts/...` URL and run:

```bash
python3 receipts.py classpass --month 2026-05 --url '<stripe_url>'
python3 receipts.py classpass --month 2026-05 --send
```

Stripe URLs are public (token in the URL), so headless Playwright renders the
PDF fine. URLs are cached in `~/.classpass-automation/stripe-urls.json`;
PDFs in `~/.classpass-automation/receipts/`. Reruns for a cached month never
need `--url` again.

## Setup

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

Sending uses Gmail SMTP with `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` from
`~/.zshrc`. Sending transmits payment details to Emburse, so always review the
send plan (run without `--send`) before sending.
