# Code review: v0.1 to v0.2

This page records an adversarial review of the original v0.1 code (commit
`2b45bbe`): its code, tests, packaging and documentation. It also records how
v0.2 addresses each finding. Every "confirmed" item was reproduced by running
the code, not just by reading it.

**Summary:** v0.1 could not be imported, its placeholder substitution never
worked, its tests could not run, and its README code blocks were broken. Beyond
those blocking bugs, the design had no preflight validation, no dry run, no way
to resume without re-sending, and no protection against HTML injection. v0.2
is a rewrite that keeps the original intent (a template, a CSV/JSON file, and
Gmail or Outlook) and fixes all of the above.

## Blocking defects (the package did not work)

| # | Finding | Where (v0.1) | Status |
| --- | --- | --- | --- |
| 1 | The package directory is `main/`, but the code, README, tests and entry point import `email_merge_tool`. **Confirmed:** `import main` raises `ModuleNotFoundError: No module named 'email_merge_tool'`. | `main/merge.py:7`, `setup.py:30`, `README.md:26` | Fixed: `src/email_merge_tool/` |
| 2 | `re` is used but never imported, so every call to `format_template_content` raises `ValueError: name 're' is not defined`. **Confirmed.** | `main/utils.py:48` | Fixed |
| 3 | Even with `re` imported, the pattern `f"{{{{{key}}}}}"` becomes the regex `{{name}}`, which only matches the literal text `{{name}}`. `{name}` was **never replaced**, so recipients would have received the raw template. **Confirmed.** Values containing `\1` or non-string values (JSON numbers) would also have broken `re.sub`. | `main/utils.py:48` | Fixed: a new renderer, with tests for backslashes, numbers and `None` |
| 4 | `credentials.get('email', credentials['username'])` evaluates `credentials['username']` first, so credentials with only `email` raise `KeyError`. **Confirmed.** | `main/merge.py:79` | Fixed: `SMTPConfig` |
| 5 | The console script points at `mail_merge`, which requires arguments, so `email-merge` would crash with `TypeError`. The README's "command line" feature did not exist. | `setup.py:30` | Fixed: a real CLI (`cli.py`) |
| 6 | Outlook was configured as implicit TLS on port 465. Outlook.com and Microsoft 365 use STARTTLS on port 587. | `main/merge.py:71-72` | Fixed: presets, with a test |
| 7 | The tests could not run: they imported a non-existent package and names not exported by `__init__`. One test expected a `ValueError` for missing fields that the code never raised. `test_mail_merge_success` nests two `patch("builtins.open")` calls, so the inner one also serves the template read, and it patches `email_merge_tool.send_email`, which doesn't affect the reference used inside `merge.py`. | `tests/test_merge.py` | Replaced: 77 tests across 5 files, and CI on Python 3.10–3.14 (Linux) and on Windows and macOS |

## Safety and correctness

| # | Finding | Where (v0.1) | Status |
| --- | --- | --- | --- |
| 8 | Recipients were validated **one at a time while sending**. A bad row halfway through left the mailing half-sent, and a row without `email` raised `KeyError` inside the `except` handler (which itself indexes `recipient['email']`), aborting the run. | `main/merge.py:106,126-139` | Fixed: every row, placeholder and attachment is validated before connecting |
| 9 | No dry run or preview. The first sign of a template mistake was recipients receiving it. | — | Fixed: dry run is the default, with `.eml` export |
| 10 | No resume. Any exception aborted the run (`raise RuntimeError`), and re-running re-sent to everyone already emailed. | `main/merge.py:140-142` | Fixed: `fsync`-ed JSONL log keyed by address, plus `--resume` |
| 11 | Merged values were inserted into HTML unescaped (HTML injection, broken markup for names like `A&B`). | `main/utils.py:48` | Fixed: auto-escaping in HTML bodies |
| 12 | Values placed in headers (To, Subject) were not checked for line breaks. Protection depended on the Python version's email library: 3.11 rejects the simplest form at serialization time, which turned it into a per-recipient failure mid-run. | `main/merge.py:94-95` | Fixed: explicit rejection during preflight |
| 13 | HTML emails had no plain-text alternative (a single-part `multipart/mixed`), which hurts accessibility and spam scoring. | `main/merge.py:92-101` | Fixed: `multipart/alternative`, with automatic text if none is supplied |
| 14 | No `Date` or `Message-ID` headers (RFC 5322). Some servers add them and some don't, and missing ones are a spam signal. | `main/merge.py:92-95` | Fixed |
| 15 | The subject could only come from a `subject` data column, and defaulted to "No Subject" (a spam trigger). Templates had no way to set one. | `main/merge.py:95` | Fixed: `Subject:` header in the template, required |
| 16 | "Email sent to ... with status" was printed even when sending failed. | `main/merge.py:139` | Fixed: explicit per-recipient status |
| 17 | `logging.basicConfig(filename='email_log.txt')` ran **at import time**. That hijacks the application's logging configuration and writes a file of recipient addresses (personal data) into whatever directory the program runs in. **Confirmed.** | `main/utils.py:4-9` | Fixed: `NullHandler` only, and an explicit, opt-in send log |
| 18 | The From address came from `connection.user`, an undocumented `smtplib` attribute, so there was no way to set a display name. | `main/merge.py:93,103` | Fixed: `sender=` / `--from`, or `From:` in the template |
| 19 | No rate limiting, no retry of temporary (4xx) failures, and no reconnection after a dropped connection. | — | Fixed |
| 20 | CSV was read as `utf-8`, not `utf-8-sig`, so Excel's "CSV UTF-8" files turn the first column into `﻿email`. The extension check was case-sensitive (`.CSV` rejected). JSON was not checked to be a list of objects. | `main/merge.py:40-52` | Fixed, with tests |
| 21 | The docstring promised case-insensitive placeholders, but field extraction and validation were case-sensitive. | `main/utils.py:38` | Resolved: case-sensitive by design, with "did you mean" suggestions |
| 22 | Broad `except Exception` blocks re-wrapped errors and lost their types (a missing template became `ValueError`, everything else `RuntimeError`). | `main/merge.py:14-22` | Fixed: specific exceptions |
| 23 | `send_error_email` was a placeholder that only logged. It was dead code. | `main/utils.py:62-70` | Removed |

## Documentation and packaging

| # | Finding | Status |
| --- | --- | --- |
| 24 | The README's install code block is never closed, so everything after it renders as code. The example used a package name and file paths that did not exist, and the template format was not documented. | Rewritten, with runnable examples |
| 25 | The README example hard-codes a password in source code, and there was no mention of Gmail app passwords or Microsoft's retirement of password SMTP. | Fixed: environment variable or prompt, and a provider guide |
| 26 | `setup.py` had placeholder author, email and URL, declared MIT without a `LICENSE` file, listed only end-of-life Pythons (3.6–3.9), and claimed "Beta" for code that could not import. | Replaced by `pyproject.toml`, `LICENSE`, Python 3.10+ |
| 27 | No CI, documentation site, examples or changelog. | Added: GitHub Actions (tests, lint, docs), a GitHub Pages site, three walkthroughs, and `CHANGELOG.md` |

## Usefulness beyond existing tools

v0.1 offered nothing beyond the standard library's `smtplib`. v0.2 focuses on
what existing Python email tools don't do out of the box, and what matters
when a mailing must be right the first time (see [How it compares](comparison.md)):

- preflight validation of the entire list, with actionable messages;
- crash-safe, address-keyed resume;
- reproducible, stratified randomized assignment for message experiments, with
  the methodology documented and cited ([Experiments](guide/experiments.md));
- reminder waves that exclude responders;
- deliverability practices built in (RFC 8058 unsubscribe, plain-text
  alternatives, rate limits), and documented ([Deliverability](guide/deliverability.md)).

## Open items

- **Legacy files:** `main/`, `setup.py` and `tests/test_merge.py` from v0.1
  are still in the repository, unused. The build uses `pyproject.toml`
  (hatchling ignores `setup.py`), and `tests/conftest.py` skips the old test
  file. Delete all three to finish the migration, along with the
  `collect_ignore` line in `tests/conftest.py`.
- **OAuth2** login (needed for some Microsoft 365 tenants) isn't implemented.
- **GitHub Pages:** in the repository settings, set Pages → Source to "GitHub
  Actions" so the docs workflow can publish the site.
