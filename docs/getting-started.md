# Getting started

## Install

Python 3.10 or newer is required. There are no other dependencies.

```bash
pip install git+https://github.com/elkronos/mail_py
# or, from a clone of the repository:
pip install -e .
```

Check the installation:

```bash
email-merge --version
python -m email_merge_tool --help     # same thing
```

## Five-minute dry run

1. Create a **template**, `invite.txt`. The optional header block at the top
   ends with a blank line:

    ```text
    Subject: See you on {date}, {first_name}!
    Reply-To: events@example.org

    Dear {first_name},

    You're invited to our open house on {date}.
    ```

2. Create the **data**, `people.csv`. Each `{placeholder}` needs a column, and
   the address column is called `email` by default:

    ```text
    email,first_name,date
    ann@example.org,Ann,12 March
    bo@example.org,Bo,12 March
    ```

3. Run a **dry run**. This is the default, so nothing is sent:

    ```bash
    email-merge -t invite.txt -d people.csv --from "Events <events@example.org>"
    ```

    You'll see the first message exactly as it would be sent:

    ```text
    ------------------------------------------------------------------------
    From: Events <events@example.org>
    To: ann@example.org
    Reply-To: events@example.org
    Subject: See you on 12 March, Ann!

    Dear Ann,

    You're invited to our open house on 12 March.

    Dry run: 2 message(s) ready. Nothing was sent; add --send to deliver.
    ```

    Add `--output-dir outbox` to save every message as a `.eml` file you can
    open in your mail client.

4. **Send** once the preview looks right:

    ```bash
    export EMAIL_MERGE_PASSWORD='your-app-password'   # or you'll be prompted
    email-merge -t invite.txt -d people.csv --from you@gmail.com \
        --service gmail --send --log sent.jsonl
    ```

    You'll be asked to confirm with `yes`. Messages go out at up to 20 per
    minute by default (`--rate`).

!!! warning "Gmail and Microsoft accounts"
    Gmail only accepts an **app password** for SMTP, not your normal password.
    Microsoft is retiring password-based SMTP. See [Sending safely](guide/sending.md).

## The same thing in Python

```python
from email_merge_tool import load_template, mail_merge

report = mail_merge(load_template("invite.txt"), "people.csv",
                    sender="Events <events@example.org>")   # dry_run=True by default
print(report.summary())            # "dry_run: 2"
print(report.messages[0])          # the full MIME message
```

Next: the [first walkthrough](tutorials/first-merge.md) covers validation errors,
sending, and resuming.
