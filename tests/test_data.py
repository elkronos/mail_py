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
    with pytest.raises(DataError, match=r"row 1 \(line 2\) has more fields"):
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
    with pytest.raises(DataError, match="row 1 .*column 'name' holds several lines"):
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


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_swallowed_rows_are_caught_with_any_line_ending(tmp_path, newline):
    text = 'email,name\nann@example.org,"Ann\nbo@example.org,Bo\ncy@example.org,Cy"\ndi@example.org,Di\n'
    p = tmp_path / "p.csv"
    p.write_bytes(text.replace("\n", newline).encode())
    with pytest.raises(DataError, match="holds several lines"):
        load_recipients(p)


def test_swallow_that_closes_on_the_next_line_is_caught(tmp_path):
    # The stray quote is in a column before the address, so only the first
    # line of the cell holds the swallowed address.
    p = tmp_path / "p.csv"
    p.write_text(
        'name,email\n"Ann Lee,ann@example.org\nBo Smith",bo@example.org\nCy,cy@example.org\n', encoding="utf-8"
    )
    with pytest.raises(DataError, match="row 1 .*column 'name' holds several lines"):
        load_recipients(p)


def test_stray_quote_in_the_header_is_an_error(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text(
        'email,first_name,"notes\nann@example.org,Ann,VIP\nbo@example.org,Bo,x"\ncy@example.org,Cy,\n', encoding="utf-8"
    )
    with pytest.raises(DataError, match="column name spans several lines"):
        load_recipients(p)


def test_prose_with_addresses_in_a_multiline_cell_still_loads(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text(
        'email,notes\nann@example.org,"Met at the conference.\nCc: bo@example.org, cy@example.org"\n'
        'di@example.org,"Di Lee\nAcme Corp\ndi@acme.example, +1 555 0100"\n',
        encoding="utf-8",
    )
    assert [r["email"] for r in load_recipients(p)] == ["ann@example.org", "di@example.org"]


def test_long_text_in_a_multiline_cell_loads_quickly(tmp_path):
    import time

    p = tmp_path / "p.csv"
    p.write_text('email,notes\nann@example.org,"see below\n' + "a" * 120_000 + '"\n', encoding="utf-8")
    started = time.perf_counter()
    load_recipients(p)
    assert time.perf_counter() - started < 1.0


def test_error_lines_skip_blank_lines(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text('email,name\n\n\nann@example.org,"Ann\nbo@example.org,Bo\n', encoding="utf-8")
    with pytest.raises(DataError, match="starting on line 4"):
        load_recipients(p)


@pytest.mark.parametrize(
    "value",
    ["'ann@example.org'", "'ann@example.org", "ann@example.org|2024-05-01", "ann@example.org#note", "ann@example.org​"],
)
def test_annotated_or_invisible_addresses_are_invalid(value):
    assert not is_valid_address(value)


def test_exclusion_entries_from_every_source_are_cleaned_the_same_way(tmp_path):
    c = tmp_path / "x.csv"
    c.write_text("email\nann@example.org​\n", encoding="utf-8")
    assert load_address_list(c) == {"ann@example.org"}
    assert load_address_list(["bo@example.org​", None, float("nan"), b"cy@example.org"]) == {
        "bo@example.org",
        "cy@example.org",
    }
    with pytest.raises(DataError, match=r"'ann@example.org\|2024' is not a single valid address"):
        load_address_list(["ann@example.org|2024"])


def test_file_named_like_an_address_is_read_as_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "optout@2024.csv").write_text("email\nann@example.org\n", encoding="utf-8")
    assert load_address_list(["optout@2024.csv"]) == {"ann@example.org"}


def test_json_exclusion_records_must_all_have_the_address_key(tmp_path):
    j = tmp_path / "x.json"
    j.write_text(json.dumps([{"email": "ann@example.org"}, {"Email": "bo@example.org"}]), encoding="utf-8")
    with pytest.raises(DataError, match="row 2: no 'email' key; did you mean 'Email'"):
        load_address_list(j)


def test_text_exclusion_file_encodings(tmp_path):
    u16 = tmp_path / "u16.txt"
    u16.write_bytes("ann@example.org\r\nbo@example.org\r\n".encode("utf-16"))
    assert load_address_list(u16) == {"ann@example.org", "bo@example.org"}
    cp = tmp_path / "cp.txt"
    cp.write_bytes("Zoë <zoe@example.org>\n".encode("cp1252"))
    with pytest.raises(DataError, match="is not valid UTF-8"):
        load_address_list(cp)


def test_space_after_a_closing_quote_gets_an_accurate_hint(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text('name,email\n"Ann" ,ann@example.org\n', encoding="utf-8")
    with pytest.raises(DataError, match="text follows a closing double quote"):
        load_recipients(p)


def test_deeply_nested_comment_is_invalid_not_a_crash():
    # Short enough for the length cap, but the email generator cannot write it.
    assert not is_valid_address("cy@example.org " + "(" * 250 + ")" * 250)
