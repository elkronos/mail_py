import copy
import dataclasses
import pickle
import re

import pytest

from email_merge_tool import Template, TemplateError, html_to_text, load_template
from email_merge_tool.templates import fill, placeholders


def test_fill_and_escapes():
    assert fill("Hi {name}!", {"name": "Ann"}) == "Hi Ann!"
    assert fill("literal {{name}}", {}) == "literal {name}"
    assert placeholders("{a} {{b}} {c} { d } {1x}") == {"a", "c"}


def test_css_braces_are_untouched():
    css = "<style>p { margin: 0 } a{color:red}</style><p>{name}</p>"
    assert fill(css, {"name": "Ann"}, escape_html=True) == "<style>p { margin: 0 } a{color:red}</style><p>Ann</p>"


def test_values_with_regex_metacharacters_and_numbers():
    # The old implementation used re.sub and broke on backslashes and non-strings.
    assert fill("{a} {b} {c}", {"a": r"C:\new\1", "b": 42, "c": None}) == r"C:\new\1 42 "


def test_missing_placeholder_raises():
    with pytest.raises(TemplateError, match="first"):
        fill("Dear {first}", {})


def test_html_values_are_escaped_but_text_is_not():
    t = Template(subject="Hi {n}", text="Hi {n}", html="<p>Hi {n}</p>")
    r = t.render({"email": "x@example.org", "n": "<b>&"})
    assert r.html == "<p>Hi &lt;b&gt;&amp;</p>"
    assert r.text == "Hi <b>&"
    assert r.subject == "Hi <b>&"


def test_header_injection_is_blocked():
    t = Template(subject="Hi {n}", text="x")
    with pytest.raises(TemplateError, match="line break"):
        t.render({"email": "x@example.org", "n": "a\nBcc: victim@example.org"})


def test_template_requires_body_and_subject():
    with pytest.raises(TemplateError):
        Template(subject="s")
    with pytest.raises(TemplateError):
        Template(subject="  ", text="x")
    with pytest.raises(TemplateError, match="unsupported header"):
        Template(subject="s", text="x", headers={"Foo": "1"})


def test_html_only_template_gets_text_alternative():
    t = Template(subject="s", html="<h1>Hi {n}</h1><p>Visit <a href='https://ex.org'>us</a></p><style>x{}</style>")
    r = t.render({"email": "a@example.org", "n": "Ann"})
    assert "Hi Ann" in r.text and "us (https://ex.org)" in r.text and "x{}" not in r.text


def test_load_template_with_header_block(tmp_path):
    p = tmp_path / "invite.txt"
    p.write_text("Subject: Hello {first_name}\nreply-to: team@example.org\n\nDear {first_name},\n", encoding="utf-8")
    t = load_template(p)
    assert t.name == "invite"
    assert t.subject == "Hello {first_name}"
    assert t.headers == {"Reply-To": "team@example.org"}
    assert t.text == "Dear {first_name},\n"
    assert t.fields == {"first_name"}


def test_body_starting_with_colon_is_not_a_header(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("Note: this is body text\n", encoding="utf-8")
    t = load_template(p, subject="S")
    assert t.text == "Note: this is body text\n"


def test_header_typo_is_reported(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("Subject: x\nReplyTo: a@b.org\n\nbody", encoding="utf-8")
    with pytest.raises(TemplateError, match="line 2"):
        load_template(p)


def test_missing_subject_and_file(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("body only", encoding="utf-8")
    with pytest.raises(TemplateError, match="no subject"):
        load_template(p)
    with pytest.raises(TemplateError, match="not found"):
        load_template(tmp_path / "nope.txt")


def test_text_plus_html_pair(tmp_path):
    (tmp_path / "t.txt").write_text("Subject: S\n\ntext {n}", encoding="utf-8")
    (tmp_path / "t.html").write_text("<p>html {n}</p>", encoding="utf-8")
    t = load_template(tmp_path / "t.txt", html_path=tmp_path / "t.html")
    assert t.text == "text {n}" and t.html == "<p>html {n}</p>"
    h = load_template(tmp_path / "t.html", subject="S")
    assert h.html and h.text is None


def test_html_to_text_lists():
    assert html_to_text("<ul><li>a</li><li>b</li></ul>").strip() == "- a\n\n- b"


def test_template_is_hashable_and_its_headers_are_read_only():
    t = Template(subject="Hi {n}", text="x", headers={"reply-to": "help@example.org"})
    same = Template(subject="Hi {n}", text="x", headers={"Reply-To": "help@example.org"})
    assert t == same and hash(t) == hash(same) and len({t, same}) == 1
    with pytest.raises(TypeError):
        t.headers["Bcc"] = "spy@example.org"  # type: ignore[index]
    assert dict(t.headers) == {"Reply-To": "help@example.org"}


def test_template_survives_pickle_copy_and_replace():
    t = Template(subject="Hi", text="x", headers={"X-Tag": "a"}, name="one")
    assert pickle.loads(pickle.dumps(t)) == t
    assert copy.deepcopy(t) == t
    renamed = dataclasses.replace(t, name="two")
    assert renamed.name == "two" and renamed.headers == {"X-Tag": "a"}


def test_positional_template_arguments_are_deprecated():
    with pytest.warns(DeprecationWarning, match="by position is deprecated"):
        t = Template("Hi", "body", None, {"Reply-To": "help@example.org"}, "legacy")
    assert (t.subject, t.text, t.name, dict(t.headers)) == ("Hi", "body", "legacy", {"Reply-To": "help@example.org"})
    with pytest.warns(DeprecationWarning), pytest.raises(TypeError, match="multiple values for argument 'text'"):
        Template("Hi", "body", text="other")
    with pytest.raises(TypeError, match="subject="):
        Template(text="body")


@pytest.mark.parametrize(
    ("value", "described"),
    [
        ("Ann\x00Bcc: evil@example.org", "the control character U+0000"),
        ("Ann\x1b[2J", "the control character U+001B"),
        ("Ann\x7f", "the control character U+007F"),
        ("Ann\x9b2J", "the control character U+009B"),
        ("Ann\x85Bcc: evil@example.org", "the control character U+0085"),
        ("Ann\x0bReply-To: evil@example.org", "the control character U+000B"),
        ("Ann\u2028Bcc: evil@example.org", "the line or paragraph separator U+2028"),
        ("Ann\u202egpj.exe", "the bidirectional control character U+202E"),
        ("Ann\u2066x\u2069", "the bidirectional control character U+2066"),
        ("=?utf-8?q?Ann=0D=0ABcc:_boss@example.org?=", "a line break (possible header injection) once its encoded"),
        ("=?utf-8?q?Ann=1B[2J?=", "the control character U+001B once its encoded words are decoded"),
    ],
)
def test_control_and_bidi_characters_are_refused_in_headers(value, described):
    t = Template(subject="Hi {n}", text="x", headers={"X-Name": "{n}"})
    with pytest.raises(TemplateError, match=f"Subject header would contain {re.escape(described)}"):
        t.render({"email": "a@example.org", "n": value})
    with pytest.raises(TemplateError, match=f"X-Name header would contain {re.escape(described)}"):
        Template(subject="Hi", text="x", headers={"X-Name": "{n}"}).render({"email": "a@example.org", "n": value})


def test_tab_and_marks_used_in_right_to_left_text_are_allowed_in_headers():
    t = Template(subject="Hi {n}", text="x")
    assert (
        t.render({"email": "a@example.org", "n": "Ann\tLee \u200f\u05e9\u05dc\u05d5\u05dd\u200e"}).subject
        == "Hi Ann\tLee \u200f\u05e9\u05dc\u05d5\u05dd\u200e"
    )


def test_template_with_a_control_character_in_a_header_is_refused_when_built():
    with pytest.raises(
        TemplateError, match=r"template 'memo': Subject header would contain the control character U\+000B"
    ):
        Template(subject="Hi\x0bReply-To: evil@example.org", text="x", name="memo")
    with pytest.raises(
        TemplateError, match=r"Reply-To header would contain the bidirectional control character U\+202E"
    ):
        Template(subject="Hi", text="x", headers={"Reply-To": "\u202eteam@example.org"})
