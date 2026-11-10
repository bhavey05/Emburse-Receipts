---
description: Show which months' receipts were already sent and which are still missing
argument-hint: "[provider,...] [--since YYYY-MM]"
allowed-tools: Bash(python3:*)
---

Show the user's receipt submission status:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/receipts.py" status $ARGUMENTS
```

This first backfills the send log from Gmail's Sent folder (so sends made before
the log existed, or from another machine, count), then prints a month × provider
table and a "Not sent yet" list.

Months listed under "Left alone" are older than the automatic-sending start month (or in the
skip list); they are deliberately not treated as missing, so only mention them, don't offer to
send them unless the user asks.

Summarize it for the user: which months are missing per provider, and offer to
submit them with `/emburse-receipts:send <providers> <months>` (e.g.
`/emburse-receipts:send tmobile,classpass 2026-07..2026-09`). Note that the current
month's bill/charge may simply not exist yet (T-Mobile bills close on the cycle end
date; ClassPass charges on the renewal date).

If it says it couldn't check Gmail, the table only reflects the local log — run
`/emburse-receipts:setup` to fix the Gmail credentials.
