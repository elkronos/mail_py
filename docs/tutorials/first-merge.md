# Walkthrough 1: your first merge

This walkthrough uses the files in
[`examples/01_quickstart`](https://github.com/elkronos/mail_py/tree/main/examples/01_quickstart).
Every step before "Sending for real" is safe to run, because nothing is sent.

## 1. Look at the inputs

`invite.txt`:

```text
--8<-- "examples/01_quickstart/invite.txt"
```

`people.csv`:

```text
--8<-- "examples/01_quickstart/people.csv"
```

The template uses four placeholders: `first_name`, `lab`, `date`, and the
subject's `first_name` again. Each one must be a column in the CSV.

## 2. Dry run

```bash
email-merge -t examples/01_quickstart/invite.txt -d examples/01_quickstart/people.csv \
    --from "Open House <openhouse@example.org>" --preview 2 --output-dir outbox
```

`--preview 2` prints the first two messages. `--output-dir` writes all three as
`.eml` files. Open one in Thunderbird, Apple Mail or Outlook to see exactly
what the recipient will see.

The Python equivalent is
[`examples/01_quickstart/run.py`](https://github.com/elkronos/mail_py/blob/main/examples/01_quickstart/run.py):

```python
--8<-- "examples/01_quickstart/run.py"
```

## 3. See validation catch mistakes

Introduce three typical errors into a copy of `people.csv`:

```text
email,firstname,lab,date
ann.lee@example.org,Ann,Neuro Lab,12 March
bo.chen@example,Bo,Neuro Lab,12 March
ann.lee@example.org,Ann,Neuro Lab,12 March
```

Running the same command now stops before anything is sent:

```text
error: 1 problem(s) found; nothing was sent:
  error: data: template uses {first_name} but there is no such column; did you mean 'firstname'?
```

Rename the column back to `first_name` and run it again:

```text
error: 2 problem(s) found; nothing was sent:
  error: row 2: invalid email address 'bo.chen@example'
  error: row 3: duplicate address 'ann.lee@example.org' (first seen in row 1)
```

Row numbers count data rows, so the header isn't row 1. Empty values (for
example, a blank `first_name`) are reported as **warnings**. They don't block
sending, because a blank is sometimes intended.

## 4. Sending for real

```bash
export EMAIL_MERGE_PASSWORD='abcd efgh ijkl mnop'    # a Gmail app password
email-merge -t invite.txt -d people.csv --from you@gmail.com \
    --service gmail --send --log openhouse.jsonl
```

The tool runs the dry-run validation again, asks you to type `yes`, then sends,
printing one line per recipient:

```text
    sent  ann.lee@example.org
    sent  bo.chen@example.org
  failed  carmen.diaz@example.org (550 b'5.1.1 mailbox unavailable')

Done: sent: 2, failed: 1.
```

A permanent failure for one recipient (such as an unknown mailbox) doesn't
stop the run. Temporary failures (4xx replies, dropped connections) are retried
with backoff. A failed login stops the run immediately, with advice.

## 5. If something interrupts the run

Press Ctrl-C, lose Wi-Fi, or hit your provider's daily limit, then simply
re-run with `--resume` and the **same log file**:

```bash
email-merge ... --send --log openhouse.jsonl --resume
```

Everyone with a `sent` entry in `openhouse.jsonl` is skipped. See
[Resume, limits and reminders](../guide/resume.md).

## 6. The log is your audit trail

Each line of `openhouse.jsonl` is one JSON object:

```json
{"timestamp": "2026-03-01T14:02:11+00:00", "to": "ann.lee@example.org", "status": "sent",
 "variant": null, "message_id": "<170928...@example.org>", "detail": "", "seed": null}
```

Load it in Python with `email_merge_tool.read_log("openhouse.jsonl")` or in
pandas with `pd.read_json("openhouse.jsonl", lines=True)`.
