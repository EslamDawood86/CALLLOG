"""The IP phone lookup in a real browser (Chromium through Playwright, fixtures shared with tests/test_ops_views.py): the call form of an
employee (typing, the unit line, the automatic branch, saving, the table, the search, the edit dialog), the layout (nothing moves when
the line appears, long Arabic names wrap, light / dark contrast, a 360 px phone) and the administrator's IP phones card.

No console error, CSP violation or failed request is allowed in any of them. All data is made up (tests/conftest.py)."""
import os

import pytest

from conftest import IP_ROWS, ip_workbook
from test_ops_views import chromium, emp, make_account, server, tab, text_of  # noqa: F401  (fixtures: Chromium, the server, an employee, a signed-in tab)

ROYAL = "فرع مصر الجديدة"
LONG = "فرع النادي الاهلي – 6 اكتوبر مول سيتي ستارز المنطقة الصناعية الجديدة الشرقية للخدمات"
PAGE_ROWS = list(IP_ROWS) + [
    [88100, "Placeholder Name", LONG],
    [88103, "Placeholder Name", LONG], [88103, "Placeholder Name", LONG],            # a long unit and two other long ones
    [88103, "Placeholder Name", "إدارة الموارد البشرية والتدريب والتطوير المؤسسي"],
    [88103, "Placeholder Name", "فرع روكسي"],
]
EMP, PASSWORD = "2001", "MyOwnPass123"
FORM_READY = "#ipUnit[data-state=idle]"
ANSWERED = "#ipUnit[data-state=found], #ipUnit[data-state=none]"


@pytest.fixture
def directory(import_ip_phones):
    return import_ip_phones(PAGE_ROWS)


def open_form(tab, **kw):
    t = tab(EMP, PASSWORD, **kw)
    page = t.open("/dashboard", FORM_READY)
    return t, page


def type_ip(page, number, wait=True, delay=30):
    page.fill("#ipInput", "")
    page.type("#ipInput", number, delay=delay)
    if wait:
        page.wait_for_selector(ANSWERED)


def unit_text(page):
    return text_of(page, "#ipUnit .ip-unit-name") if page.locator("#ipUnit .ip-unit-name").count() else ""


def picker_text(page):
    return page.input_value("#branchHost .bp-input")


def auto_note_visible(page):
    return page.locator("#branchHost .bp-auto.on").count() == 1


def layout(page):
    return page.evaluate("""() => {
      const top = (sel) => document.querySelector(sel).getBoundingClientRect().top + window.scrollY;
      const box = document.querySelector('#ipUnit').getBoundingClientRect();
      return { label: top('#branchLabel'), reasons: top('#reasons'), unitTop: box.top + window.scrollY, unitH: box.height,
               pageW: document.documentElement.scrollWidth, innerW: window.innerWidth };
    }""")


def choose_reason(page, index=1):
    page.click(f"#reasons button >> nth={index}")
    page.wait_for_selector(".toast-msg")


def api_records(browser):
    return browser.get("/api/records").get_json()["records"]


# ====================================================================================== the employee's form
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_typing_shows_the_unit_and_fills_the_branch(emp, directory, tab, scheme):
    t, page = open_form(tab, scheme=scheme)
    assert text_of(page, "#ipUnit") == "Branch / department appears here"
    type_ip(page, "75942")
    assert unit_text(page) == ROYAL
    assert text_of(page, "#ipUnit .ip-unit-meta") == "from the IP phone directory"
    assert picker_text(page) == "100 · مصر الجديدة" and auto_note_visible(page)
    assert text_of(page, "#branchHost .bp-auto") == "Filled from the IP phone"
    assert page.evaluate("document.activeElement.id") == "ipInput"                 # the lookup never takes the focus
    assert page.get_attribute("#ipUnit", "role") == "status" and page.get_attribute("#ipUnit", "aria-live") == "polite"
    assert t.later_problems() == [], t.problems


def test_arabic_digits_are_looked_up_like_western_ones(emp, directory, tab):
    t, page = open_form(tab)
    type_ip(page, "٨٢٨٧٧")
    assert page.input_value("#ipInput") == "82877" and unit_text(page) == "جامعة الازهر"
    assert picker_text(page) == "143 · جامعة الازهر"                                 # a branch whose name has no "فرع"
    assert t.later_problems() == [], t.problems


def test_the_line_never_moves_the_form(emp, directory, tab):
    """Idle, found, long name, several units, department, not found: the fields below keep their place and the line its height."""
    for width in (1280, 360):
        t, page = open_form(tab, width=width)
        seen = [("idle", layout(page))]
        for number in ("75942", "73550", "88100", "88103", "77123", "99999", "82877"):
            type_ip(page, number)
            seen.append((number, layout(page)))
        base = seen[0][1]
        for name, now in seen:
            assert abs(now["reasons"] - base["reasons"]) <= 1, (width, name, now, base)
            assert abs(now["label"] - base["label"]) <= 1 and abs(now["unitH"] - base["unitH"]) <= 1, (width, name, now, base)
            assert now["pageW"] <= now["innerW"], (width, name, "the page scrolls sideways")
        assert t.later_problems() == [], t.problems


def test_long_arabic_names_wrap_inside_the_card_on_a_phone(emp, directory, tab):
    t, page = open_form(tab, width=360)
    type_ip(page, "88103")
    assert unit_text(page) == LONG
    box = page.evaluate("""() => {
      const name = document.querySelector('#ipUnit .ip-unit-name'), host = document.querySelector('#ipUnit'), card = document.querySelector('#entryCard');
      const line = parseFloat(getComputedStyle(name).lineHeight);
      return { rows: Math.round(name.getBoundingClientRect().height / line), right: host.getBoundingClientRect().right, cardRight: card.getBoundingClientRect().right,
               title: host.title, overflow: name.scrollHeight > name.clientHeight };
    }""")
    assert box["rows"] <= 2 and box["right"] <= box["cardRight"]
    assert box["title"].startswith(LONG) and "also used by" in box["title"] and "إدارة الموارد البشرية" in box["title"]   # nothing is lost: the whole text is in the tooltip
    assert text_of(page, "#ipUnit .ip-unit-meta").startswith("also used by:")
    assert t.later_problems() == [], t.problems


def test_several_units_show_the_first_and_who_else_uses_the_number(emp, directory, tab):
    t, page = open_form(tab)
    type_ip(page, "73550")
    assert unit_text(page) == "فرع روكسي"
    assert text_of(page, "#ipUnit .ip-unit-meta") == "also used by: إدارة الموارد البشرية"
    assert picker_text(page) == "126 · روكسي"                                         # the first unit decides the branch
    assert t.later_problems() == [], t.problems


def test_not_found_is_quiet_and_never_blocks_saving(emp, directory, tab):
    t, page = open_form(tab)
    type_ip(page, "99999")
    assert text_of(page, "#ipUnit") == "No branch / department found for this number"
    assert page.locator(".toast-msg").count() == 0 and page.is_hidden("#ipHelp") and picker_text(page) == ""
    choose_reason(page)
    row = page.locator("#recordsBody tr").first
    assert "99999" in row.inner_text() and row.locator(".ip-where").count() == 0
    record = api_records(emp)[0]
    assert record["ip_unit"] == "" and record["ip_unit_auto"] is False
    assert t.later_problems() == [], t.problems


def test_a_department_shows_only_the_unit_and_is_saved_with_it(emp, directory, tab):
    t, page = open_form(tab)
    type_ip(page, "77123")
    assert unit_text(page) == "إدارة الموارد البشرية" and picker_text(page) == "" and not auto_note_visible(page)
    choose_reason(page, 0)
    assert text_of(page, "#recordsBody tr:first-child .ip-where") == "إدارة الموارد البشرية"
    record = api_records(emp)[0]
    assert record["ip_unit"] == "إدارة الموارد البشرية" and record["branch"] == "" and record["ip_unit_auto"] is False
    assert t.later_problems() == [], t.problems


def test_save_with_an_automatic_branch_and_see_it_in_the_table(emp, directory, tab):
    t, page = open_form(tab)
    type_ip(page, "75942")
    choose_reason(page, 1)
    assert page.input_value("#ipInput") == "" and picker_text(page) == "" and not auto_note_visible(page)
    assert page.get_attribute("#ipUnit", "data-state") == "idle"                      # the form is ready for the next call
    assert page.evaluate("document.activeElement.id") == "ipInput"
    row = page.locator("#recordsBody tr").first
    assert text_of(page, "#recordsBody tr:first-child .ip-where") == ROYAL
    assert "100" in row.inner_text() and "مصر الجديدة" in row.inner_text()
    record = api_records(emp)[0]
    assert (record["ip_unit"], record["ip_unit_auto"], record["branch"]) == (ROYAL, True, "100")
    assert t.later_problems() == [], t.problems


def test_a_branch_the_employee_picked_is_never_overwritten(emp, directory, tab):
    t, page = open_form(tab)
    page.fill("#branchHost .bp-input", "")
    page.type("#branchHost .bp-input", "126", delay=20)
    page.keyboard.press("Enter")                                                          # chosen by hand: 126 · روكسي
    assert picker_text(page) == "126 · روكسي" and not auto_note_visible(page)
    type_ip(page, "75942")
    assert unit_text(page) == ROYAL and picker_text(page) == "126 · روكسي" and not auto_note_visible(page)
    choose_reason(page)
    record = api_records(emp)[0]
    assert record["branch"] == "126" and record["ip_unit"] == ROYAL and record["ip_unit_auto"] is False
    assert t.later_problems() == [], t.problems


def test_text_typed_in_the_branch_box_is_not_overwritten_either(emp, directory, tab):
    t, page = open_form(tab)
    page.type("#branchHost .bp-input", "12", delay=20)                                  # started typing, nothing chosen yet
    type_ip(page, "75942")
    assert picker_text(page) == "12" and not auto_note_visible(page)
    assert t.later_problems() == [], t.problems


def test_the_automatic_branch_follows_the_number_until_the_employee_touches_it(emp, directory, tab):
    t, page = open_form(tab)
    type_ip(page, "75942")
    assert picker_text(page) == "100 · مصر الجديدة"
    type_ip(page, "73550")
    assert picker_text(page) == "126 · روكسي" and auto_note_visible(page)               # still automatic: replaced
    type_ip(page, "99999")
    assert picker_text(page) == "" and not auto_note_visible(page)                         # no unit: the automatic branch is taken back
    type_ip(page, "75942")
    type_ip(page, "77123")
    assert picker_text(page) == "" and unit_text(page) == "إدارة الموارد البشرية"           # a department: no branch
    type_ip(page, "75942")
    assert picker_text(page) == "100 · مصر الجديدة"
    page.fill("#ipInput", "")
    page.wait_for_selector(FORM_READY)
    assert picker_text(page) == "" and not auto_note_visible(page)                         # the number was erased: so is its branch
    type_ip(page, "75942")
    page.fill("#branchHost .bp-input", "")
    page.type("#branchHost .bp-input", "126", delay=20)
    page.keyboard.press("Enter")                                                            # the employee's own choice takes over
    assert not auto_note_visible(page)
    type_ip(page, "75942")
    assert picker_text(page) == "126 · روكسي"
    page.click("#branchHost .bp-clear")
    type_ip(page, "73550")
    assert picker_text(page) == "126 · روكسي" and auto_note_visible(page)                 # cleared by hand = empty again = automatic again
    assert t.later_problems() == [], t.problems


def test_changing_the_automatic_branch_by_hand_makes_it_manual(emp, directory, tab):
    t, page = open_form(tab)
    type_ip(page, "75942")
    assert auto_note_visible(page)
    page.click("#branchHost .bp-input")
    page.keyboard.type("1")                                                                  # any edit makes it the employee's own
    assert not auto_note_visible(page)
    type_ip(page, "73550")
    assert picker_text(page).endswith("1") and not auto_note_visible(page)
    assert t.later_problems() == [], t.problems


def test_one_request_after_the_pause_and_answers_are_cached_for_the_page(emp, directory, tab):
    t, page = open_form(tab)
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/api/ip-phones/" in r.url else None)
    page.type("#ipInput", "75", delay=20)
    page.wait_for_timeout(500)
    assert asked == [], "fewer than 3 digits: nothing is asked"
    page.type("#ipInput", "942", delay=40)                                                   # three digits in a row: one request after the pause
    page.wait_for_selector(ANSWERED)
    assert len(asked) == 1 and asked[0].endswith("/api/ip-phones/75942")
    type_ip(page, "73550")
    assert len(asked) == 2
    type_ip(page, "75942", wait=False)                                                       # asked before: answered at once, no request
    page.wait_for_selector(ANSWERED)
    assert unit_text(page) == ROYAL and len(asked) == 2
    assert t.later_problems() == [], t.problems


def test_an_answer_that_arrives_late_is_dropped(emp, directory, tab):
    t, page = open_form(tab)
    held = []
    page.route("**/api/ip-phones/75942", lambda route: held.append(route))
    page.type("#ipInput", "75942", delay=20)
    for _ in range(40):                                                                     # wait until the request is on its way
        if held:
            break
        page.wait_for_timeout(50)
    assert held
    type_ip(page, "73550")                                                                  # the newer number is answered first
    assert unit_text(page) == "فرع روكسي"
    held[0].continue_()                                                                     # now the old answer arrives
    page.wait_for_timeout(500)
    assert unit_text(page) == "فرع روكسي" and picker_text(page) == "126 · روكسي"
    assert t.later_problems() == [], t.problems


def test_enter_moves_to_the_branch_box_and_the_lookup_does_not_steal_the_focus(emp, directory, tab):
    t, page = open_form(tab)
    page.fill("#ipInput", "")
    page.type("#ipInput", "75942", delay=10)
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.className") == "bp-input"
    page.wait_for_selector(ANSWERED)
    page.wait_for_selector("#branchHost .bp-auto.on")
    assert page.evaluate("document.activeElement.className") == "bp-input" and picker_text(page) == "100 · مصر الجديدة"
    assert t.later_problems() == [], t.problems


def test_saving_right_after_typing_waits_for_the_answer(emp, directory, tab):
    """A fast employee clicks the reason before the directory has answered: the call still gets its branch."""
    t, page = open_form(tab)
    held = []
    page.route("**/api/ip-phones/75942", lambda route: held.append(route))
    page.fill("#ipInput", "")
    page.type("#ipInput", "75942", delay=0)
    page.click("#reasons button >> nth=1")                                                  # immediately, no pause
    page.wait_for_timeout(600)
    assert len(held) == 1 and page.locator(".toast-msg").count() == 0                       # saving is waiting for the answer
    held[0].continue_()
    page.wait_for_selector(".toast-msg")
    record = api_records(emp)[0]
    assert (record["branch"], record["ip_unit"], record["ip_unit_auto"]) == ("100", ROYAL, True)
    assert t.later_problems() == [], t.problems


def test_a_failing_lookup_is_silent_and_saving_still_works(emp, directory, tab):
    t, page = open_form(tab)
    page.route("**/api/ip-phones/75942", lambda route: route.fulfill(status=500, content_type="application/json", body='{"error":"boom"}'))
    type_ip(page, "75942", wait=False)
    page.wait_for_timeout(700)
    assert page.get_attribute("#ipUnit", "data-state") == "idle" and page.locator(".toast-msg").count() == 0
    choose_reason(page)
    assert api_records(emp)[0]["ip_unit"] == ROYAL                                          # the server looked it up on its own
    assert t.later_problems("/api/ip-phones/75942", "Failed to load resource") == [], t.problems


def test_search_finds_calls_by_their_location(emp, directory, tab):
    t, page = open_form(tab)
    for number, reason in (("75942", 1), ("73550", 0), ("99999", 2)):
        type_ip(page, number)
        page.click(f"#reasons button >> nth={reason}")
        page.wait_for_selector(f"#recordsBody tr:has-text('{number}')")
        page.wait_for_selector(".toast-msg")
        page.evaluate("document.querySelectorAll('.toast-msg').forEach((n) => n.remove())")

    def rows(query):
        page.fill("#searchInput", query)
        return [r.split()[1] for r in page.locator("#recordsBody tr").all_inner_texts()]
    assert rows("") != [] and len(page.locator("#recordsBody tr").all()) == 3
    assert rows("مصر") == ["75942"] and rows("الجديده") == ["75942"] and rows("الجديدة") == ["75942"]     # taa marbuta / haa
    assert rows("روكسي") == ["73550"] and rows("99999") == ["99999"]
    page.fill("#searchInput", "لا يوجد")
    assert page.locator("#recordsBody tr").count() == 0 and text_of(page, "#emptyMsg") == "No calls match your search."
    assert t.later_problems() == [], t.problems


def test_the_edit_dialog_has_the_same_lookup(emp, directory, tab):
    t, page = open_form(tab)
    type_ip(page, "75942")
    choose_reason(page)
    page.click("#recordsBody tr:first-child button[title=Edit]")
    dialog = page.locator("dialog.dlg")
    dialog.wait_for()
    assert text_of(page, "#editIpUnit .ip-unit-name") == ROYAL and text_of(page, "#editIpUnit .ip-unit-meta") == "recorded with this call"
    assert dialog.locator(".bp-input").input_value() == "100 · مصر الجديدة" and dialog.locator(".bp-auto.on").count() == 1
    dialog.locator("input[aria-label='IP phone']").fill("")
    dialog.locator("input[aria-label='IP phone']").type("73550", delay=20)
    page.wait_for_selector("#editIpUnit[data-state=found] >> text=also used by")
    assert text_of(page, "#editIpUnit .ip-unit-name") == "فرع روكسي"
    assert dialog.locator(".bp-input").input_value() == "126 · روكسي" and dialog.locator(".bp-auto.on").count() == 1
    dialog.locator("button[type=submit]").click()
    page.wait_for_selector("dialog.dlg", state="detached")
    assert text_of(page, "#recordsBody tr:first-child .ip-where") == "فرع روكسي"
    record = api_records(emp)[0]
    assert (record["ip_phone"], record["ip_unit"], record["branch"], record["ip_unit_auto"]) == ("73550", "فرع روكسي", "126", True)
    assert t.later_problems() == [], t.problems


def test_the_edit_dialog_keeps_a_hand_picked_branch_and_a_plain_edit_keeps_the_unit(emp, directory, tab):
    t, page = open_form(tab)
    type_ip(page, "75942")
    page.fill("#branchHost .bp-input", "")
    page.type("#branchHost .bp-input", "126", delay=20)
    page.keyboard.press("Enter")
    choose_reason(page)
    page.click("#recordsBody tr:first-child button[title=Edit]")
    dialog = page.locator("dialog.dlg")
    dialog.wait_for()
    assert dialog.locator(".bp-auto.on").count() == 0
    dialog.locator("input[aria-label='IP phone']").fill("73550")
    page.wait_for_selector("#editIpUnit[data-state=found] >> text=also used by")
    assert dialog.locator(".bp-input").input_value() == "126 · روكسي"                  # the employee's own choice stays
    dialog.locator("textarea").fill("changed my mind")
    dialog.locator("button[type=submit]").click()
    page.wait_for_selector("dialog.dlg", state="detached")
    record = api_records(emp)[0]
    assert (record["ip_unit"], record["branch"], record["ip_unit_auto"], record["note"]) == ("فرع روكسي", "126", False, "changed my mind")
    page.wait_for_selector("#recordsBody tr:first-child >> text=changed my mind")        # the table has been redrawn
    page.click("#recordsBody tr:first-child button[title=Edit]")                            # a plain edit afterwards: the unit stays
    page.locator("dialog.dlg").wait_for()
    page.locator("dialog.dlg textarea").fill("again")
    page.locator("dialog.dlg button[type=submit]").click()
    page.wait_for_selector("dialog.dlg", state="detached")
    assert api_records(emp)[0]["ip_unit"] == "فرع روكسي"
    assert t.later_problems() == [], t.problems


def contrast(fg, bg):
    def lum(c):
        c = [v / 255 for v in c]
        c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    a, b = sorted((lum(fg), lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def over(rgba, under):
    r, g, b, a = rgba
    return tuple(round(a * x + (1 - a) * y) for x, y in zip((r, g, b), under))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_line_is_readable_in_light_and_dark(emp, directory, tab, scheme):
    t, page = open_form(tab, scheme=scheme)

    def colours():
        return page.evaluate("""() => {
          const css = (el, p) => getComputedStyle(el)[p];
          const parse = (s) => { const m = s.match(/[\\d.]+/g).map(Number); return [m[0], m[1], m[2], m.length > 3 ? m[3] : 1]; };
          const host = document.querySelector('#ipUnit'), card = document.querySelector('#entryCard');
          const pick = (sel) => { const el = document.querySelector(sel); return el ? parse(css(el, 'color')) : null; };
          return { card: parse(css(card, 'backgroundColor')), host: parse(css(host, 'backgroundColor')), text: pick('#ipUnit .ip-unit-name') || pick('#ipUnit .ip-unit-hint') || pick('#ipUnit .ip-unit-none'),
                   meta: pick('#ipUnit .ip-unit-meta'), hint: pick('#ipUnit .ip-unit-hint'), none: pick('#ipUnit .ip-unit-none'), auto: pick('#branchHost .bp-auto'), icon: parse(css(host.querySelector('svg'), 'color')) };
        }""")
    idle = colours()
    card = tuple(idle["card"][:3])
    assert contrast(tuple(idle["hint"][:3]), over(idle["host"], card)) >= 4.5
    type_ip(page, "75942")
    found = colours()
    surface = over(found["host"], card)
    assert surface != card, "the found line has its own background"
    assert contrast(tuple(found["text"][:3]), surface) >= 4.5 and contrast(tuple(found["meta"][:3]), surface) >= 4.5
    assert contrast(tuple(found["auto"][:3]), card) >= 4.5
    assert contrast(tuple(found["icon"][:3]), surface) >= 3
    type_ip(page, "99999")
    none = colours()
    assert contrast(tuple(none["none"][:3]), over(none["host"], card)) >= 4.5
    assert t.later_problems() == [], t.problems


def test_the_employee_page_works_on_a_phone(emp, directory, tab):
    t, page = open_form(tab, width=360)
    type_ip(page, "75942")
    choose_reason(page)
    assert layout(page)["pageW"] <= 360
    assert t.later_problems() == [], t.problems


# ======================================================================================== the administrator
def admin_branches(tab):
    t = tab()
    page = t.open("/admin#branches", "#ipPhonesCard")
    return t, page


def test_admin_card_import_through_the_page(tmp_path, admin, emp, tab, app):
    t, page = admin_branches(tab)
    assert "No IP phone directory has been imported yet" in text_of(page, "#ipPhonesCard")
    assert page.locator("#importIpPhones").inner_text().strip() == "Import IP phones"
    page.click("#importIpPhones")
    dialog = page.locator("dialog.dlg")
    dialog.wait_for()
    assert "never stored" in dialog.inner_text() and "manage.py import-ip-phones" in dialog.inner_text()

    wrong = tmp_path / "notes.txt"
    wrong.write_text("hello", encoding="utf-8")
    dialog.locator("input[type=file]").set_input_files(str(wrong))
    dialog.locator("button[type=submit]").click()
    page.wait_for_selector("dialog.dlg .form-error:not([hidden])")
    assert ".csv or .xlsx" in text_of(page, "dialog.dlg .form-error")
    assert not app.extensions["store"].ip_phones_path.exists()                                # nothing was replaced

    good = tmp_path / "ip_phones.xlsx"
    good.write_bytes(ip_workbook(PAGE_ROWS))
    dialog.locator("input[type=file]").set_input_files(str(good))
    dialog.locator("button[type=submit]").click()
    page.wait_for_selector("#ipImportResult")
    result = text_of(page, "#ipImportResult")
    assert "22 rows read" in result and "10 IP phone numbers kept" in result and "rows skipped" in result and "Alice" not in result
    page.click("dialog.dlg >> text=Close")
    page.wait_for_selector("dialog.dlg", state="detached")
    page.wait_for_selector("#ipPhonesCard .ip-stats")
    card = text_of(page, "#ipPhonesCard")
    assert "IP phone numbers" in card and "branches / departments" in card and "by admin from ip_phones.xlsx" in card
    stats = [text_of(page, f"#ipPhonesCard .tile:nth-child({i}) strong") for i in (1, 2, 3, 4)]
    status = admin.get("/api/admin/ip-phones").get_json()
    assert stats == [str(status["numbers"]), str(status["units"]), str(status["branch_units"]), str(status["ambiguous"])]
    assert emp.get("/api/ip-phones/75942").get_json()["unit"] == ROYAL                      # and the employee side sees it at once

    page.click("#importIpPhones")                                                           # a second import: the first one is kept as 'previous'
    dialog = page.locator("dialog.dlg")
    dialog.wait_for()
    assert f"({status['numbers']} numbers) will be replaced" in dialog.inner_text()
    dialog.locator("button:has-text('Cancel')").click()
    page.wait_for_selector("dialog.dlg", state="detached")
    assert t.later_problems("/api/admin/ip-phones/import", "Failed to load resource") == [], t.problems


def test_admin_card_accepts_a_dropped_file(tmp_path, admin, tab, app):
    t, page = admin_branches(tab)
    page.click("#importIpPhones")
    page.locator("dialog.dlg").wait_for()
    csv_text = "telephoneNumber,displayName,msExchExtensionAttribute27\n75942,Placeholder Name,فرع مصر الجديدة\n73550,Placeholder Name,إدارة المبيعات\n"
    dt = page.evaluate_handle("""(text) => { const dt = new DataTransfer(); dt.items.add(new File([text], 'dropped.csv', { type: 'text/csv' })); return dt; }""", csv_text)
    page.dispatch_event("dialog.dlg .dropzone", "dragover", {"dataTransfer": dt})
    assert "over" in page.get_attribute("dialog.dlg .dropzone", "class")
    page.dispatch_event("dialog.dlg .dropzone", "drop", {"dataTransfer": dt})
    assert "over" not in page.get_attribute("dialog.dlg .dropzone", "class")
    assert page.evaluate("document.querySelector('dialog.dlg input[type=file]').files[0].name") == "dropped.csv"
    page.locator("dialog.dlg button[type=submit]").click()
    page.wait_for_selector("#ipImportResult")
    assert "2 rows read" in text_of(page, "#ipImportResult")
    assert admin.get("/api/admin/ip-phones").get_json()["source_name"] == "dropped.csv"
    assert t.later_problems() == [], t.problems


def test_admin_card_shows_a_directory_that_cannot_be_read_and_the_page_still_works(admin, tab, app):
    app.extensions["store"].ip_phones_path.write_text("{ not json", encoding="utf-8")
    t, page = admin_branches(tab)
    assert "could not be read" in text_of(page, "#ipPhonesCard")
    assert page.locator("#importIpPhones").is_enabled()                                      # a new import can repair it
    assert t.later_problems("/api/admin/ip-phones", "Failed to load resource", "503") == [], t.problems


def test_admin_records_table_and_search_show_the_location(admin, emp, directory, tab):
    emp.post("/api/records", {"ip_phone": "75942", "reason": "solved", "branch": "100", "ip_unit_auto": True})
    emp.post("/api/records", {"ip_phone": "73550", "reason": "solved"})
    t = tab()
    page = t.open("/admin#records", "#tab-records table.data")
    assert text_of(page, "#tab-records tbody tr:first-child .ip-where") in (ROYAL, "فرع روكسي")
    texts = page.locator("#tab-records .ip-where").all_inner_texts()
    assert sorted(texts) == sorted([ROYAL, "فرع روكسي"])
    page.fill("#fQuery", "الجديده")
    page.wait_for_selector("#tab-records .ip-where >> text=مصر")
    page.wait_for_timeout(500)
    assert page.locator("#tab-records tbody tr").count() == 1
    assert page.get_attribute("#fQuery", "placeholder") == "Search IP, location, branch, note, name"
    assert t.later_problems() == [], t.problems


def test_admin_audit_text_for_the_import_and_the_unit(admin, emp, directory, tab):
    emp.post("/api/records", {"ip_phone": "75942", "reason": "solved", "branch": "100", "ip_unit_auto": True})
    t = tab()
    page = t.open("/admin#audit", "#tab-audit table.data")
    text = text_of(page, "#tab-audit")
    assert "Imported the IP phone directory" in text and "rows skipped" in text and "ip_phones.xlsx" in text
    assert "(فرع مصر الجديدة)" in text and "(from the IP phone)" in text
    assert t.later_problems() == [], t.problems


def test_the_look_of_the_call_form_in_screenshots(emp, directory, tab):
    folder = os.environ.get("CALLLOG_SCREENSHOTS")
    if not folder:
        pytest.skip("set CALLLOG_SCREENSHOTS=<folder> to write the screenshots")
    t, page = open_form(tab)
    type_ip(page, "75942")
    page.locator("#entryCard").screenshot(path=os.path.join(folder, "ip-phone-lookup.png"))
