"""Command-line interface: ``email-merge``."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Sequence
from email.utils import parseaddr

from . import __version__
from .data import DataError
from .merge import MergeReport, Result, mail_merge
from .templates import TemplateError, load_template
from .transport import PRESETS, FatalSendError, SMTPConfig, SMTPTransport

PASSWORD_ENV = "EMAIL_MERGE_PASSWORD"  # noqa: S105 (a variable name, not a secret)

EPILOG = f"""\
By default nothing is sent: messages are validated, the first one is printed,
and (with --output-dir) all are saved as .eml files. Add --send to deliver.

The SMTP password is read from ${PASSWORD_ENV}, or prompted for. It is never
accepted as a command-line argument, where it would be visible to other users
and saved in shell history.

examples:
  email-merge -t invite.txt -d people.csv --from "Lab <lab@example.org>"
  email-merge -t invite.txt -d people.csv --from lab@gmail.com --service gmail --send --log sent.jsonl
  email-merge -t control.txt -t personal.txt -d people.csv --seed 2024 --strata dept \\
      --assignments assignments.csv --from lab@example.org
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="email-merge",
        description="Personalized bulk email from a template and a CSV/JSON file, with validation, "
        "dry runs, resumable sending and randomized message experiments.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    g = p.add_argument_group("content")
    g.add_argument(
        "-t",
        "--template",
        action="append",
        required=True,
        metavar="FILE",
        help="template file (.txt or .html); repeat for experimental variants",
    )
    g.add_argument(
        "--html",
        action="append",
        metavar="FILE",
        help="HTML version of the matching --template (give once per --template)",
    )
    g.add_argument("-d", "--data", required=True, metavar="FILE", help="recipients (.csv or .json)")
    g.add_argument("--to-field", default="email", metavar="COLUMN", help="address column (default: email)")
    g.add_argument("--from", dest="sender", metavar="ADDRESS", help='sender, e.g. "Lab <lab@example.org>"')
    g.add_argument(
        "--list-unsubscribe", metavar="VALUE", help='List-Unsubscribe header, e.g. "<https://example.org/u?id={id}>"'
    )
    g.add_argument("--one-click", action="store_true", help="add RFC 8058 one-click unsubscribe header")
    g.add_argument("--attach", action="append", default=[], metavar="FILE", help="attach FILE to every message")
    g.add_argument("--attachment-field", metavar="COLUMN", help="column with per-recipient file path(s), ';'-separated")
    g.add_argument("--allow-duplicates", action="store_true", help="allow an address on several rows")

    g = p.add_argument_group("experiment")
    g.add_argument("--seed", help="random seed for assigning templates (required with several templates)")
    g.add_argument("--strata", action="append", metavar="COLUMN", help="stratify assignment on this column")
    g.add_argument("--weights", metavar="W1,W2,...", help="allocation ratio, e.g. 2,1")
    g.add_argument("--variant-field", metavar="COLUMN", help="column naming each row's template instead of --seed")
    g.add_argument("--assignments", metavar="FILE", help="write the assignment table to this CSV")

    g = p.add_argument_group("sending")
    g.add_argument("--send", action="store_true", help="actually send (otherwise dry run)")
    g.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation before sending")
    g.add_argument("--service", choices=sorted(PRESETS), help="provider preset")
    g.add_argument("--host", help="SMTP host (instead of --service)")
    g.add_argument("--port", type=int, help="SMTP port (default 587, or 465 with --security ssl)")
    g.add_argument("--security", choices=["ssl", "starttls", "none"], default="starttls")
    g.add_argument("--username", help="SMTP login (default: the --from address)")
    g.add_argument("--no-auth", action="store_true", help="do not log in (e.g. an internal relay on port 25)")
    g.add_argument("--log", metavar="FILE", help="JSON Lines send log (audit trail)")
    g.add_argument("--resume", action="store_true", help="skip recipients already sent to in --log")
    g.add_argument("--exclude", metavar="FILE", help="addresses to skip (.txt one per line, or .csv/.json)")
    g.add_argument("--limit", type=int, help="send at most N messages this run")
    g.add_argument(
        "--rate",
        type=float,
        default=20.0,
        metavar="PER_MIN",
        help="maximum messages per minute (default 20; 0 for no limit)",
    )

    g = p.add_argument_group("dry run")
    g.add_argument("--output-dir", metavar="DIR", help="save every message as a .eml file")
    g.add_argument("--preview", type=int, default=1, metavar="N", help="print the first N messages (default 1)")
    return p


def _transport(args: argparse.Namespace) -> SMTPTransport:
    username = None if args.no_auth else (args.username or parseaddr(args.sender or "")[1] or None)
    password = os.environ.get(PASSWORD_ENV)
    if password is None and username:
        password = getpass.getpass(f"SMTP password for {username}: ")
    if args.service:
        config = SMTPConfig.from_service(args.service, username, password)
    elif args.host:
        port = args.port or (465 if args.security == "ssl" else 587)
        config = SMTPConfig(args.host, port, args.security, username, password)
    else:  # pragma: no cover - rejected in main() before anything happens
        raise ValueError("--send needs --service or --host")
    return SMTPTransport(config)


def _print_progress(result: Result) -> None:
    extra = f" [{result.variant}]" if result.variant else ""
    detail = f" ({result.detail})" if result.detail else ""
    print(f"{result.status:>8}  {result.to}{extra}{detail}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.send and not (args.service or args.host):
        parser.error("--send needs --service or --host")
    try:
        if args.html and len(args.html) != len(args.template):
            raise TemplateError("give --html once for each --template (same order)")
        htmls = args.html or [None] * len(args.template)
        templates = [load_template(t, html_path=h) for t, h in zip(args.template, htmls, strict=True)]
        weights = [float(w) for w in args.weights.split(",")] if args.weights else None
        common = dict(
            sender=args.sender,
            to_field=args.to_field,
            seed=args.seed,
            strata=args.strata,
            weights=weights,
            variant_field=args.variant_field,
            assignments_path=args.assignments,
            exclude=args.exclude,
            log_path=args.log,
            resume=args.resume,
            limit=args.limit,
            list_unsubscribe=args.list_unsubscribe,
            one_click_unsubscribe=args.one_click,
            allow_duplicates=args.allow_duplicates,
            attachments=args.attach,
            attachment_field=args.attachment_field,
        )
        # Always validate with a dry run first; it is also what --send confirms.
        preview = mail_merge(templates, args.data, dry_run=True, output_dir=args.output_dir, **common)
        _show_preview(preview, args.preview)
        if not args.send:
            skipped = f", {preview.skipped} skipped" if preview.skipped else ""
            ready = preview.count("dry_run")
            print(f"\nDry run: {ready} message(s) ready{skipped}. Nothing was sent; add --send to deliver.")
            if args.output_dir:
                print(f"Messages saved as .eml files in {args.output_dir}")
            return 0
        to_send = preview.count("dry_run")
        if not args.yes and input(f"\nSend {to_send} message(s)? Type 'yes' to continue: ").strip() != "yes":
            print("Aborted; nothing was sent.")
            return 1
        interval = 60.0 / args.rate if args.rate and args.rate > 0 else 0.0
        report = mail_merge(
            templates,
            args.data,
            dry_run=False,
            transport=_transport(args),
            min_interval=interval,
            progress=_print_progress,
            **common,
        )
        print(f"\nDone: {report.summary()}.")
        return 1 if report.failed else 0
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (TemplateError, ValueError, FatalSendError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run with --resume (and the same --log) to continue.", file=sys.stderr)
        return 130


def _show_preview(report: MergeReport, count: int) -> None:
    for issue in report.issues:
        print(issue, file=sys.stderr)
    for message in report.messages[:count]:
        print("-" * 72)
        for name in ("From", "To", "Cc", "Reply-To", "Subject", "List-Unsubscribe"):
            if message[name]:
                print(f"{name}: {message[name]}")
        print()
        body = message.get_body(("plain",))
        print(body.get_content() if body is not None else "")
    if report.assignments:
        counts: dict[str, int] = {}
        for a in report.assignments:
            counts[a.variant] = counts.get(a.variant, 0) + 1
        print("Assignment: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
