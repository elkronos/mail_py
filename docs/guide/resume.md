# Resume, limits and reminders

## The send log

With `log_path=` (`--log`), every recipient considered in a real send gets one
JSON line: `timestamp`, `to`, `status` (`sent`, `failed`, `uncertain` or `skipped`),
`variant`, `message_id`, `detail` and `seed`. Lines are flushed and
`fsync`-ed as they are written, so the log survives a crash or power loss. Dry
runs don't write to the log.

**Use one log file per mailing.** The log records *who has received this
mailing*. Reusing it for a different mailing would skip everyone from the
first one.

A send that names a log which already records sent messages is refused
unless you add `--resume` (`resume=True`). Running the same command twice by
accident therefore cannot email everyone a second time. With `--resume`, a
log that doesn't exist yet or has no sent records produces a warning, which
catches a mistyped file name.

## Resume

```bash
email-merge ... --send --log invite.jsonl --resume
```

Recipients with a `sent` or `uncertain` line are skipped, and `failed` ones
are tried again.
Matching is by (normalized) **address**, not by row number, so resume stays
correct even if you've sorted, filtered or appended to the CSV in the
meantime. Tools that resume "from row N" can silently skip or repeat people
when the file changes.

A message is logged `uncertain` when the connection drops after its content
was sent but before the server confirmed it. The server may have delivered it,
so it is not retried and `--resume` skips it. Check those recipients yourself,
for example in your provider's Sent folder.

The only remaining duplicate risk is a crash in the instant after the server
accepts a message and before its log line is written. That's at most one
message per crash.

## Limits (daily quotas)

```bash
email-merge ... --send --log big.jsonl --resume --limit 450
```

This sends at most 450 messages this run, and the rest are logged as `skipped`
(`limit reached`). Run the same command tomorrow to send the next 450.

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
