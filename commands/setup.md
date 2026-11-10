---
description: One-shot setup — checks, Gmail, destination email, provider logins, a verified first send, and (optionally) automatic scheduled sending
allowed-tools: Bash(python3:*), AskUserQuestion
---

Set up the receipts plugin end to end, then show the user how to use it. Keep interruptions to a
minimum: gather everything you need in ONE round of questions (step 3), and only stop again for
things only the user can do (app password, keychain prompt, logging in, checking their expense
system) and for the confirmations before email is sent.

If you are following this file directly rather than through `/emburse-receipts:setup` (for example
right after installing the plugin in this session), replace `${CLAUDE_PLUGIN_ROOT}` below with the
plugin's install directory.

There is nothing to install: the script uses only Python's standard library and the Google Chrome
that is already on the Mac.

1. **Tell the user what is about to happen** in a few lines, then continue unless they object:
   - It reads their logged-in Chrome session for t-mobile.com / classpass.com (cookies are decrypted
     locally and sent only to those sites) to download bills and receipts.
   - It emails those PDFs from their Gmail to their expense inbox. Nothing is emailed without their
     confirmation during setup.
   - They will be asked to do three things themselves: paste a Gmail app password into a macOS
     dialog, click **Always Allow** on one keychain prompt, and check that the first receipt
     arrives in their expense system.

2. Run the doctor:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" doctor
   ```

   - **`[FAIL] Google Chrome`** → they need Google Chrome installed and logged in to their providers.
   - **`[warn] python network library (LibreSSL ...)`** → only matters for ClassPass, whose bot
     protection blocks macOS's built-in Python. If they want ClassPass, they need a newer Python:
     with their OK run `brew install python` (or have them install it from python.org), confirm
     `python3` now resolves to it, and rerun the doctor. T-Mobile hasn't shown this problem.
   - Config `[FAIL]` lines are expected on a first run; the next steps fix them.

3. Ask the user, in a single message, for everything still missing:
   - **Destination email** (if `send_to` is unset): where receipts go. For Emburse/Chrome River this is their receipt inbox, e.g. `receipt@ca1.chromeriver.com` (the subdomain varies by company).
   - **Gmail address** (if unset): the address they send from. Warn them that their expense system only accepts receipts from addresses registered on their account (for Chrome River: their profile email or an Alternative Email under Preferences/Settings → Personal Settings), and ask them to confirm it's registered.
   - **Providers** they want: `tmobile`, `classpass`, or both.
   - **Automatic sending?** If yes, the earliest month to submit (`YYYY-MM`) — nothing older is ever sent automatically. Suggest the first month they have NOT already expensed.

   Then apply the answers:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" config --send-to <email> --gmail-address <email> --auto-providers <providers> --auto-since <YYYY-MM>
   ```

   (Omit flags for values that were already set; omit `--auto-providers`/`--auto-since` if they don't want automatic sending.)

4. **Gmail app password** (if the doctor reports it unset) — it must NOT pass through this chat.
   - If the doctor says it's exported in a shell file like `~/.zshrc`, offer to copy it into the keychain with the exact command the doctor prints (`zsh -ic '... config --set-app-password --from-env'`); run it only after the user agrees. The value is never shown.
   - Otherwise tell the user a dialog titled "Emburse receipts" is about to appear (it may open behind other windows — look for a bouncing Dock icon), that its **Create one** button opens Google's app-password page (requires 2-Step Verification), and that they paste the 16-letter password there and click **Save**. Then run this and wait for it to return:
     ```bash
     python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" config --set-app-password --dialog
     ```
     It stores the password in the macOS keychain and prints whether Gmail accepted it. "No password provided" means they cancelled; offer to try again.
   - **gmail login FAIL** → the address/app password pair is wrong or revoked; have them create a new one and store it as above.

5. Re-run the doctor until every required check passes (`[warn]` lines are provider-specific; mention them but don't block). Then run `status` so the send history is backfilled from Gmail's Sent folder:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" status
   ```

6. **Check provider logins by trying them** rather than asking. Before running, tell the user: macOS will ask whether `security` may use "Chrome Safe Storage" — they should enter their Mac login password and click **Always Allow** (plain "Allow" makes the prompt come back on every scheduled run). Then fetch last month without sending:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" fetch <tmobile,classpass> --month <previous month>
   ```

   - Expired session → ask the user to log in at the URL in the message in Chrome (never log in for them), wait ~30 seconds (Chrome writes cookies to disk periodically), and retry.
   - "No bill/charge yet" is fine.
   - "ClassPass's bot protection blocked this Python" → see the Python note in step 2.

7. **Verified first send.** Expense systems silently drop mail from unregistered senders, so prove one receipt arrives before anything is automated. Skip this step if `status` already shows earlier sends to this destination.
   1. Show the send plan from step 6 (recipient, subjects, amounts) and ask for confirmation to send it.
   2. On confirmation, rerun the step 6 command with `--send`.
   3. Ask the user to open their expense system and confirm the receipt appeared (it can take a few minutes). If it didn't, the Gmail address isn't registered on their account — have them add it (Chrome River: Alternative Emails) and resend with `--force` for that single provider and month. Don't enable automatic sending until a receipt has arrived, unless the user explicitly chooses to go ahead.
   4. If there is nothing to send (no bill issued yet, or they already expensed that month and don't want a duplicate), say plainly that delivery is unverified and let them decide whether to continue.

8. **Automatic sending** (only if they asked for it; needs macOS):
   1. Show what the first run would send — this sends nothing:
      ```bash
      python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" auto
      ```
   2. Get ONE explicit confirmation, stating plainly: installing sends everything listed as due right away, then a background job checks daily at 10:00 and at login and sends each new receipt once it's issued, with no further confirmation. Duplicate protection (send log + Gmail Sent folder) still applies, and problems are reported by notification and by an email to themselves.
   3. On confirmation:
      ```bash
      python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" schedule install
      python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" schedule status
      ```

9. **Finish by showing how to use it.** Run `status` once more and show the table, then summarize:
   - what's configured, what was sent, and (from `schedule status`) what the scheduled job is waiting for next;
   - the commands: `/emburse-receipts:status` (what's sent and what's missing), `/emburse-receipts:send pending` (submit everything missing), `/emburse-receipts:send tmobile 2026-07..2026-09` (specific months), `/emburse-receipts:fetch` (download only) — or plain requests like "which receipts haven't I submitted?";
   - if the plugin was installed during this session, the slash commands appear after restarting Claude Code;
   - if a provider login expires, they'll get a notification and an email; logging in again in Chrome is all that's needed, and the next daily run picks it up. The log is `~/.claude-receipts/runs.log`.

Never echo, log, or store the app password yourself, and never print cookie values or tokens.
