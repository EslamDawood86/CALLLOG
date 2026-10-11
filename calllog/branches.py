"""The list of bank branches offered in the call form (a call is logged with the branch it came from).

The list lives in ``data/branches.json``. On first start it is seeded from ``default_branches.json``
(converted from the branch workbook); an administrator can replace it later by importing a new
Excel file (columns: region, branch number, branch name, branch type).
"""
import json
import re
from collections import Counter
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("default_branches.json")

# Column titles accepted when importing (Arabic as in the bank's workbook, or English).
_ALIASES = {
    "region": {"اسم المنطقة التابع لها", "المنطقة", "اسم المنطقة", "region", "area"},
    "code": {"رقم الفرع", "كود الفرع", "branch no", "branch no.", "branch number", "branch code", "code"},
    "name": {"اسم الفرع", "الفرع", "branch name", "branch", "name"},
    "type": {"نوع الفرع", "النوع", "branch type", "type"},
}
_TATWEEL = "ـ"


def _clean(value):
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text.replace(_TATWEEL, "")).strip()


def _code(value):
    text = _clean(value)
    if re.fullmatch(r"\d+\.0", text):      # numbers typed in Excel arrive as 95.0
        text = text[:-2]
    return text.zfill(3) if text.isdigit() else text


def parse_rows(rows):
    """Header row + data rows -> (entries, stats). Raises ValueError when the columns are missing."""
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        raise ValueError("The file is empty.")
    header = [_clean(h).lower() for h in rows[0]]
    col = {}
    for field, aliases in _ALIASES.items():
        wanted = {a.lower() for a in aliases}
        for i, title in enumerate(header):
            if title in wanted:
                col[field] = i
                break
    if "code" not in col or "name" not in col:
        raise ValueError("The first row must contain the column titles 'رقم الفرع' (branch number) "
                         "and 'اسم الفرع' (branch name); 'اسم المنطقة التابع لها' and 'نوع الفرع' are optional.")

    def cell(row, field):
        i = col.get(field)
        return row[i] if i is not None and i < len(row) else ""

    unique, skipped, merged = {}, 0, 0
    for row in rows[1:]:
        code, name = _code(cell(row, "code")), _clean(cell(row, "name"))
        region, kind = _clean(cell(row, "region")), _clean(cell(row, "type"))
        if not code or not name or len(code) > 12 or len(name) > 100:
            skipped += 1
            continue
        key = (region, code, name)
        if key in unique:                   # the same branch listed twice (e.g. with another "type")
            merged += 1
            continue
        unique[key] = {"code": code, "name": name, "region": region, "type": kind}
    entries = list(unique.values())
    _assign_ids(entries)
    return entries, {"rows": len(rows) - 1, "skipped": skipped, "duplicates_merged": merged}


def _assign_ids(entries):
    """Shortest unique id per entry: the number, or number/region, or number/region/name."""
    by_number = Counter(_id(e, 1) for e in entries)
    by_region = Counter(_id(e, 2) for e in entries)
    for e in entries:
        if by_number[_id(e, 1)] == 1:
            e["id"] = _id(e, 1)
        elif by_region[_id(e, 2)] == 1:
            e["id"] = _id(e, 2)
        else:
            e["id"] = _id(e, 3)


def _id(entry, parts):
    return "/".join([entry["code"], entry["region"], entry["name"]][:parts])


def load_default():
    with open(DEFAULT_PATH, encoding="utf-8") as f:
        return json.load(f)
