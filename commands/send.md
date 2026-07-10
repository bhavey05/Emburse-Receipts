---
description: Fetch (if needed) and email a monthly receipt to your expense system
argument-hint: <provider> [YYYY-MM]
allowed-tools: Bash(python3:*), AskUserQuestion
---

Email the receipt for provider `$1` and month `$2` (defaults to the previous month if omitted) to the configured destination.

1. First run WITHOUT `--send` to fetch the PDF and show the send plan:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" fetch <provider> --month <YYYY-MM>
   ```

   If fetching needs extra input (ClassPass Stripe URL, Verizon PDF), follow the provider flow described by the script's error message — same as `/emburse-receipts:fetch`.

2. Show the user the send plan (recipient, subject, attachment) and ask them to confirm. Sending transmits a financial document externally, so never skip this confirmation. If this looks like their first send, also remind them the sending Gmail address must be registered on their expense account (e.g. as an alternative email in Emburse/Chrome River) — otherwise the receipt is silently dropped and never appears.

3. On confirmation, rerun the same command with `--send` appended and report the result.

Every send is recorded in `~/.claude-receipts/sent.json`. If the plan shows a WARNING that this provider/month was already sent (or `--send` reports SKIPPED), tell the user when and where it was sent and stop. Only add `--force` if the user explicitly confirms they want to send a duplicate.
