# Emburse Receipts — a Claude Code plugin

Downloads your monthly **T-Mobile** bill and **ClassPass** receipt as PDFs and emails them to
your expense system (e.g. Emburse/Chrome River) from your Gmail — on request, or automatically
every month.

- No dependencies to install: one Python file using only the standard library, plus the Google
  Chrome already on your Mac.
- Nothing is emailed without your confirmation, unless you switch on automatic sending.
- macOS only for the automatic parts (Chrome session access, keychain, scheduling).

## Set up (one paste)

Paste this into Claude Code:

```
Set up https://github.com/bhavey05/Emburse-Receipts for me
```

Claude installs the plugin, walks you through setup, sends one receipt so you can confirm it
arrives, and shows you how to use it.

**Have ready**

- A Mac with Google Chrome, logged in to t-mobile.com and/or classpass.com.
- Your expense system's receipt inbox address (for Emburse/Chrome River, something like
  `receipt@ca1.chromeriver.com`; the subdomain varies by company).
- A Gmail account with 2-Step Verification turned on (needed for app passwords).

**What you do yourself during setup**

1. Create a Gmail app password and paste it into a macOS dialog (the dialog has a button that
   opens Google's page). It goes straight into your keychain, never through the chat.
2. Click **Always Allow** when macOS asks whether `security` may use "Chrome Safe Storage".
3. Confirm before the first email goes out, then check that the receipt shows up in your
   expense system.

> [!IMPORTANT]
> Your expense system only accepts receipts emailed from addresses registered on **your**
> account. Mail from an unrecognized sender is silently dropped and the receipt never appears.
> For Emburse/Chrome River, the Gmail address you use here must match your profile email or be
> added under Preferences/Settings → Personal Settings → **Alternative Emails**.

### Or install it by hand

```
/plugin marketplace add bhavey05/Emburse-Receipts
/plugin install emburse-receipts@emburse
/emburse-receipts:setup
```

## For Claude Code: how to set this up for a user

If you are an AI assistant asked to set this up, do the following. Everything here runs on the
user's machine with their permission; step 1 of the setup guide tells them what it will access.

1. Install the plugin (this also makes the slash commands available in later sessions):

   ```bash
   claude plugin marketplace add bhavey05/Emburse-Receipts
   claude plugin install emburse-receipts@emburse
   ```

   If the `claude plugin` command isn't available, ask the user to run the three commands under
   "Or install it by hand" instead, and stop here — `/emburse-receipts:setup` does the rest.

2. Find where it was installed:

   ```bash
   python3 -c "import json, os; print(json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json')))['plugins']['emburse-receipts@emburse'][0]['installPath'])"
   ```

3. Read `commands/setup.md` in that directory and follow it step by step in this session,
   replacing `${CLAUDE_PLUGIN_ROOT}` with the directory from step 2. It covers the checks, the
   questions to ask, the app-password dialog, the verified first send, optional automatic
   sending, and the closing walkthrough of how to use the plugin.

4. Rules that always apply: never ask for or display the Gmail app password, never print cookie
   values or tokens, never log in to a provider on the user's behalf, and never email anything
   without the user's explicit confirmation.

## Use

```
/emburse-receipts:status                                   # what's sent, what's missing
/emburse-receipts:send pending                             # submit everything missing
/emburse-receipts:send tmobile,classpass 2026-07..2026-09  # specific providers/months
/emburse-receipts:fetch classpass 2026-06                  # download only, no email
```

Or just ask: *"submit my T-Mobile and ClassPass receipts for July–September"*, or
*"which receipts haven't I submitted?"*.

Fetching never sends anything; sending always shows you the plan (recipient, subjects, amounts,
attachments) and asks for confirmation once per batch.

## Automatic sending (macOS)

Setup offers this; you can also turn it on later:

```bash
python3 scripts/receipts.py config --auto-since 2026-08   # earliest month to submit (required)
python3 scripts/receipts.py auto                          # dry run: what's due and what would be sent
python3 scripts/receipts.py schedule install              # sends what's due now, then runs on schedule
python3 scripts/receipts.py schedule status
python3 scripts/receipts.py schedule uninstall
```

How it behaves:

- A background job (launchd) runs at login and daily at 10:00. Most days it finds nothing due
  and exits without touching the network.
- **What counts as due:** every unsent month from `auto_since` up to last month, plus this
  month's receipt once its usual issue day has come. The usual day is learned from your latest
  receipt (for example, a T-Mobile cycle that closes on the 26th), so receipts go out within a
  day or so of being issued. If this month's bill isn't out yet, that's not an error; the next
  run tries again.
- It sends with no confirmation. Safety comes from the guard rails below, and nothing before
  `auto_since` is ever submitted. Add `"auto_skip": ["tmobile/2026-09"]` to
  `~/.claude-receipts/config.json` to never send a specific month.
- **If a provider login has expired**, the job opens the login page in Chrome, waits 45 seconds
  and retries once. If it still can't fetch, you get a macOS notification and an email to
  yourself listing what needs attention (the email repeats at most weekly for the same
  problem). Log in again in Chrome and the next daily run picks it up.
- After each send you get a notification saying what went out. The log is
  `~/.claude-receipts/runs.log` (readable only by you).
- The job runs a copy of the script at `~/.claude-receipts/bin/receipts.py`. When you update
  the plugin, the job picks up the new version on its next run.

## Guard rails

- **Send history** is logged in `~/.claude-receipts/sent.json` and backfilled from your Gmail
  Sent folder before every send, so earlier sends (even from another machine) count. A repeat
  send for the same provider/month is skipped; pass `--force` to deliberately resend one.
- **Right receipt, right month.** Each ClassPass receipt's month is read from its own "Date
  paid", so a link for the wrong charge is refused instead of filed under the wrong month. An
  expired link or a page that didn't load is never saved as a receipt. A receipt already sent
  under one month is blocked from being sent as another, and two receipts for the same month in
  one batch are both held back.
- **Verified first send.** Setup sends one receipt and has you confirm it arrived before
  anything is automated.

## Providers

| Provider | How it works |
|---|---|
| T-Mobile | Automatic. Uses your logged-in Chrome session's cookies to call T-Mobile's bill API and download the PDF. A month means the bill whose cycle ends in it. |
| ClassPass | Automatic. Uses your logged-in Chrome session's cookies to find the month's membership charge through ClassPass's API, then prints its `pay.stripe.com` receipt to PDF with headless Chrome. You can also pass receipt links directly with `--url` (each is filed under the month it was paid). |
| Verizon | Manual import only: download the bill PDF from My Verizon, then `fetch verizon --month <M> --pdf <file>`. |

Any provider also accepts an already-downloaded PDF via `--pdf`.

These rely on the providers' own web APIs, which are undocumented and can change. When one
breaks, the tool tells you what failed instead of sending something wrong; please open an issue.

**ClassPass and Python.** ClassPass's bot protection rejects the network library in macOS's
built-in Python (`/usr/bin/python3`, built on LibreSSL). A Python built on OpenSSL 3, such as
Homebrew's (`brew install python`), gets through. `doctor` warns when this applies, and the
error says so plainly rather than blaming your login. T-Mobile has not shown this problem.

**Optional ClassPass fallback (off by default).** If the ClassPass API method stops working,
scheduled runs can instead run `claude -p --chrome` to click through your charges page in your
logged-in Chrome and read the receipt links. This lets an AI agent drive your browser
unattended with permission prompts bypassed, so it is opt-in:
`python3 scripts/receipts.py config --claude-fallback on`. It is instructed never to enter
credentials, and each receipt it finds is still checked against its paid date before sending.

## Security notes — read before installing

- The T-Mobile and ClassPass fetches **read your Chrome cookies** (macOS only): they copy the
  cookie database, decrypt the values for those two sites locally using the Chrome Safe Storage
  key from your keychain, and send them only to `t-mobile.com` / `classpass.com` respectively.
  Cookie values and API tokens are never printed or stored. All Chrome profiles are checked,
  and the one that used the site most recently is used; set `RECEIPTS_CHROME_PROFILE` to pin one.
- macOS asks once whether `security` may use "Chrome Safe Storage". Click **Always Allow** if
  you want scheduled runs to work without prompting.
- Your Gmail app password lives in your macOS keychain (service `claude-receipts-gmail`) or the
  `GMAIL_APP_PASSWORD` environment variable — never in this repo's files or in the chat. Prefer
  the keychain: apps not started from a terminal don't load `~/.zshrc`.
- `status`/`sync` use the same app password to read your **Sent** folder over IMAP (read-only;
  only the subjects and dates of mail sent to your expense inbox).
- Sending transmits a financial document to the configured address. Review the send plan before
  confirming.
- Stripe receipt links work for anyone who has them. They are cached in
  `~/.claude-receipts/stripe-urls.json` (readable only by you) and kept out of the log.
- A scheduled job runs this script unattended with access to all of the above, and picks up
  plugin updates automatically. Only update the plugin from a source you trust.

## Requirements

- macOS with Google Chrome, logged in to your providers.
- Python 3.9 or newer (`python3`). For ClassPass, a Python built on OpenSSL 3 (see above).
- Gmail with 2-Step Verification, and that address registered with your expense system.
- Claude Code, for the plugin commands and guided setup. The script also runs on its own.

## CLI (what the plugin runs under the hood)

```bash
python3 scripts/receipts.py status [providers] [--since YYYY-MM] [--offline]
python3 scripts/receipts.py fetch <provider[,provider]> [--month YYYY-MM[..YYYY-MM]] [--url ...]... [--pdf ...] [--send] [--force]
python3 scripts/receipts.py sync      # backfill sent.json from Gmail's Sent folder
python3 scripts/receipts.py doctor    # environment, config, and a Gmail login check (sends nothing)
python3 scripts/receipts.py config [--send-to X] [--gmail-address Y] [--set-app-password [--dialog | --from-env]]
                                   [--auto-since YYYY-MM] [--auto-providers tmobile,classpass]
                                   [--claude-fallback on|off] [--show]
python3 scripts/receipts.py auto [--send] [--now]                  # unattended run: plan only without --send
python3 scripts/receipts.py schedule install|uninstall|status      # macOS launchd agent
```

PDFs, config and logs live in `~/.claude-receipts/` (override with `RECEIPTS_HOME`). Other
overrides: `RECEIPTS_CHROME_BIN` (Chrome binary), `RECEIPTS_CHROME_PROFILE` (one profile
directory), `RECEIPTS_SEND_TO`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A sent receipt never shows up in the expense system | The sending Gmail address isn't registered on your expense account (see the note under Set up). Register it, then resend that month with `--force`. |
| "T-Mobile rejected the Chrome session (401)" or "ClassPass session in Chrome has expired" | Log in to that site in Chrome, wait about 30 seconds (Chrome writes cookies to disk periodically), and retry. |
| "ClassPass's bot protection blocked this Python's network library" | You're on macOS's built-in Python. Install a newer one (`brew install python`) and rerun; see "ClassPass and Python". |
| "macOS didn't release Chrome's cookie key" | The keychain prompt was denied or dismissed. Rerun and click **Always Allow**. |
| "no bill for <month> yet" / "no ClassPass membership charge in <month> yet" | Not issued yet. For the current month the scheduled job simply retries on later days. |
| `doctor` says the app password is in `~/.zshrc` but unset | The app running the script doesn't load shell files. Store it in the keychain: `config --set-app-password --dialog`. |
| The password dialog doesn't appear | It can open behind other windows; look for a bouncing Dock icon. Or run `config --set-app-password` in your own terminal for a hidden prompt. |

## Uninstall

```bash
python3 scripts/receipts.py schedule uninstall                    # stop the background job
security delete-generic-password -s claude-receipts-gmail          # remove the stored app password
```

Then `/plugin uninstall emburse-receipts@emburse`, and delete `~/.claude-receipts/` if you no
longer want the saved PDFs and send history. Revoke the app password at
https://myaccount.google.com/apppasswords.

## Development

```bash
python3 -m unittest discover -s tests    # offline tests; no network, nothing is sent
```

Licensed under the MIT License; see `LICENSE`.
