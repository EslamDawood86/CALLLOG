#!/usr/bin/env python3
"""Build the distributable ZIP:  dist/NBE-CallLog.zip

    python docs/build/build_package.py

Layout of the ZIP (no wrapper folder at the root, forward slashes, deflate):

    READ-ME-FIRST.txt   copy of docs/build/READ-ME-FIRST.txt (bytes copied unchanged)
    program/            the application = the files tracked by git, minus tests/, docs/,
                        .gitignore, .gitattributes and pytest.ini
    guides/             every *.docx and *.pdf found in docs/out/ (may be empty)

The tracked-file LIST comes from `git ls-files`; the CONTENT is taken from the working tree
(not from git HEAD), so the ZIP contains exactly what you see in the folder right now.

The build FAILS (exit code 1) if the ZIP would contain a .bat/.cmd/.ps1/.vbs/.exe file, a
__pycache__ entry, or anything that must never be shipped (secrets, data, libs, logs, tests).

Standard library only.
"""
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]          # project root (the git repository)
BUILD_DIR = ROOT / "docs" / "build"
GUIDES_DIR = ROOT / "docs" / "out"
DIST_DIR = ROOT / "dist"
ZIP_PATH = DIST_DIR / "NBE-CallLog.zip"
README_FIRST = BUILD_DIR / "READ-ME-FIRST.txt"

# tracked files that are NOT part of the program
EXCLUDE_PREFIXES = ("tests/", "docs/", "tools/")
EXCLUDE_NAMES = (".gitignore", ".gitattributes", "pytest.ini")

# the product must never contain these (user rule: no .bat/.cmd/.ps1/.vbs - run.py only)
FORBIDDEN_SUFFIXES = (".bat", ".cmd", ".ps1", ".vbs", ".exe")
# things that must never be inside the ZIP (secrets, runtime data, build leftovers)
FORBIDDEN_PARTS = ("__pycache__", "data", "libs", "tests", "docs", ".git", ".venv", "venv",
                   ".pytest_cache", "dist")
FORBIDDEN_FILENAMES = (".secret_key", "config.json", "server.log", "server.log.1", ".gitignore",
                       ".gitattributes", "pytest.ini")
FORBIDDEN_FILE_SUFFIXES = (".pyc", ".pyo", ".tmp", ".log")

# the program must contain at least these
REQUIRED_PROGRAM_FILES = (
    "run.py", "bootstrap.py", "manage.py", "requirements.txt", "config.example.json", "README.md",
    "calllog/__init__.py", "calllog/app.py", "calllog/default_branches.json",
)
REQUIRED_PROGRAM_DIRS = ("calllog/static/", "calllog/templates/")

GUIDE_SUFFIXES = (".docx", ".pdf")


def fail(message):
    print(f"\nBUILD FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def tracked_files():
    """Paths (forward slashes, relative to the project root) of the files tracked by git."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        fail(f"cannot run 'git ls-files' in {ROOT}: {exc} {detail.decode('utf-8', 'replace')}")
    return sorted(p for p in out.decode("utf-8").split("\0") if p)


def is_program_file(path):
    return not (path.startswith(EXCLUDE_PREFIXES) or path in EXCLUDE_NAMES)


def zip_time(path):
    """ZIP timestamps start in 1980; use the file's own modification time."""
    t = time.localtime(max(path.stat().st_mtime, 315532800))
    return t[:6]


def add_file(zf, arcname, source, mode=0o644):
    info = zipfile.ZipInfo(arcname, date_time=zip_time(source))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100000 | mode) << 16
    zf.writestr(info, source.read_bytes())


def collect():
    """Return [(arcname, source_path)] for the whole ZIP, in the order they are written."""
    entries = []

    # 1. READ-ME-FIRST.txt
    if not README_FIRST.is_file():
        fail(f"missing {README_FIRST}")
    raw = README_FIRST.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        fail("READ-ME-FIRST.txt is not valid UTF-8")
    if b"\r\n" not in raw or b"\n" in raw.replace(b"\r\n", b""):
        fail("READ-ME-FIRST.txt must use CRLF line endings throughout")
    entries.append(("READ-ME-FIRST.txt", README_FIRST))

    # 2. program/
    program = []
    for rel in tracked_files():
        if not is_program_file(rel):
            continue
        src = ROOT / rel
        if not src.is_file():
            fail(f"tracked file is missing in the working tree: {rel} (restore it or run 'git rm')")
        program.append((f"program/{rel}", src))
    if not program:
        fail("git reports no tracked files")
    entries += program

    # 3. guides/
    guides = []
    if GUIDES_DIR.is_dir():
        for f in sorted(GUIDES_DIR.iterdir(), key=lambda p: p.name.lower()):
            if f.is_file() and f.suffix.lower() in GUIDE_SUFFIXES and not f.name.startswith("~$"):
                guides.append((f"guides/{f.name}", f))
    entries += guides
    return entries, len(program), guides


def check_entries(names):
    """Raise a build failure if the ZIP content breaks a rule."""
    problems = []
    seen = set()
    for name in names:
        low = name.lower()
        parts = PurePosixPath(name).parts
        base = parts[-1]
        if name in seen:
            problems.append(f"duplicate entry: {name}")
        seen.add(name)
        if "\\" in name or name.startswith("/") or ".." in parts:
            problems.append(f"bad path: {name}")
        if low.endswith(FORBIDDEN_SUFFIXES):
            problems.append(f"forbidden file type (.bat/.cmd/.ps1/.vbs/.exe): {name}")
        if "__pycache__" in parts:
            problems.append(f"__pycache__ entry: {name}")
        if base.lower().endswith(FORBIDDEN_FILE_SUFFIXES):
            problems.append(f"build leftover / log / bytecode: {name}")
        if base in FORBIDDEN_FILENAMES and not name.startswith("guides/"):
            problems.append(f"must not be shipped: {name}")
        # directories that must not appear below program/ (the ZIP root has only 3 entries)
        if name.startswith("program/"):
            inner = parts[1:-1]
            for bad in FORBIDDEN_PARTS:
                if bad in inner:
                    problems.append(f"must not be shipped ({bad}/): {name}")
    # required program content
    program = {n[len("program/"):] for n in names if n.startswith("program/")}
    for need in REQUIRED_PROGRAM_FILES:
        if need not in program:
            problems.append(f"program/ lacks {need}")
    for need in REQUIRED_PROGRAM_DIRS:
        if not any(p.startswith(need) for p in program):
            problems.append(f"program/ lacks files in {need}")
    top = {n.split("/", 1)[0] for n in names}
    extra = top - {"READ-ME-FIRST.txt", "program", "guides"}
    if extra:
        problems.append(f"unexpected top-level entries: {sorted(extra)}")
    if problems:
        fail("\n  - " + "\n  - ".join(problems))


def main():
    entries, n_program, guides = collect()
    check_entries([arc for arc, _ in entries])

    DIST_DIR.mkdir(exist_ok=True)
    tmp = ZIP_PATH.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for arcname, source in entries:
                add_file(zf, arcname, source)
        # verify what was actually written, not what we meant to write
        with zipfile.ZipFile(tmp) as zf:
            bad = zf.testzip()
            if bad:
                fail(f"corrupt entry in the new ZIP: {bad}")
            written = zf.namelist()
        check_entries(written)
        tmp.replace(ZIP_PATH)
    finally:
        if tmp.exists():
            tmp.unlink()

    print(f"Source of the program files: working tree (file list from 'git ls-files', "
          f"excluding {', '.join(EXCLUDE_PREFIXES + EXCLUDE_NAMES)})")
    print(f"ZIP: {ZIP_PATH}")
    print(f"Size: {ZIP_PATH.stat().st_size:,} bytes  ({len(written)} entries: "
          f"1 READ-ME-FIRST, {n_program} program files, {len(guides)} guides)")
    if guides:
        print("Guides included: " + ", ".join(Path(arc).name for arc, _ in guides))
    else:
        print(f"Guides included: none ({GUIDES_DIR} has no *.docx or *.pdf yet)")
    print("Entries:")
    with zipfile.ZipFile(ZIP_PATH) as zf:
        for info in zf.infolist():
            print(f"  {info.file_size:>9,}  {info.filename}")
    print("OK: no .bat/.cmd/.ps1/.vbs/.exe, no __pycache__, no secrets/data/libs/tests in the ZIP.")


if __name__ == "__main__":
    main()
