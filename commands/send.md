---
description: Fetch (if needed) and email receipts to your expense system — one or many providers/months
argument-hint: "<provider[,provider]|pending> [YYYY-MM | YYYY-MM..YYYY-MM]"
allowed-tools: Bash(python3:*), AskUserQuestion
---

Email receipts for provider(s) `$1` and month(s) `$2` to the configured destination.
`$1` may be a comma-separated list (`tmobile,classpass`); `$2` may be a single month,
a comma list, or a range (`2026-07..2026-09`) and defaults to the previous month.
If `$1` is `pending` (or the user asks for "everything not submitted yet"), run
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" status` first and use its
"Not sent yet" list as the providers/months.

1. Fetch everything and show the send plan (no email is sent without `--send`):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" fetch <providers> --month <months>
   ```

   The batch keeps going when one item fails and lists all problems at the end. Resolve
   them as described in `/emburse-receipts:fetch` (T-Mobile or ClassPass login, Verizon
   PDF), then rerun until the plan contains everything that's available.
   A month whose bill/charge doesn't exist yet (e.g. the current month) is fine to leave out — say so.

2. Show the user the send plan (recipient, subjects, amounts, attachments) and ask them to
   confirm once for the whole batch. Sending transmits financial documents externally,
   so never skip this confirmation. If this looks like their first send, also remind them the
   sending Gmail address must be registered on their expense account (e.g. as an alternative
   email in Emburse/Chrome River) — otherwise receipts are silently dropped.

3. On confirmation, rerun the same command with `--send` appended and report what was sent.

Every send is recorded in `~/.claude-receipts/sent.json`. If the plan shows a WARNING that a
provider/month was already sent (or `--send` reports SKIPPED), tell the user when it was sent
and leave it out. Only add `--force` if the user explicitly confirms a resend — e.g. the earlier
send had the wrong receipt. `--force` only works for a single provider + month (so a range can't
accidentally resend everything); run already-sent months separately from the rest of the batch. A `BLOCKED` item means that exact receipt was already sent under a
different month; never work around it — it's the wrong receipt for this month.
