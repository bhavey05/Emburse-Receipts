---
description: Check dependencies and walk through configuration (Gmail, destination email)
allowed-tools: Bash(python3:*), AskUserQuestion
---

Set up the receipts plugin. Work through this checklist, fixing issues as you find them:

1. Run the doctor:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" doctor
   ```

2. For each `[FAIL]` line, apply the suggested fix:
   - **playwright missing** → run `python3 -m pip install playwright` (fall back to `pip3 install --user playwright` if the environment is externally managed, or offer a venv).
   - **chromium missing** → run `python3 -m playwright install chromium`.
   - **destination email unset** → ask the user where receipts should be emailed. For Emburse/Chrome River this is their receipt inbox (e.g. `receipt@ca1.chromeriver.com` — the subdomain varies by company). Then run:
     `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" config --send-to <email>`
   - **gmail address unset** → ask for the Gmail address they send from, then run:
     `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" config --gmail-address <email>`

     IMPORTANT: warn the user that their expense system only accepts emailed
     receipts from addresses registered on their account. Emails from an
     unrecognized address are silently dropped — the receipt just never shows
     up. For Emburse/Chrome River: the Gmail address must match the email on
     their Chrome River profile, or be added as an alternative email
     (Preferences/Settings → Personal Settings → Alternative Emails). Ask them
     to confirm this before finishing setup.
   - **gmail app password unset** → the password must NOT pass through this chat.
     - If the doctor says it's exported in a shell file like `~/.zshrc` (common: that works in a
       terminal but not in the Claude desktop app, which doesn't load it), offer to copy it into the
       keychain with the exact command the doctor prints (`zsh -ic '... config --set-app-password --from-env'`).
       The value is read from the shell environment and never shown. Run it only after the user agrees.
     - Otherwise tell the user to:
       1. Create an app password at https://myaccount.google.com/apppasswords (requires 2-Step Verification).
       2. Run this in their own terminal (hidden prompt; stores the password in the macOS keychain):
          ```bash
          python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" config --set-app-password
          ```
       Wait for them to confirm before continuing.
   - **gmail login FAIL** → the address/app password pair is wrong or the app password was revoked;
     have them create a new app password and store it as above.

3. `[warn]` lines are provider-specific (macOS / Chrome profile are only needed for T-Mobile's automatic fetch). Mention them but don't block on them.

4. Re-run the doctor to confirm everything passes, then run `receipts.py status` so the send
   history is backfilled from Gmail's Sent folder, and tell the user they're ready: `/emburse-receipts:status`
   shows what's missing and `/emburse-receipts:send pending` submits it.

Never echo, log, or store the app password yourself, and never print cookie values.
