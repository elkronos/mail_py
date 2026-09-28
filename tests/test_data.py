import json

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
    ],
)
def test_is_valid_address(addr, ok):
    assert is_valid_address(addr) is ok


def test_normalize():
    assert normalize_address(" Ann <Ann@Example.ORG> ") == "ann@example.org"


def test_validation_reports_everything(people):
    rows = [*people, {"email": "ANN@example.org", "first_name": "", "dept": "x", "id": "5"}, {"email": "bad"}]
    issues = validate_recipients(rows, {"first_name"})
    messages = [str(i) for i in issues]
    assert any("duplicate" in m and "row 5" in m for m in messages)
    assert any("warning: row 5" in m and "empty value" in m for m in messages)
    assert any("invalid email address 'bad'" in m for m in messages)


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
