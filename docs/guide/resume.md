# Resume, limits and reminders

## The send log

With `log_path=` (`--log`), every recipient considered in a real send gets one
JSON line: `timestamp`, `to`, `status` (`sent`, `failed`, `bounced`,
`uncertain` or `skipped`), `variant`, `message_id`, `detail`, `smtp_code`,
`enhanced_status` and `seed`. `smtp_code` and `enhanced_status` hold the
server's reply behind a failure or bounce, such as `550` and `"5.1.1"`, and
are `null` otherwise. Lines are flushed and `fsync`-ed as they are written, so
the log survives a crash or power loss. Dry runs don't write to the log.

**Use one log file per mailing.** The log records *who has received this
mailing*. Reusing it for a different mailing would skip everyone from the
first one.

A send that names a log which already records sent messages is refused
unless you add `--resume` (`resume=True`), so repeating a command with the
same log file cannot email everyone a second time. The preview prints the
log's full path and says whether it is a new file, which shows when a
relative path points somewhere unexpected.

`--resume` checks the log too. A log that doesn't exist is an error (a
mistyped name would otherwise skip nobody), and so is a log whose addresses
are all missing from the recipient data (probably another mailing's log).
While a run sends, it holds a lock on the log, so a second run with the same
log stops with an error instead of emailing the same people.

## Resume

```bash
email-merge ... --send --log invite.jsonl --resume
```

Recipients with a `sent` or `uncertain` line are skipped, and `failed` ones
are tried again. Addresses with a `bounced` line are skipped too: mailing a
mailbox that doesn't exist again harms the sender's reputation. Add
`--retry-bounced` (`retry_bounced=True`) to send to them anyway, for example
after the recipients' server was fixed.
Matching is by (normalized) **address**, not by row number, so resume stays
correct even if you've sorted, filtered or appended to the CSV in the
meantime. Tools that resume "from row N" can silently skip or repeat people
when the file changes.

A message is logged `uncertain` when the connection drops, the reply is
garbled, or you press Ctrl-C after its content was sent but before the server
confirmed it. The server may have delivered it,
so it is not retried and `--resume` skips it. Check those recipients yourself,
for example in your provider's Sent folder.

A message is logged `bounced` when the server refuses the recipient's
mailbox for good: enhanced status `5.1.x` (an unknown or invalid address,
except `5.1.7` and `5.1.8`, which are about the sender), `5.2.1` (a disabled
mailbox), or a plain `550` reply that says the user is unknown. Other
permanent refusals, such as a full mailbox (`5.2.2`) or a policy or spam
refusal (`5.7.x`), are `failed`, and resume tries them again. Bounces reported
later by email (after the server accepted the message) never reach the log.

The only remaining duplicate risk is a crash in the instant after the server
accepts a message and before its log line is written. That's at most one
message per crash.

## Limits (daily quotas)

```bash
email-merge ... --send --log big.jsonl --limit 450            # day 1
email-merge ... --send --log big.jsonl --limit 450 --resume   # each later day
```

This sends at most 450 messages a run, and the rest are logged as `skipped`
(`limit reached`). Messages logged `uncertain` count toward the limit.

If the provider's own daily limit runs out first, its refusal (such as
Gmail's `550 5.4.5 Daily user sending limit exceeded`) stops the run at once.
The message that met it is logged as `failed`, and the next day's `--resume`
sends it along with the rest.

## Reminder waves

Multiple contacts reliably increase survey response (Dillman et al., 2014).
To remind only non-responders:

```bash
email-merge -t reminder.txt -d panel.csv --exclude responded.txt --send --log reminder1.jsonl
```

`--exclude` accepts a `.txt` file with one address per line, or a `.csv` or
`.json` file with the address column. In a `.txt` file, blank lines, `#`
comment lines, trailing `# comments`, and trailing commas or semicolons left
by spreadsheet exports are ignored. Every other entry must be one valid
address: a line such as `ann@example.org, responded` stops the run with its
line number, because an entry that matches nobody would exclude nobody. If no
excluded address appears in the recipient data, you get a warning.

In Python, `exclude=` also accepts a DataFrame, or a list of addresses,
records (dicts) or file paths. Use a *new* log for each wave.

Also honor opt-outs: keep a suppression list of people who unsubscribed or
asked not to be contacted, and pass it with `--exclude` on every mailing. In
Python, pass several lists at once (`exclude=["optout.txt", "responded.csv"]`);
on the command line, combine them into one file first.
