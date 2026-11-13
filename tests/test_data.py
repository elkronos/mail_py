import json
from email.message import EmailMessage
from email.utils import getaddresses

import pytest

from email_merge_tool import DataError, is_valid_address, load_recipients, validate_recipients
from email_merge_tool.data import load_address_list, normalize_address


def test_csv_with_excel_bom_and_padding(tmp_path):
    p = tmp_path / "people.CSV"  # upper-case extension must work
    p.write_bytes("﻿ email ,name\nann@example.org,Ann\n,\n".encode())
    assert load_recipients(p) == [{"email": "ann@example.org", "name": "Ann"}]


def test_csv_extra_field_is_an_error(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text("email,name\nann@example.org,Smith, Ann\n", encoding="utf-8")
    with pytest.raises(DataError, match="row 1 has more fields"):
        load_recipients(p)


def test_csv_duplicate_columns(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text("email,name,name\n", encoding="utf-8")
    with pytest.raises(DataError, match="duplicate column"):
        load_recipients(p)


def test_json_values_are_strings(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps([{"email": "a@example.org", "n": 3, "x": None}]), encoding="utf-8")
    assert load_recipients(p) == [{"email": "a@example.org", "n": "3", "x": ""}]
    p.write_text(json.dumps({"email": "a"}), encoding="utf-8")
    with pytest.raises(DataError, match="array"):
        load_recipients(p)


def test_other_sources():
    assert load_recipients([{"email": "a@example.org", "n": 1}]) == [{"email": "a@example.org", "n": "1"}]
    with pytest.raises(DataError):
        load_recipients("people.xlsx")
    with pytest.raises(DataError):
        load_recipients(42)


def test_dataframe_duck_typing():
    class FakeFrame:
        columns = ["email"]

        def to_dict(self, orient):
            assert orient == "records"
            return [{"email": "a@example.org", "age": float("nan")}]

    assert load_recipients(FakeFrame()) == [{"email": "a@example.org", "age": ""}]


@pytest.mark.parametrize(
    "addr,ok",
    [
        ("ann@example.org", True),
        ("Ann Smith <ann@example.org>", True),
        ("ann.o'neil+tag@sub.example.co.uk", True),
        ("ann@example", False),
        ("ann example.org", False),
        ("ann@@example.org", False),
        ("ann@example,org", False),
        ("ann@example.org, bo@example.org", False),  # two people in one cell
        ("ann@example.org; bo@example.org", False),
        ("", False),
        ("Dr. Ann Smith <ann@example.org>", True),  # unquoted period in the name
        ('"Lee, Ann" <ann@example.org>', True),
        ("ann@example.org (Ann)", True),
        ("Zoë Li <zoe@example.org>", True),
        ("ann@example.org extra", False),  # delivered to ann@example.org, so it must not validate
        ("ann:lee@example.org", False),  # group syntax: delivered to lee@example.org
        ("mailto:ann@example.org", False),
        ("Lee, Ann <ann@example.org>", False),  # unquoted comma: two entries
        ("ann@example.org;", False),
        ("josé@example.org", False),  # needs SMTPUTF8
        (". <ann@example.org>", False),  # crashes the stdlib parser on 3.10-3.12
        ("ann@example.org " + "(" * 600 + ")" * 600, False),  # nested comments recurse in the parser
    ],
)
def test_is_valid_address(addr, ok):
    assert is_valid_address(addr) is ok


def test_normalize():
    assert normalize_address(" Ann <Ann@Example.ORG> ") == "ann@example.org"


@pytest.mark.parametrize(
    "value",
    [
        "ann@example.org",
        "Ann <Ann@Example.ORG>",
        "Dr. Ann Smith <ann@example.org>",
        '"Lee, Ann" <ann@example.org>',
        "ann@example.org (Ann)",
        "<ann@example.org>",
        "ann.o'neil+tag@sub.example.co.uk",
    ],
)
def test_key_of_a_valid_address_is_the_delivered_mailbox(value):
    # smtplib derives the envelope recipient from the To header like this, so
    # exclusion, de-duplication and resume keys must match it exactly.
    message = EmailMessage()
    message["To"] = value
    delivered = [addr for _, addr in getaddresses([str(message["To"])])]
    assert is_valid_address(value)
    assert [normalize_address(value)] == [a.lower() for a in delivered]


def test_csv_unbalanced_quote_is_an_error(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text('email,name\nann@example.org,"Ann\nbo@example.org,Bo\ncy@example.org,Cy\n', encoding="utf-8")
    with pytest.raises(DataError, match="starting on line 2.*unbalanced double quote"):
        load_recipients(p)


def test_csv_quote_pair_that_swallows_rows_is_an_error(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text(
        'email,name\nann@example.org,"Ann\nbo@example.org,Bo\ncy@example.org,Cy"\ndi@example.org,Di\n', encoding="utf-8"
    )
    with pytest.raises(DataError, match="row 1 .*column 'name' spans several lines"):
        load_recipients(p)


def test_csv_multiline_cells_and_inner_quotes_still_load(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text('email,notes\nann@example.org,"line one\nline two"\nbo@example.org,O"Brien\n', encoding="utf-8")
    assert load_recipients(p) == [
        {"email": "ann@example.org", "notes": "line one\nline two"},
        {"email": "bo@example.org", "notes": 'O"Brien'},
    ]


def test_validation_reports_everything(people):
    rows = [*people, {"email": "ANN@example.org", "first_name": "", "dept": "x", "id": "5"}, {"email": "bad"}]
    issues = validate_recipients(rows, {"first_name"})
    messages = [str(i) for i in issues]
    assert any("duplicate" in m and "row 5" in m for m in messages)
    assert any("warning: row 5" in m and "empty value" in m for m in messages)
    assert any("invalid email address 'bad' (no @ sign)" in m for m in messages)


def test_validation_suggests_column_names(people):
    issues = validate_recipients(people, {"firstname"})
    assert "did you mean 'first_name'" in issues[0].message
    issues = validate_recipients(people, set(), to_field="Email")
    assert "did you mean 'email'" in issues[0].message
    assert validate_recipients([], set())[0].message == "no recipients to send to"


def test_address_list(tmp_path):
    t = tmp_path / "done.txt"
    t.write_text("# responded\nAnn@Example.org\n\n", encoding="utf-8")
    assert load_address_list(t) == {"ann@example.org"}
    c = tmp_path / "done.csv"
    c.write_text("email\nbo@example.org\n", encoding="utf-8")
    assert load_address_list(c) == {"bo@example.org"}
    assert load_address_list(["Cy@example.org"]) == {"cy@example.org"}


def test_address_list_rejects_entries_that_would_exclude_nobody(tmp_path):
    t = tmp_path / "optout.txt"
    t.write_text("# opt-outs\nann@example.org\nbo@example.org, responded\ncy@example.org\tsurvey\n", encoding="utf-8")
    with pytest.raises(DataError, match="2 address list entries") as info:
        load_address_list(t)
    assert "line 3: 'bo@example.org, responded'" in str(info.value)
    assert "line 4" in str(info.value)


def test_address_list_accepts_common_formats(tmp_path):
    t = tmp_path / "optout.txt"
    t.write_text(
        "﻿# opt-outs\n  Ann <Ann@Example.org>\ncy@example.org;\n​di@example.org  # asked in May\nEd@example.org,\n",
        encoding="utf-8",
    )
    assert load_address_list(t) == {"ann@example.org", "cy@example.org", "di@example.org", "ed@example.org"}


def test_address_list_sources(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("ann@example.org\n", encoding="utf-8")
    b = tmp_path / "b.csv"
    b.write_text("email\nbo@example.org\n", encoding="utf-8")
    assert load_address_list([str(a), b]) == {"ann@example.org", "bo@example.org"}  # several files
    assert load_address_list([{"email": "Cy@example.org", "x": 1}]) == {"cy@example.org"}  # records

    class FakeFrame:
        columns = ["email"]

        def to_dict(self, orient):
            return [{"email": "di@example.org"}]

    assert load_address_list(FakeFrame()) == {"di@example.org"}
    with pytest.raises(DataError, match="no 'email' key"):
        load_address_list([{"mail": "ann@example.org"}])
    with pytest.raises(DataError, match="address list not found"):
        load_address_list(["missing.txt"])
    c = tmp_path / "c.csv"
    c.write_text("Email Address\nann@example.org\n", encoding="utf-8")
    with pytest.raises(DataError, match="no 'email' column"):
        load_address_list(c)
