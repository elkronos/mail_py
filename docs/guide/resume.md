# Resume, limits and reminders

## The send log

With `log_path=` (`--log`), every recipient considered in a real send gets one
JSON line: `timestamp`, `to`, `status` (`sent`, `failed` or `skipped`),
`variant`, `message_id`, `detail` and `seed`. Lines are flushed and
`fsync`-ed as they are written, so the log survives a crash or power loss. Dry
runs don't write to the log.

**Use one log file per mailing.** The log records *who has received this
mailing*. Reusing it for a different mailing would skip everyone from the
first one.

## Resume

```bash
email-merge ... --send --log invite.jsonl --resume
```

Recipients with a `sent` line are skipped, and `failed` ones are tried again.
Matching is by (normalized) **address**, not by row number, so resume stays
correct even if you've sorted, filtered or appended to the CSV in the
meantime. Tools that resume "from row N" can silently skip or repeat people
when the file changes.

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

`--exclude` accepts a `.txt` file (one address per line, `#` comments
allowed), or a `.csv` or `.json` file with the address column. In Python,
`exclude=` also accepts any iterable of addresses. Use a *new* log for each
wave.

Also honor opt-outs: keep a suppression list of people who unsubscribed or
asked not to be contacted, and pass it with `--exclude` on every mailing. If
you need several lists, combine them into one file first.
