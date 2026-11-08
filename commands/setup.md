---
description: One-shot setup — dependencies, Gmail, destination email, provider logins, and (optionally) automatic scheduled sending
allowed-tools: Bash(python3:*), AskUserQuestion
---

Set up the receipts plugin end to end. Keep interruptions to a minimum: gather everything you need
from the user in ONE round of questions (step 3), and only stop again for things only they can do
(app password, logging in) or for the single confirmation before automatic sending is switched on.

1. Run the doctor:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" doctor
   ```

2. Fix dependency `[FAIL]` lines yourself, without asking:
   - **playwright missing** → `python3 -m pip install playwright` (fall back to `pip3 install --user playwright` if the environment is externally managed, or offer a venv).
   - **chromium missing** → `python3 -m playwright install chromium`.

3. Ask the user, in a single message, for everything still missing:
   - **Destination email** (if `send_to` is unset): where receipts go. For Emburse/Chrome River this is their receipt inbox, e.g. `receipt@ca1.chromeriver.com` (the subdomain varies by company).
   - **Gmail address** (if unset): the address they send from. Warn them that their expense system only accepts receipts from addresses registered on their account (for Chrome River: their profile email or an Alternative Email under Preferences/Settings → Personal Settings), and ask them to confirm it's registered.
   - **Providers** they want: any of `tmobile`, `classpass`, `verizon`.
   - **Automatic sending?** If yes, the earliest month to submit (`YYYY-MM`) — nothing older is ever sent. Automatic sending needs macOS.

   Then apply the answers:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" config --send-to <email> --gmail-address <email> --auto-providers <providers> --auto-since <YYYY-MM>
   ```

   (Omit flags for values that were already set; omit `--auto-providers`/`--auto-since` if they don't want automatic sending. Leave `verizon` out of `--auto-providers` — it needs a manually downloaded PDF.)

4. **Gmail app password** (if the doctor reports it unset) — it must NOT pass through this chat.
   - If the doctor says it's exported in a shell file like `~/.zshrc`, offer to copy it into the keychain with the exact command the doctor prints (`zsh -ic '... config --set-app-password --from-env'`); run it only after the user agrees. The value is never shown.
   - Otherwise tell the user to create one at https://myaccount.google.com/apppasswords (requires 2-Step Verification) and run this in their own terminal (hidden prompt, stored in the macOS keychain), then wait for them:
     ```bash
     python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" config --set-app-password
     ```
   - **gmail login FAIL** → the address/app password pair is wrong or revoked; have them create a new one and store it as above.

5. Re-run the doctor until every required check passes (`[warn]` lines are provider-specific; mention them but don't block). Then run `status` so the send history is backfilled from Gmail's Sent folder:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" status
   ```

6. **Check provider logins by trying them** rather than asking. T-Mobile and ClassPass are fetched with the user's logged-in Google Chrome session (macOS). Fetch last month without sending:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" fetch <tmobile,classpass> --month <previous month>
   ```

   If a provider reports an expired session, ask the user to log in at the URL in the message in Chrome (never log in for them), wait ~30 seconds (Chrome writes cookies to disk periodically), and retry. "No bill/charge yet" is fine.

7. **Automatic sending** (only if they asked for it):
   1. Show what the first run would send — this sends nothing:
      ```bash
      python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" auto
      ```
   2. Get ONE explicit confirmation, stating plainly: installing sends everything listed as pending right away, then a launchd agent checks daily at 10:00 and at login and sends anything new, at most once every 14 days after a successful run, with no further confirmation. Duplicate protection (send log + Gmail Sent folder) still applies.
   3. On confirmation:
      ```bash
      python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" schedule install
      python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" schedule status
      ```
      The `claude` CLI is optional: if it's installed, scheduled runs can fall back to Claude in Chrome for ClassPass when the Chrome-session method fails.

8. Finish with a short summary: what's configured, what was sent (if anything), when the next automatic run is due (from `schedule status`), and that `/emburse-receipts:status` shows what's missing and `/emburse-receipts:send pending` submits it manually. Mention that notifications and `~/.claude-receipts/runs.log` report problems such as an expired login.

Never echo, log, or store the app password yourself, and never print cookie values or tokens.
