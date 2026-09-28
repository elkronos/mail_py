# Command line

```text
email-merge -t TEMPLATE [-t TEMPLATE ...] -d DATA [options]
python -m email_merge_tool ...           # equivalent
```

Without `--send`, the command validates everything, prints the first message
(`--preview N` for more), and optionally saves all of them with
`--output-dir`. With `--send`, it runs the same validation, asks you to type
`yes` (unless `--yes`), and then sends.

## Options

| Option | Meaning |
| --- | --- |
| `-t, --template FILE` | Template (`.txt` or `.html`). Repeat it for experimental variants. |
| `--html FILE` | HTML version of the matching `-t`. Give it once per `-t`, in the same order. |
| `-d, --data FILE` | Recipients, `.csv` or `.json`. |
| `--to-field COLUMN` | Address column (default `email`). |
| `--from ADDRESS` | Sender, e.g. `"Lab <lab@example.org>"`. Not needed if the template has `From:`. |
| `--list-unsubscribe VALUE` | `List-Unsubscribe` header. Placeholders are allowed. |
| `--one-click` | Add the RFC 8058 `List-Unsubscribe-Post` header (needs an `https` URI). |
| `--attach FILE` | Attach a file to every message. Repeatable. |
| `--attachment-field COLUMN` | Column with per-recipient file path(s), `;`-separated. Placeholders are allowed. |
| `--allow-duplicates` | Permit an address on several rows. |
| `--seed SEED` | Randomly assign templates, reproducibly. |
| `--strata COLUMN` | Stratify the assignment on this column. Repeatable. |
| `--weights 2,1` | Allocation ratio between templates (default equal). |
| `--variant-field COLUMN` | Use a column naming each row's template, instead of `--seed`. |
| `--assignments FILE` | Write the assignment table (CSV). |
| `--send` | Actually send. |
| `-y, --yes` | Skip the confirmation prompt. |
| `--service NAME` | `gmail`, `outlook` or `office365`. |
| `--host`, `--port`, `--security` | A custom SMTP server. `--security` is `ssl`, `starttls` (default) or `none`. |
| `--username` | SMTP login (default: the `--from` address). |
| `--no-auth` | Don't log in (for example, an internal relay). |
| `--log FILE` | JSON Lines send log. |
| `--resume` | Skip addresses already logged as `sent`. |
| `--exclude FILE` | Addresses to skip (`.txt`, one per line, or `.csv`/`.json`). |
| `--limit N` | Send at most N messages this run. |
| `--rate PER_MIN` | Maximum send rate (default 20 per minute; `0` for no limit). |
| `--output-dir DIR` | Dry run: save every message as a `.eml` file. |
| `--preview N` | Dry run: print the first N messages (default 1). |

The SMTP password comes from the `EMAIL_MERGE_PASSWORD` environment variable,
or an interactive prompt.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success (a dry run, or every message sent) |
| 1 | Some messages failed, or you declined the confirmation |
| 2 | Validation, template or connection error (nothing was sent by this invocation, or it stopped early; see the log) |
| 130 | Interrupted with Ctrl-C (re-run with `--resume`) |

## Recipes

```bash
# Preview five messages and save all of them for inspection
email-merge -t invite.txt -d people.csv --from me@example.org --preview 5 --output-dir outbox

# Send through Gmail, logging, 450 per day
email-merge -t invite.txt -d people.csv --from me@gmail.com --service gmail \
    --send --log invite.jsonl --resume --limit 450

# Institutional relay on port 25 without authentication
email-merge -t invite.txt -d people.csv --from me@uni.edu --host smtp.uni.edu --port 25 --no-auth --send

# Two-arm experiment stratified by department, 2:1 allocation
email-merge -t long.txt -t short.txt -d panel.csv --from me@uni.edu \
    --seed 7 --strata department --weights 2,1 --assignments assign.csv

# Reminder to non-responders, with a certificate attached per person
email-merge -t reminder.txt -d panel.csv --from me@uni.edu --exclude responded.txt \
    --attachment-field certificate_path
```
