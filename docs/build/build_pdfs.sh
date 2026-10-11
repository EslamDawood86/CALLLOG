#!/bin/bash
# Convert the Word files in docs/out to PDF with LibreOffice (same folder).
set -e
OUT="$(cd "$(dirname "$0")/../out" && pwd)"
SOFFICE=/root/.claude/skills/synced/d7c90f3f-1cba-4294-82de-172da6ff545d_4209299a-d8ff-41ab-8840-06016647936b/docx/scripts/office/soffice.py
for f in "$OUT"/*.docx; do python3 "$SOFFICE" --headless --convert-to pdf --outdir "$OUT" "$f" >/dev/null 2>&1; done
ls -la "$OUT"
