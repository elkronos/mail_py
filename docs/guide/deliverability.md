# Deliverability and good practice

An email that lands in spam is worse than useless: it looks sent in your log,
and it damages your domain's reputation for next time. Most deliverability is
decided outside this tool, but the tool does its part and makes the rest easy.

## What the tool does for you

| Practice | Why | Reference |
| --- | --- | --- |
| `Date` and `Message-ID` on every message | Required or expected by RFC 5322, and missing ones are a spam signal | RFC 5322 §3.6 |
| Plain-text part with every HTML message (`multipart/alternative`) | Accessibility (screen readers, text clients), and HTML-only mail scores worse with filters | RFC 2046 §5.1.4 |
| `List-Unsubscribe` with per-recipient values | Lets mail clients show an "Unsubscribe" button | RFC 2369 |
| `List-Unsubscribe-Post: List-Unsubscribe=One-Click` | One-click unsubscribe, required by Gmail and Yahoo for bulk senders since 2024 | RFC 8058 |
| Rate limiting (default 20/min) and a daily `--limit` | Bursts and quota overruns trigger throttling and blocks | provider sending limits |
| One message per recipient, with no exposed recipient list | Privacy, and no "reply all" storms | — |
| Duplicate detection and exclusion lists | Repeated or unwanted mail drives spam complaints | — |

## What you must set up (outside this tool)

1. **SPF, DKIM and DMARC** for your sending domain (RFC 7208, RFC 6376,
   RFC 7489). Gmail's sender guidelines require all three for bulk senders
   (about 5,000+ messages a day to Gmail), and SPF or DKIM for everyone. If you
   send through Gmail, Microsoft 365 or a transactional service using *their*
   domain setup, these are usually handled for you. With your own domain, your
   provider's setup guide covers them.
2. **Send from an address you own**, matching the SMTP account. Many servers
   reject or rewrite a mismatched `From`.
3. **Consent and a working unsubscribe.** Only mail people who expect it, and
   honor opt-outs promptly (Gmail's guidelines say within two days). Keep a
   suppression list and pass it with `--exclude`. Laws such as CAN-SPAM (US),
   CASL (Canada) and the GDPR/ePrivacy rules (EU/UK) apply to many mailings,
   and research ethics boards often have their own rules on contact frequency.
4. **Keep spam complaints low.** Gmail asks bulk senders to stay below a 0.3%
   spam-report rate. Clear subjects, a recognizable sender and expected
   content matter more than any header.
5. **Test first.** Send to yourself and a colleague on a different provider
   and check the spam folder before the real run. A dry run with
   `--output-dir` shows the exact bytes that will be sent.

## Accessibility checklist for HTML mail

- Set `<html lang="...">`, and use real headings and text, not text inside images.
- Give every image an `alt`, and keep link text meaningful ("Read the report", not "click here").
- Keep good contrast, and remember many clients strip `<style>` blocks, so critical styling should be inline.
- Always provide the plain-text version (automatic if you omit it, better if you write it).
