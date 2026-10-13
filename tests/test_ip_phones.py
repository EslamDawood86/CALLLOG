"""The IP phone directory (Feature A of docs/FEATURES-V2.md): reading the Active Directory export, what is stored (never a person's
name), the look-up, the two new fields on a Call Log record, the admin API, the exports and the command line.

Every name, number and unit below is made up. The real browser flows are in tests/test_ip_phones_page.py."""
import ast
import io
import json
import re
import statistics
import time
from pathlib import Path

import openpyxl
import pytest

from calllog import ip_phones
from calllog.ops.engines.common import norm_arabic
from calllog.storage import Store
from conftest import IP_EXPECTED, IP_HEADER, IP_NAME_SENTINELS, Browser, ip_workbook
from test_ops_views import emp, make_account, mgr  # noqa: F401  (fixtures: an employee, a manager)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "calllog" / "static"
ROYAL = "فرع مصر الجديدة"          # a unit that is a Call Log branch (code 100)
NEW_HELIOPOLIS = {"id": "100", "code": "100", "name": "مصر الجديدة", "region": "شمال القاهرة الكبرى"}


def build(rows=None, header=IP_HEADER, name="ip_phones.xlsx"):
    return ip_phones.build_from_bytes(ip_workbook(rows, header), name, "admin", "2026-10-11 09:00:00")


def csv_bytes(text, encoding="utf-8-sig"):
    return text.encode(encoding)


def all_files_text(root):
    """Everything under ``root`` as bytes (the data folder: files, audit logs, hidden temp files)."""
    return b"\n".join(p.read_bytes() for p in Path(root).rglob("*") if p.is_file())


# ============================================================================================ cleaning
@pytest.mark.parametrize("raw,expected", [
    (75942, "75942"), ("75942", "75942"), ("٧٥٩٤٢", "75942"), ("۷۵۹۴۲", "75942"), (" 75 942 ", "75942"), ("75-942", "75942"),
    ("75\u2013942", "75942"), (75942.0, "75942"), ("75942.0", "75942"), ("\u200f75942\u200e", "75942"), ("\u00a075942", "75942"),
    ("01001234567", "01001234567"), ("123", "123"), ("123456789012345", "123456789012345"),
])
def test_numbers_that_are_good(raw, expected):
    assert ip_phones.clean_number(raw) == (expected, None)


@pytest.mark.parametrize("raw,problem", [
    (None, "no_number"), ("", "no_number"), ("   ", "no_number"), ("-", "no_number"),
    ("12", "bad_number"), ("1234567890123456", "bad_number"), ("abc", "bad_number"), ("75942x", "bad_number"), ("+20100", "bad_number"),
    ("75942.5", "bad_number"), (75942.5, "bad_number"), (True, "bad_number"), ("7 5 9 4 2 x", "bad_number"), ("١٢٣abc", "bad_number"),
])
def test_numbers_that_are_not(raw, problem):
    assert ip_phones.clean_number(raw) == (None, problem)


@pytest.mark.parametrize("raw,expected", [
    ("  فرع   مصر الجديدة ", "فرع مصر الجديدة"), ("جامعة الازهر#", "جامعة الازهر"), ("الطيران # ", "الطيران"), ("فرع طيبه2000", "فرع طيبه"),
    ("فرع الرحاب 2", "فرع الرحاب 2"), ("فرع مول بوينت 90", "فرع مول بوينت 90"),         # a space before the digits: part of the name
    ("فرع الفيروز – الغردقة", "فرع الفيروز – الغردقة"), ("#", ""), ("-", ""), ("", ""), (None, ""), ("   ", ""),
    ("2000", "2000"),                                                                       # digits only: nothing to remove, kept as written
    ("a\u200fb\x00c", "abc"), ("x" * 300, "x" * ip_phones.MAX_UNIT_CHARS),
])
def test_unit_cleaning_keeps_the_administrators_text(raw, expected):
    assert ip_phones.clean_unit(raw) == expected


def test_match_keys():
    assert ip_phones.match_key("فرع جامعة الأزهر") == ip_phones.match_key("جامعة الازهر") == "جامعه الازهر"
    assert ip_phones.match_key("فروع الجيزة") == ip_phones.match_key("الجيزة")
    assert ip_phones.match_key("إدارة الموارد") == ip_phones.match_key("اداره الموارد") == "الموارد"
    assert ip_phones.match_key("مكتب الاعلام") == "الاعلام"
    assert ip_phones.match_key("فرع") == "فرع"                                          # nothing left otherwise: keep it
    assert ip_phones.match_key("الفيروز – الغردقة") == ip_phones.match_key("الفيروز - الغردقة")
    assert ip_phones.match_key("فرع الرحاب 2", loose=True) == "الرحاب" and ip_phones.match_key("فرع الرحاب 2") == "الرحاب 2"
    assert ip_phones.match_key("فرع 6", loose=True) == "6"                              # never reduced to nothing


def test_fold_extends_norm_arabic_without_changing_it():
    for text in ["مُحَمَّد  إأآ ة ى ـ", "فرع جامعة الأزهر", "١٢٣ abc", "  ", "ABC def"]:
        assert ip_phones.fold(text) == norm_arabic(text)
    assert ip_phones.fold("۱۲۳") == "123" and ip_phones.fold("أ # ب") == "ا ب"


# ============================================================================================ reading the file
def test_the_made_up_workbook_gives_the_expected_directory():
    data = build()
    assert data["version"] == 1 and data["source_name"] == "ip_phones.xlsx" and data["imported_by"] == "admin"
    report = data["report"]
    assert {k: report[k] for k in IP_EXPECTED} == IP_EXPECTED
    assert report["units"] == 8 and data["count"] == 8 == len(data["numbers"])
    assert "جامعة الازهر" in data["units"] and "فرع الدقي" in data["units"] and "فرع طيبة" in data["units"]   # '#' and glued '2000' gone
    assert not any("#" in u or u.endswith("2000") for u in data["units"])
    assert "فرع طيبه" not in data["units"]                                                                       # one unit, not two
    numbers = data["numbers"]
    assert set(numbers) == {"75942", "82877", "73550", "75943", "77123", "01001234567", "88001", "88002"}      # digits converted, spaces gone
    units = data["units"]
    assert units[numbers["75942"]] == ROYAL and units[numbers["82877"]] == "جامعة الازهر"
    assert [units[i] for i in numbers["73550"]] == ["فرع روكسي", "إدارة الموارد البشرية"]                     # most frequent first
    assert list(numbers) == sorted(numbers)                                                                      # a stable file


def test_source_names_are_just_a_file_name():
    assert ip_phones.safe_source_name("C:\\fakepath\\second.xlsx") == "second.xlsx" and ip_phones.safe_source_name("/tmp/x/ip.csv") == "ip.csv"
    assert ip_phones.safe_source_name("a\u200f  b\x00.xlsx") == "a b.xlsx" and ip_phones.safe_source_name(None) == ""
    assert len(ip_phones.safe_source_name("x" * 500)) == ip_phones.SOURCE_NAME_CHARS


def test_the_stored_dict_has_no_trace_of_the_names():
    blob = json.dumps(build(), ensure_ascii=False)
    for name in IP_NAME_SENTINELS + ("Person C", "Section header row"):
        assert name not in blob
    assert set(build()) == {"version", "imported_at", "imported_by", "source_name", "count", "units", "numbers", "report"}


@pytest.mark.parametrize("header", [
    ["telephoneNumber", "displayName", "msExchExtensionAttribute27"], ["TELEPHONENUMBER", "DISPLAYNAME", "MSEXCHEXTENSIONATTRIBUTE27"],
    ["Phone", "Name", "Branch"], ["ip phone", "name", "department"], ["Extension", "Who", "Unit"], ["رقم التليفون", "الاسم", "الفرع"],
    ["ip_phone", "x", "Unit"], None,                                                                             # no titles: first / last column
])
def test_header_detection_and_fallbacks(header):
    data = build([[75942, "Placeholder Name", ROYAL], [82877, "Another One", "إدارة المبيعات"]], header=header)
    assert data["count"] == 2 and data["report"]["rows"] == 2


def test_columns_in_another_order_are_found_by_their_titles():
    rows = [["فرع روكسي", "Placeholder Name", 73550]]
    data = build(rows, header=["msExchExtensionAttribute27", "displayName", "telephoneNumber"])
    assert data["count"] == 1 and data["units"] == ["فرع روكسي"] and "73550" in data["numbers"]


def test_headerless_file_counts_the_title_like_row_as_a_bad_number():
    data = build([["Ph", "Who", "Where"], [75942, "Placeholder Name", ROYAL]], header=None)   # titles it does not know
    assert data["report"]["rows"] == 2 and data["report"]["bad_number"] == 1 and data["count"] == 1


def test_a_second_sheet_or_extra_columns_do_not_confuse_it():
    rows = [[75942, "Placeholder Name", ROYAL, "extra", "more"]]
    data = build(rows, header=IP_HEADER + ["x", "y"])
    assert data["count"] == 1


def test_short_rows_and_blank_rows_are_survivable():
    data = build([[75942], [], [None, None], [82877, "Placeholder Name"], [73550, "Placeholder Name", "فرع روكسي"]])
    assert data["count"] == 1 and data["report"]["no_unit"] == 2 and data["report"]["rows"] == 3


@pytest.mark.parametrize("rows,message", [
    ([], "The file is empty"),
    ([[None, None, None]], "The file is empty"),
    ([["abc", "x", "فرع روكسي"], [None, "Header", None]], "No phone number"),
    ([[75942, "x", None], [82877, "y", ""]], "No phone number"),
])
def test_files_without_a_usable_row_are_refused_with_a_plain_message(rows, message):
    with pytest.raises(ip_phones.IpPhoneError, match=message):
        build(rows, header=None if message == "The file is empty" else IP_HEADER)
    if message == "No phone number":
        with pytest.raises(ip_phones.IpPhoneError, match=message):
            build([], header=IP_HEADER)                                      # titles only


def test_a_file_whose_unit_column_holds_names_of_people_is_refused():
    rows = [[10000 + i, "Placeholder", f"Placeholder Person {i}"] for i in range(400)]
    with pytest.raises(ip_phones.IpPhoneError, match="column looks wrong"):
        build(rows)
    with pytest.raises(ip_phones.IpPhoneError):                       # even when it is only the order of the columns
        build([[f"Placeholder Person {i}", 10000 + i, "فرع روكسي"] for i in range(5)] + [["x", "y", "z"]], header=None)


def test_unknown_columns_in_a_single_column_file():
    with pytest.raises(ip_phones.IpPhoneError, match="two columns|Could not tell"):
        build([[75942], [82877]], header=None)


def test_csv_files_semicolons_tabs_and_arabic_windows_text():
    text = "telephoneNumber;displayName;msExchExtensionAttribute27\n75942;Placeholder Name;فرع روكسي\n٨٢٨٧٧;Other Name;جامعة الازهر#\n"
    for encoding in ("utf-8-sig", "utf-8"):
        data = ip_phones.build_from_bytes(csv_bytes(text, encoding), "ip.csv")
        assert data["count"] == 2 and "فرع روكسي" in data["units"] and "جامعة الازهر" in data["units"], encoding
    windows = ip_phones.build_from_bytes(csv_bytes(text.replace("٨٢٨٧٧", "82877"), "cp1256"), "ip.csv")      # no Arabic-Indic digits in cp1256
    assert windows["count"] == 2 and "فرع روكسي" in windows["units"] and "جامعة الازهر" in windows["units"]
    tabs = ip_phones.build_from_bytes(csv_bytes(text.replace(";", "\t")), "ip.csv")
    assert tabs["count"] == 2
    commas = ip_phones.build_from_bytes(csv_bytes(text.replace(";", ",")), "ip.CSV")
    assert commas["count"] == 2


def test_other_file_types_and_broken_workbooks():
    with pytest.raises(ip_phones.IpPhoneError, match=".csv or .xlsx"):
        ip_phones.build_from_bytes(b"x", "ip.pdf")
    with pytest.raises(ip_phones.IpPhoneError, match="not a valid"):
        ip_phones.build_from_bytes(b"this is not a zip file", "ip.xlsx")
    with pytest.raises(ip_phones.IpPhoneError):
        ip_phones.build_from_bytes(b"", "ip.csv")


def test_too_many_rows_stops_the_import(monkeypatch):
    monkeypatch.setattr(ip_phones, "MAX_ROWS", 5)
    with pytest.raises(ip_phones.IpPhoneError, match="more than"):
        build([[10000 + i, "x", "فرع روكسي"] for i in range(6)])


def test_a_workbook_with_a_wrong_dimension_tag_is_still_read():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(IP_HEADER)
    ws.append([75942, "Placeholder Name", ROYAL])
    ws.append([82877, "Placeholder Name", "فرع روكسي"])
    ws.append([73550, "Placeholder Name", "إدارة الموارد البشرية"])
    buf = io.BytesIO()
    wb.save(buf)
    import zipfile
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            blob = src.read(item.filename)
            if item.filename.endswith("sheet1.xml"):
                blob = re.sub(rb'<dimension ref="[^"]*"/>', b'<dimension ref="A1"/>', blob)
            dst.writestr(item, blob)
    assert ip_phones.build_from_bytes(out.getvalue(), "ip.xlsx")["count"] == 3


# ============================================================================================ the directory object
def make_directory(rows=None, branches=None):
    from calllog import branches as branch_list
    return ip_phones.IpDirectory(build(rows), branches if branches is not None else branch_list.load_default())


def test_lookup_answers():
    d = make_directory()
    hit = d.lookup("75942")
    assert hit == {"number": "75942", "unit": ROYAL, "alternatives": [], "branch": NEW_HELIOPOLIS}
    ambiguous = d.lookup("73550")
    assert ambiguous["unit"] == "فرع روكسي" and ambiguous["alternatives"] == ["إدارة الموارد البشرية"] and ambiguous["branch"]["code"] == "126"
    assert d.lookup("82877")["unit"] == "جامعة الازهر" and d.lookup("82877")["branch"]["code"] == "143"      # a branch without the word فرع
    assert d.lookup("77123")["branch"] is None                    # a department
    assert d.lookup("75943")["branch"] is None                    # the name belongs to two branches: never guess
    assert d.lookup("99999") is None and d.lookup("") is None and d.lookup("abc") is None
    assert d.lookup("88001")["unit"] == "فرع الدقي"


def test_branch_matching_is_exact_first_and_never_guesses():
    branches = [{"id": "1", "code": "1", "name": "الرحاب", "region": "A"}, {"id": "2", "code": "2", "name": "الرحاب 2", "region": "A"},
                {"id": "3", "code": "3", "name": "سموحة", "region": "B"}, {"id": "4", "code": "4", "name": "سموحة", "region": "C"},
                {"id": "5", "code": "5", "name": "المعادي 3", "region": "A"}, {"id": "6", "code": "6", "name": "المعادي 4", "region": "A"}]
    d = ip_phones.IpDirectory({"units": [], "numbers": {}}, branches)
    code = lambda unit: (d.branch_for(unit) or {}).get("code")
    assert code("فرع الرحاب") == "1" and code("فرع الرحاب 2") == "2"                  # an exact name wins over the one without the digit
    assert code("فرع الرحاب 3") is None                                                # dropping the digit would give two candidates
    d.branches.append({"id": "7", "code": "7", "name": "الدقي", "region": "A"})
    d._idx = None
    assert code("فرع الدقي 2") == "7" and code("فرع الدقي2".replace("2", " 9")) == "7"  # one candidate once the trailing digit is dropped
    assert code("فرع سموحة") is None                                                   # two branches with that name
    assert code("فرع المعادي 5") is None                                               # dropping the digit would give two: no guess
    assert code("فرع المعادي") is None                                                 # nothing equal
    assert code("") is None


def test_damaged_directory_data_does_not_break_a_lookup():
    d = ip_phones.IpDirectory({"units": ["فرع روكسي", 5], "numbers": {"1": 0, "2": [0, 9], "3": 9, "4": "x", "5": [True], "6": None}}, [])
    assert d.lookup("1")["unit"] == "فرع روكسي" and d.lookup("2")["alternatives"] == []
    assert all(d.lookup(n) is None for n in "3456")        # out of range, not a list, a bool, null
    assert ip_phones.IpDirectory(None, None).lookup("1") is None and not ip_phones.IpDirectory([], []).status()["imported"]


def test_status_counts():
    d = make_directory()
    assert d.status() == {"imported": True, "numbers": 8, "units": 8, "ambiguous": 1, "branch_units": 5}


# ============================================================================================ storage
def test_replace_keeps_the_previous_file_and_a_second_replace_moves_it_on(app, tmp_path):
    store = app.extensions["store"]
    assert not store.ip_phones_path.exists() and store.ip_directory().lookup("75942") is None
    first = build([[75942, "x", ROYAL]])
    store.replace_ip_phones(first)
    assert store.ip_directory().lookup("75942")["unit"] == ROYAL and not store.ip_phones_previous_path.exists()
    store.replace_ip_phones(build([[82877, "x", "فرع روكسي"]]))
    assert store.ip_directory().lookup("75942") is None and store.ip_directory().lookup("82877")["unit"] == "فرع روكسي"
    previous = json.loads(store.ip_phones_previous_path.read_text(encoding="utf-8"))
    assert previous["numbers"].keys() == {"75942"}
    store.replace_ip_phones(build([[73550, "x", "فرع روكسي"]]))
    assert json.loads(store.ip_phones_previous_path.read_text(encoding="utf-8"))["numbers"].keys() == {"82877"}
    assert not [p for p in store.root.iterdir() if p.name.endswith(".tmp")]                 # nothing left behind


def test_the_file_is_compact_and_valid_utf8_json(app):
    store = app.extensions["store"]
    store.replace_ip_phones(build())
    text = store.ip_phones_path.read_text(encoding="utf-8")
    assert "\n" not in text.strip() and "فرع" in text and json.loads(text)["count"] == 8


def test_the_parsed_directory_is_cached_not_parsed_per_request(app, monkeypatch):
    store = app.extensions["store"]
    store.replace_ip_phones(build())
    calls = []
    real = json.load
    monkeypatch.setattr("calllog.storage.json.load", lambda f, *a, **k: calls.append(getattr(f, "name", "")) or real(f, *a, **k))
    store.TTL_LISTS = 0                                                    # re-check the file on every call; parse only when it changed
    for _ in range(50):
        assert store.ip_directory().lookup("75942")["unit"] == ROYAL
    assert len([c for c in calls if c.endswith("ip_phones.json")]) <= 1
    first = store.ip_directory()
    assert store.ip_directory() is first                                  # the wrapper (and its branch matches) is reused too


def test_a_second_process_import_is_picked_up_by_modification_time(app, tmp_path):
    store = app.extensions["store"]
    store.replace_ip_phones(build([[75942, "x", ROYAL]]))
    assert store.ip_directory().lookup("75942")["unit"] == ROYAL
    other = Store(store.root)                                              # e.g. `manage.py import-ip-phones` in another process
    other.replace_ip_phones(build([[75942, "x", "فرع روكسي"]]))
    store.TTL_LISTS = 0
    assert store.ip_directory().lookup("75942")["unit"] == "فرع روكسي"


def test_the_branch_list_changing_rebuilds_the_branch_matches(app, admin):
    store = app.extensions["store"]
    store.replace_ip_phones(build([[75942, "x", ROYAL]]))
    assert store.ip_directory().lookup("75942")["branch"]["code"] == "100"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["رقم الفرع", "اسم الفرع", "اسم المنطقة التابع لها"])
    ws.append([555, "مصر الجديدة", "منطقة جديدة"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    assert admin.post("/api/admin/branches/import", data={"file": (buf, "b.xlsx")}, content_type="multipart/form-data").status_code == 200
    hit = store.ip_directory().lookup("75942")
    assert hit["branch"]["code"] == "555" and hit["branch"]["region"] == "منطقة جديدة"


def test_a_damaged_file_means_no_directory_and_calls_can_still_be_saved(app, make_employee):
    store = app.extensions["store"]
    store.replace_ip_phones(build())
    store.ip_phones_path.write_text("{ this is not json", encoding="utf-8")
    store.TTL_LISTS = 0
    store._ip_broken_until = 0.0
    emp = make_employee("1001")
    assert emp.get("/api/ip-phones/75942").get_json() == {"found": False, "number": "75942"}
    assert emp.post("/api/records", {"ip_phone": "75942", "reason": "solved"}).status_code == 201
    with pytest.raises(Exception):
        store.ip_directory(strict=True)


# ============================================================================================ the look-up API
def lookup(browser, number):
    return browser.get(f"/api/ip-phones/{number}")


def test_lookup_role_matrix(app, admin, mgr, emp, import_ip_phones):
    import_ip_phones()
    anonymous = app.test_client()
    assert anonymous.get("/api/ip-phones/75942").status_code == 401
    for who in (admin, mgr, emp):
        r = lookup(who, "75942")
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
        assert r.get_json() == {"found": True, "number": "75942", "unit": ROYAL, "alternatives": [], "branch": NEW_HELIOPOLIS}


def test_lookup_answers_found_ambiguous_department_and_unknown(emp, import_ip_phones):
    import_ip_phones()
    ambiguous = lookup(emp, "73550").get_json()
    assert ambiguous["found"] and ambiguous["unit"] == "فرع روكسي" and ambiguous["alternatives"] == ["إدارة الموارد البشرية"]
    assert ambiguous["branch"]["code"] == "126"
    dept = lookup(emp, "77123").get_json()
    assert dept == {"found": True, "number": "77123", "unit": "إدارة الموارد البشرية", "alternatives": [], "branch": None}
    assert lookup(emp, "75943").get_json()["branch"] is None                                 # two branches called المطرية
    assert lookup(emp, "99999").get_json() == {"found": False, "number": "99999"}
    assert lookup(emp, "01001234567").get_json()["unit"] == "مكتب الاعلام"                   # a leading zero is kept


def test_lookup_reads_arabic_digits_and_refuses_junk(emp, import_ip_phones):
    import_ip_phones()
    assert lookup(emp, "٨٢٨٧٧").get_json()["unit"] == "جامعة الازهر"
    for junk in ("abc", "12", "7" * 16, "75942x", "..", "%20", "x" * 200):
        r = lookup(emp, junk)
        assert r.status_code == 400 and "digits only" in r.get_json()["error"], junk
    assert lookup(emp, "75942/extra").status_code == 404


def test_lookup_without_any_directory_is_a_calm_not_found(emp):
    assert lookup(emp, "75942").get_json() == {"found": False, "number": "75942"}


def test_lookup_never_returns_a_name(emp, admin, import_ip_phones):
    answer = import_ip_phones()
    texts = [json.dumps(answer, ensure_ascii=False), admin.get("/api/admin/ip-phones").get_data(as_text=True)]
    texts += [lookup(emp, n).get_data(as_text=True) for n in ("75942", "82877", "73550", "77123", "75943", "99999", "01001234567")]
    blob = "\n".join(texts)
    for name in IP_NAME_SENTINELS + ("Person C", "Person D", "Section header row"):
        assert name not in blob


# ============================================================================================ admin API
def test_admin_status_before_and_after_an_import(admin, import_ip_phones):
    status = admin.get("/api/admin/ip-phones").get_json()
    assert status["imported"] is False and status["numbers"] == 0 and status["has_previous"] is False
    answer = import_ip_phones(name="ip_phones.xlsx")
    assert {k: answer["report"][k] for k in IP_EXPECTED} == IP_EXPECTED
    assert answer["previous"] == 0 and answer["numbers"] == 8 and answer["units"] == 8 and answer["branch_units"] == 5
    assert answer["imported_by"] == "admin" and answer["imported_at"].startswith("2026-10-11 09:00") and answer["source_name"] == "ip_phones.xlsx"
    assert admin.get("/api/admin/ip-phones").get_json() == {k: v for k, v in answer.items() if k != "previous"}
    again = import_ip_phones([[75942, "x", "فرع روكسي"]], name="second.xlsx")
    assert again["previous"] == 8 and again["numbers"] == 1 and again["source_name"] == "second.xlsx" and again["has_previous"] is True


def test_import_role_matrix(app, admin, mgr, emp, import_ip_phones):
    def post(who, **kw):
        return who.post("/api/admin/ip-phones/import", data={"file": (io.BytesIO(ip_workbook()), "ip.xlsx")}, content_type="multipart/form-data", **kw)
    assert post(emp).status_code == 403 and post(mgr).status_code == 403
    assert emp.get("/api/admin/ip-phones").status_code == 403 and mgr.get("/api/admin/ip-phones").status_code == 403
    anonymous = app.test_client()
    assert anonymous.get("/api/admin/ip-phones").status_code == 401
    assert anonymous.post("/api/admin/ip-phones/import", data={"file": (io.BytesIO(ip_workbook()), "ip.xlsx")},
                          content_type="multipart/form-data").status_code in (400, 401)
    no_csrf = admin.client.post("/api/admin/ip-phones/import", data={"file": (io.BytesIO(ip_workbook()), "ip.xlsx")}, content_type="multipart/form-data")
    assert no_csrf.status_code == 400
    assert not app.extensions["store"].ip_phones_path.exists()                    # nothing of the refused ones got in
    import_ip_phones()
    assert app.extensions["store"].ip_phones_path.exists()


@pytest.mark.parametrize("data,message", [
    (None, "Choose a file"),
    ((b"just text", "notes.txt"), ".csv or .xlsx"),
    ((b"not a zip", "ip.xlsx"), "not a valid"),
    ((b"a,b\n1,2\n", "ip.csv"), "No phone number"),
    ((b"", "ip.csv"), "empty"),
])
def test_import_refuses_what_it_cannot_use_and_changes_nothing(admin, app, import_ip_phones, data, message):
    import_ip_phones()
    before = app.extensions["store"].ip_phones_path.read_bytes()
    body = {"file": (io.BytesIO(data[0]), data[1])} if data else {"other": "x"}
    r = admin.post("/api/admin/ip-phones/import", data=body, content_type="multipart/form-data")
    assert r.status_code == 400 and message in r.get_json()["error"]
    assert app.extensions["store"].ip_phones_path.read_bytes() == before


def test_a_big_file_is_accepted_on_this_route_only(app, admin):
    rows = "\n".join(f"{10000 + i},Placeholder Name {i},{('فرع روكسي', 'فرع مصر الجديدة', 'إدارة المبيعات')[i % 3]}" for i in range(90000))
    raw = ("telephoneNumber,displayName,msExchExtensionAttribute27\n" + rows).encode("utf-8")
    assert 2 * 1024 * 1024 < len(raw) < 20 * 1024 * 1024
    r = admin.post("/api/admin/ip-phones/import", data={"file": (io.BytesIO(raw), "big.csv")}, content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["numbers"] == 90000
    other = admin.post("/api/admin/branches/import", data={"file": (io.BytesIO(raw), "big.csv")}, content_type="multipart/form-data")
    assert other.status_code == 413 and "2 MB" in other.get_json()["error"]
    assert admin.post("/api/admin/users/import", data={"file": (io.BytesIO(raw), "big.csv")}, content_type="multipart/form-data").status_code == 413


def test_more_than_twenty_megabytes_is_refused(admin, app):
    raw = b"telephoneNumber,displayName,msExchExtensionAttribute27\n" + b"1" * (21 * 1024 * 1024)
    r = admin.post("/api/admin/ip-phones/import", data={"file": (io.BytesIO(raw), "huge.csv")}, content_type="multipart/form-data")
    assert r.status_code == 413 and "20 MB" in r.get_json()["error"]
    assert not app.extensions["store"].ip_phones_path.exists()


def test_import_is_audited_with_counts_and_no_names(app, admin, import_ip_phones):
    import_ip_phones()
    store = app.extensions["store"]
    entries = [e for e in store.read_audit("2026-10-11", "2026-10-11") if e["action"] == "ip_phones_imported"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["actor"] == "admin" and entry["target"] == "ip_phones.xlsx"
    assert entry["details"] == {**{k: v for k, v in IP_EXPECTED.items()}, "units": 8, "previous": 0}
    shown = admin.get("/api/admin/audit?action=ip_phones_imported").get_json()
    assert shown["total"] == 1
    assert "ip_phones_imported" in shown["actions"]


def test_the_importer_never_stores_or_logs_a_name_anywhere(app, admin, import_ip_phones, make_employee):
    """The strongest check: after an import, no byte of the data folder (directory file, previous file, audit log, temp files)
    holds a displayName - and the same after the look-ups and a call saved from a number of the directory."""
    import_ip_phones()
    import_ip_phones()                                                   # a second import makes the 'previous' file as well
    emp = make_employee("1001")
    emp.get("/api/ip-phones/75942")
    emp.post("/api/records", {"ip_phone": "75942", "reason": "solved", "branch": "100", "ip_unit_auto": True})
    admin.get("/export/records.csv?from=2026-10-11&to=2026-10-11")
    blob = all_files_text(app.extensions["store"].root).decode("utf-8", errors="replace")
    assert "ip_phones.previous.json" in {p.name for p in app.extensions["store"].root.iterdir()}
    for name in IP_NAME_SENTINELS + ("Person C", "Person I", "Section header row"):
        assert name not in blob, name
    assert "displayName" not in blob and "Placeholder" not in blob.replace("Placeholder Name", "")  # only the unit text and numbers are kept


# ============================================================================================ the new fields on a call
def log(browser, ip="75942", branch=None, **extra):
    body = {"ip_phone": ip, "reason": "solved", **extra}
    if branch is not None:
        body["branch"] = branch
    return browser.post("/api/records", body)


def test_a_call_gets_the_unit_the_server_looks_up(app, make_employee, import_ip_phones):
    import_ip_phones()
    emp = make_employee("1001")
    r = log(emp, "75942", "100", ip_unit_auto=True, ip_unit="Something the page invented")
    rec = r.get_json()["record"]
    assert r.status_code == 201 and rec["ip_unit"] == ROYAL and rec["ip_unit_auto"] is True and rec["branch"] == "100"
    stored = app.extensions["store"].get_record(rec["id"])
    assert stored["ip_unit"] == ROYAL and stored["ip_unit_auto"] is True
    assert "invented" not in json.dumps(stored, ensure_ascii=False)


def test_the_page_cannot_claim_an_automatic_branch_that_is_not_the_directorys(make_employee, import_ip_phones):
    import_ip_phones()
    emp = make_employee("1001")
    other_branch = log(emp, "75942", "126", ip_unit_auto=True).get_json()["record"]            # chose another branch by hand, flag left on
    assert other_branch["ip_unit"] == ROYAL and other_branch["ip_unit_auto"] is False and other_branch["branch"] == "126"
    no_flag = log(emp, "75942", "100").get_json()["record"]                                   # the right branch, but picked by hand
    assert no_flag["ip_unit_auto"] is False
    string_flag = log(emp, "75942", "100", ip_unit_auto="true").get_json()["record"]
    assert string_flag["ip_unit_auto"] is False
    no_branch = log(emp, "75942", ip_unit_auto=True).get_json()["record"]
    assert no_branch["ip_unit_auto"] is False and no_branch["branch"] == "" and no_branch["ip_unit"] == ROYAL
    unknown = log(emp, "99999", "100", ip_unit_auto=True).get_json()["record"]
    assert unknown["ip_unit"] == "" and unknown["ip_unit_auto"] is False
    dept = log(emp, "77123", ip_unit_auto=True).get_json()["record"]
    assert dept["ip_unit"] == "إدارة الموارد البشرية" and dept["branch"] == "" and dept["ip_unit_auto"] is False


def test_calls_work_without_a_directory_and_without_the_new_fields(app, make_employee):
    emp = make_employee("1001")
    rec = log(emp).get_json()["record"]
    assert rec["ip_unit"] == "" and rec["ip_unit_auto"] is False                                # defaults for the pages
    stored = app.extensions["store"].get_record(rec["id"])
    assert "ip_unit" not in stored and "ip_unit_auto" not in stored                              # nothing extra written
    audit = [e for e in app.extensions["store"].read_audit("2026-10-11", "2026-10-11") if e["action"] == "record_created"][0]
    assert set(audit["details"]) == {"ip_phone", "reason", "note", "branch"}


def test_the_unit_is_cut_at_120_characters(app, make_employee):
    app.extensions["store"].replace_ip_phones({"units": ["ي" * 300], "numbers": {"75942": 0}})
    emp = make_employee("1001")
    assert log(emp).get_json()["record"]["ip_unit"] == "ي" * 120


def test_editing_the_ip_phone_looks_the_unit_up_again(app, make_employee, import_ip_phones):
    import_ip_phones()
    emp = make_employee("1001")
    rid = log(emp, "75942", "100", ip_unit_auto=True).get_json()["record"]["id"]
    r = emp.put(f"/api/records/{rid}", {"ip_phone": "73550", "branch": "126", "ip_unit_auto": True})
    rec = r.get_json()["record"]
    assert rec["ip_phone"] == "73550" and rec["ip_unit"] == "فرع روكسي" and rec["branch"] == "126" and rec["ip_unit_auto"] is True
    gone = emp.put(f"/api/records/{rid}", {"ip_phone": "99999", "branch": ""}).get_json()["record"]
    assert gone["ip_unit"] == "" and gone["ip_unit_auto"] is False and gone["branch"] == ""
    back = emp.put(f"/api/records/{rid}", {"ip_phone": "77123"}).get_json()["record"]
    assert back["ip_unit"] == "إدارة الموارد البشرية" and back["ip_unit_auto"] is False


def test_editing_something_else_keeps_the_unit_the_call_was_made_with(app, admin, make_employee, import_ip_phones):
    import_ip_phones()
    emp = make_employee("1001")
    rid = log(emp, "75942", "100", ip_unit_auto=True).get_json()["record"]["id"]
    import_ip_phones([[75942, "x", "فرع روكسي"]])                                              # the directory changed since the call
    for body in ({"note": "later"}, {"ip_phone": "75942", "note": "later again", "ip_unit_auto": False}, {"branch": "100", "ip_unit_auto": False}):
        rec = emp.put(f"/api/records/{rid}", body).get_json()["record"]
        assert rec["ip_unit"] == ROYAL and rec["ip_unit_auto"] is True, body                    # the unit and the flag stay as they were
    changed = emp.put(f"/api/records/{rid}", {"branch": "126", "ip_unit_auto": True}).get_json()["record"]
    assert changed["ip_unit"] == ROYAL and changed["branch"] == "126" and changed["ip_unit_auto"] is False   # 126 is not the branch of ROYAL
    assert admin.put(f"/api/records/{rid}", {"note": "admin"}).get_json()["record"]["ip_unit"] == ROYAL


def test_the_audit_lines_carry_the_unit(app, admin, make_employee, import_ip_phones):
    import_ip_phones()
    emp = make_employee("1001")
    rid = log(emp, "75942", "100", ip_unit_auto=True).get_json()["record"]["id"]
    emp.put(f"/api/records/{rid}", {"ip_phone": "73550", "branch": "126", "ip_unit_auto": True})
    emp.delete(f"/api/records/{rid}")
    by_action = {e["action"]: e for e in app.extensions["store"].read_audit("2026-10-11", "2026-10-11")}
    assert by_action["record_created"]["details"]["ip_unit"] == ROYAL and by_action["record_created"]["details"]["branch_auto"] is True
    update = by_action["record_updated"]["details"]
    assert update["before"]["ip_unit"] == ROYAL and update["after"]["ip_unit"] == "فرع روكسي" and "ip_unit_auto" not in update["after"]
    assert by_action["record_deleted"]["details"]["ip_unit"] == "فرع روكسي"
    rows = admin.get("/api/admin/audit?q=ip_unit").get_json()
    assert rows["total"] >= 1


def test_the_employee_and_admin_listings_show_the_unit(admin, make_employee, import_ip_phones):
    import_ip_phones()
    emp = make_employee("1001")
    log(emp, "75942", "100", ip_unit_auto=True)
    log(emp, "99999")
    mine = {r["ip_phone"]: r for r in emp.get("/api/records").get_json()["records"]}
    assert mine["75942"]["ip_unit"] == ROYAL and mine["75942"]["ip_unit_auto"] is True
    assert mine["99999"]["ip_unit"] == "" and mine["99999"]["ip_unit_auto"] is False
    listed = {r["ip_phone"]: r for r in admin.get("/api/admin/records?from=2026-10-11&to=2026-10-11").get_json()["items"]}
    assert listed["75942"]["ip_unit"] == ROYAL


def test_search_finds_the_unit_with_other_spellings(admin, make_employee, import_ip_phones):
    import_ip_phones()
    emp = make_employee("1001")
    log(emp, "75942", "100")
    log(emp, "73550")
    log(emp, "99999")

    def found(q):
        return sorted(r["ip_phone"] for r in admin.get(f"/api/admin/records?from=2026-10-11&to=2026-10-11&q={q}").get_json()["items"])
    assert found("جامعة") == []
    assert found("مصر") == ["75942"] and found("الجديدة") == ["75942"] and found("الجديده") == ["75942"]       # taa marbuta spelling
    assert found("روكسي") == ["73550"] and found("ROKSY") == []
    assert found("75942") == ["75942"] and found("99999") == ["99999"]


def test_exports_have_the_location_column_at_the_end(admin, make_employee, import_ip_phones):
    import_ip_phones()
    emp = make_employee("1001")
    log(emp, "75942", "100")
    log(emp, "99999")
    for url, who in (("/export/records.xlsx", admin), ("/export/report.xlsx", admin), ("/export/my.xlsx", emp)):
        wb = openpyxl.load_workbook(io.BytesIO(who.get(f"{url}?from=2026-10-11&to=2026-10-11").data))
        rows = list(wb["Calls"].iter_rows(values_only=True)) if "Calls" in wb.sheetnames else list(wb.active.iter_rows(values_only=True))
        assert rows[0][-1] == "IP phone location" and rows[0][5:12] == ("IP Phone", "Branch No.", "Branch", "Region", "Action Taken", "Action (English)", "Status")
        by_ip = {r[5]: r for r in rows[1:]}
        assert by_ip["75942"][-1] == ROYAL and (by_ip["99999"][-1] or "") == ""
    csv_text = admin.get("/export/records.csv?from=2026-10-11&to=2026-10-11").data.decode("utf-8-sig")
    header, *lines = csv_text.splitlines()
    assert header.endswith(",IP phone location") and any(line.endswith(f",{ROYAL}") for line in lines)
    assert emp.get("/export/my.csv?from=2026-10-11&to=2026-10-11").data.decode("utf-8-sig").splitlines()[0].endswith("IP phone location")


def test_a_unit_that_looks_like_a_formula_is_not_one_in_the_exports(app, admin, make_employee):
    emp = make_employee("1001")
    store = app.extensions["store"]
    store.replace_ip_phones({"units": ['=HYPERLINK("http://example.test")'], "numbers": {"75942": 0}})
    log(emp, "75942")
    wb = openpyxl.load_workbook(io.BytesIO(admin.get("/export/records.xlsx?from=2026-10-11&to=2026-10-11").data))
    cells = [c for row in wb.active.iter_rows() for c in row]
    assert not [c for c in cells if c.data_type == "f"]
    assert "'=HYPERLINK" in admin.get("/export/records.csv?from=2026-10-11&to=2026-10-11").data.decode("utf-8-sig")


# ============================================================================================ command line
def test_manage_import_ip_phones(tmp_path, monkeypatch, capsys):
    import manage
    data_dir = tmp_path / "cli-data"
    monkeypatch.setenv("CALLLOG_DATA_DIR", str(data_dir))
    source = tmp_path / "export.xlsx"
    source.write_bytes(ip_workbook())
    manage.main(["import-ip-phones", str(source)])
    out = capsys.readouterr().out
    assert "rows read:" in out and "17" in out and "numbers kept:" in out and "not copied" in out
    for name in IP_NAME_SENTINELS:
        assert name not in out
    store = Store(data_dir)
    store.init()
    assert store.ip_directory().lookup("75942")["unit"] == ROYAL and store.ip_directory().status()["numbers"] == 8
    saved = json.loads(store.ip_phones_path.read_text(encoding="utf-8"))
    assert saved["imported_by"] == "manage.py" and saved["source_name"] == "export.xlsx"
    audit = [e for e in store.read_audit(saved["imported_at"][:10], saved["imported_at"][:10]) if e["action"] == "ip_phones_imported"]
    assert len(audit) == 1 and audit[0]["details"]["numbers"] == 8
    manage.main(["import-ip-phones", str(source)])                                                  # again: the old one is kept
    assert store.ip_phones_previous_path.is_file()
    assert "Zed Quartz" not in all_files_text(data_dir).decode("utf-8", errors="replace")


def test_manage_import_ip_phones_refuses_a_missing_or_wrong_file(tmp_path, monkeypatch):
    import manage
    monkeypatch.setenv("CALLLOG_DATA_DIR", str(tmp_path / "d"))
    with pytest.raises(SystemExit, match="not found"):
        manage.main(["import-ip-phones", str(tmp_path / "nope.xlsx")])
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="No phone number"):
        manage.main(["import-ip-phones", str(bad)])


# ============================================================================================ speed
@pytest.fixture
def big_directory(app):
    """A directory the size of the real one (25,000 numbers, 700 units) written without going through a file upload."""
    units = [f"فرع تجريبي {i}" for i in range(700)]
    numbers = {str(10000 + i): (i % 700 if i % 110 else [i % 700, (i + 1) % 700]) for i in range(25000)}
    app.extensions["store"].replace_ip_phones({"version": 1, "units": units, "numbers": numbers, "count": len(numbers)})
    return numbers


def test_lookup_requests_are_fast_even_with_a_full_size_directory(emp, big_directory):
    emp.get("/api/ip-phones/10000")                                       # the first one reads the file
    times = []
    for i in range(400):
        started = time.perf_counter()
        r = emp.get(f"/api/ip-phones/{10000 + (i * 37) % 25000}")
        times.append((time.perf_counter() - started) * 1000)
        assert r.status_code == 200 and r.get_json()["found"]
    times.sort()
    p95 = times[int(len(times) * 0.95)]
    assert p95 < 25, f"lookup p95 {p95:.1f} ms (median {statistics.median(times):.1f} ms)"      # measured ~2-3 ms; the bound is generous


def test_the_first_lookup_after_a_restart_parses_once(app, make_app, big_directory, monkeypatch):
    fresh = make_app()                                                      # a new process: nothing cached
    calls = []
    real = json.load
    monkeypatch.setattr("calllog.storage.json.load", lambda f, *a, **k: calls.append(getattr(f, "name", "")) or real(f, *a, **k))
    b = Browser(fresh)
    b.login("admin", app.config["INITIAL_ADMIN_PASSWORD"])
    started = time.perf_counter()
    for i in range(100):
        assert b.get(f"/api/ip-phones/{10000 + i}").status_code in (200, 403)                  # 403: still has to change the password
    assert len([c for c in calls if c.endswith("ip_phones.json")]) <= 1
    assert time.perf_counter() - started < 5


# ============================================================================================ source rules
SCRIPTS = ("common.js", "user.js", "admin.js")
FORBIDDEN = [r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write", r"\beval\(", r"new Function",
             r"setAttribute\(\s*[\"']style", r"\.cssText", r"javascript:", r"<script", r"setTimeout\(\s*[\"'`]"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_scripts_follow_the_csp_and_textcontent_rules(name):
    text = (STATIC / name).read_text(encoding="utf-8")
    assert text.startswith('"use strict";')
    for pattern in FORBIDDEN:
        assert not re.search(pattern, text), f"{name}: {pattern}"
    assert not re.findall(r"https?://", text.replace("http://www.w3.org/2000/svg", "")), "no external address"


def test_the_ip_lookup_uses_text_nodes_only():
    text = (STATIC / "common.js").read_text(encoding="utf-8")
    block = text[text.index("// ------------------------------------------------------ IP phone lookup"):text.index("// ---------------------------------------------------------- record editor")]
    assert "innerHTML" not in block and "insertAdjacent" not in block                # h() builds text nodes: nothing is parsed as markup
    assert ".style" not in block and "style" not in re.findall(r"setAttribute\(\s*[\"'](\w+)", block)
    assert ".focus(" not in block, "the lookup must never take the focus"


@pytest.mark.parametrize("page", ["user.html", "admin.html"])
def test_templates_have_no_inline_script_style_or_handler(page):
    html = (ROOT / "calllog" / "templates" / page).read_text(encoding="utf-8")
    assert not re.search(r"\sstyle\s*=", html) and not re.search(r"\son[a-z]+\s*=", html)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and "<style" not in html
    assert 'id="ipUnit"' in html or page == "admin.html"


def test_the_css_block_is_marked_and_self_contained():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}")
    block = css[css.index("/* IP phone lookup */"):]
    assert ".ip-unit" in block and "@import" not in css and "url(http" not in css
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block), "colours come from the theme variables so dark mode works"


@pytest.mark.parametrize("path", ["calllog/ip_phones.py", "calllog/api.py", "calllog/storage.py", "calllog/queries.py", "calllog/exports.py",
                                  "calllog/app.py", "manage.py", "tests/test_ip_phones.py", "tests/test_ip_phones_page.py", "tests/conftest.py"])
def test_python_3_9_compatible(path):
    source = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))                           # no match statement, no parenthesised with, ...
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "zip":
            assert not [k for k in node.keywords if k.arg == "strict"], "zip(strict=) needs 3.10"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            for side in (node.left, node.right):
                assert not (isinstance(side, ast.Constant) and side.value is None), "X | None needs 3.10 at runtime"
                assert not (isinstance(side, ast.Name) and side.id in {"int", "str", "float", "bool", "bytes", "list", "tuple", "set"}), "X | Y types need 3.10"


def test_no_script_files_ship_with_the_feature():
    bad = [p for p in ROOT.rglob("*") if p.suffix.lower() in {".bat", ".cmd", ".ps1", ".vbs", ".exe"} and ".git" not in p.parts and "dist" not in p.parts]
    assert bad == []
