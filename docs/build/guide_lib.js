'use strict'
// Shared layout library for the NBE Call Log guides (English = LTR, Arabic = RTL).
const fs = require('fs')
const path = require('path')
const D = require('/opt/node-tools/node_modules/docx')
const {
  Document, Packer, Paragraph, TextRun, ImageRun, Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  AlignmentType, LevelFormat, Footer, PageNumber, PageBreak, HeadingLevel, TabStopType, VerticalAlign,
} = D

const GREEN = '006A4E', ORANGE = 'EE7203', INK = '1A1A19', GREY = '5A5A58', LIGHT = 'F3F6F4', LINE = 'C9D3CE'
const PAGE = { w: 11906, h: 16838, mt: 1000, mb: 1000, ml: 1100, mr: 1100 }
const CONTENT_W = PAGE.w - PAGE.ml - PAGE.mr            // 9706 DXA
const MAX_IMG_PX = 610                                  // 96-dpi pixels that fit the text width
const LRM = '‎'
const hasArabic = (s) => /[؀-ۿ]/.test(s)
const IMG_DIR = path.join(__dirname, '..', 'img')

// ---- content helpers (used by the guide files) --------------------------------------------------
const H1 = (t) => ({ k: 'h1', t })
const H2 = (t) => ({ k: 'h2', t })
const H3 = (t) => ({ k: 'h3', t })
const P = (t) => ({ k: 'p', t })
const UL = (items) => ({ k: 'ul', items })
const OL = (items) => ({ k: 'ol', items })
const NOTE = (t) => ({ k: 'box', kind: 'note', t })
const TIP = (t) => ({ k: 'box', kind: 'tip', t })
const WARN = (t) => ({ k: 'box', kind: 'warn', t })
const CODE = (t) => ({ k: 'code', t })
const TABLE = (widths, head, rows) => ({ k: 'table', widths, head, rows })
const IMG = (file, caption, legend) => ({ k: 'img', file, caption, legend })
const COVER = (o) => ({ k: 'cover', ...o })
const BREAK = () => ({ k: 'break' })
const GAP = () => ({ k: 'gap' })

function pngSize(file) {
  const b = fs.readFileSync(file)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

// ---- inline markup:  **bold**   [[UI label]]   `code`   -----------------------------------------
const TOKEN = /(\*\*[^*]+\*\*|\[\[[^\]]+\]\]|`[^`]+`)/

function makeRuns(text, o) {
  const rtl = !!o.rtl
  const out = []
  for (const part of String(text).split(TOKEN)) {
    if (!part) continue
    let t = part, kind = 'text'
    if (part.startsWith('**')) { t = part.slice(2, -2); kind = 'bold' }
    else if (part.startsWith('[[')) { t = part.slice(2, -2); kind = 'ui' }
    else if (part.startsWith('`')) { t = part.slice(1, -1); kind = 'code' }
    const arabic = hasArabic(t)
    const props = { text: t, size: o.size || 22, color: o.color || INK, bold: !!o.bold, italics: !!o.italics }
    props.font = kind === 'code' ? 'Consolas' : 'Arial'
    if (kind === 'bold') props.bold = true
    if (kind === 'ui') { props.bold = true; props.color = GREEN }
    if (kind === 'code') { props.shading = { type: ShadingType.CLEAR, fill: 'ECEFED', color: 'auto' }; props.size = (o.size || 22) - 2 }
    if (rtl) {
      if (arabic) props.rightToLeft = true
      else if (kind === 'code' || /[\\/.:@%<>0-9\u2026_()\-]/.test(t)) props.text = LRM + t + LRM     // keep paths, ports and trailing punctuation with the Latin text
    }
    out.push(new TextRun(props))
  }
  return out
}

// ---- the builder ----------------------------------------------------------------------------------
function buildDocument(spec) {
  const rtl = spec.lang === 'ar'
  let figure = 0
  const missing = []
  const numbering = {
    config: [
      { reference: 'bullets', levels: [{ level: 0, format: LevelFormat.BULLET, text: '•', alignment: AlignmentType.START,
          style: { paragraph: { indent: { left: 540, hanging: 270 } } } }] },
    ],
  }
  let listCount = 0
  const newList = () => {
    const ref = 'steps-' + (listCount++)
    numbering.config.push({ reference: ref, levels: [{ level: 0, format: LevelFormat.DECIMAL, text: '%1.', alignment: AlignmentType.START,
      style: { paragraph: { indent: { left: 540, hanging: 360 } }, run: { bold: true, color: GREEN } } }] })
    return ref
  }
  const bi = { bidirectional: rtl }
  const children = []
  const para = (text, o = {}) => new Paragraph({
    ...bi, spacing: { before: o.before ?? 0, after: o.after ?? 120, line: o.line ?? 300 }, alignment: o.align,
    keepNext: o.keepNext, keepLines: true, children: makeRuns(text, { rtl, ...o }),
  })

  for (const b of spec.blocks) {
    switch (b.k) {
      case 'h1':
        children.push(new Paragraph({ ...bi, heading: HeadingLevel.HEADING_1, pageBreakBefore: true, keepNext: true,
          children: makeRuns(b.t, { rtl, size: 36, bold: true, color: GREEN }),
          border: { bottom: { style: BorderStyle.SINGLE, size: 12, color: ORANGE, space: 4 } }, spacing: { before: 0, after: 240 } }))
        break
      case 'h2':
        children.push(new Paragraph({ ...bi, heading: HeadingLevel.HEADING_2, keepNext: true,
          children: makeRuns(b.t, { rtl, size: 28, bold: true, color: GREEN }), spacing: { before: 320, after: 140 } }))
        break
      case 'h3':
        children.push(new Paragraph({ ...bi, heading: HeadingLevel.HEADING_3, keepNext: true,
          children: makeRuns(b.t, { rtl, size: 24, bold: true, color: INK }), spacing: { before: 220, after: 100 } }))
        break
      case 'p': children.push(para(b.t)); break
      case 'ul':
        for (const it of b.items) children.push(new Paragraph({ ...bi, numbering: { reference: 'bullets', level: 0 },
          spacing: { after: 80, line: 300 }, keepLines: true, children: makeRuns(it, { rtl }) }))
        children.push(new Paragraph({ spacing: { after: 60 }, children: [] }))
        break
      case 'ol': {
        const ref = newList()
        for (const it of b.items) children.push(new Paragraph({ ...bi, numbering: { reference: ref, level: 0 },
          spacing: { after: 100, line: 300 }, keepLines: true, children: makeRuns(it, { rtl }) }))
        children.push(new Paragraph({ spacing: { after: 60 }, children: [] }))
        break
      }
      case 'box': {
        const col = b.kind === 'warn' ? 'C0392B' : b.kind === 'tip' ? GREEN : '8A5A00'
        const fill = b.kind === 'warn' ? 'FBEDEB' : b.kind === 'tip' ? 'E8F3EE' : 'FFF4E0'
        const label = (spec.labels || {})[b.kind] || b.kind
        const side = { style: BorderStyle.SINGLE, size: 6, color: col, space: 6 }
        children.push(new Paragraph({ ...bi, keepLines: true, spacing: { before: 120, after: 160, line: 290 },
          shading: { type: ShadingType.CLEAR, fill, color: 'auto' },
          border: { top: side, bottom: side, left: side, right: side },
          indent: { left: 140, right: 140 },
          children: [...makeRuns('**' + label + '** ', { rtl, color: col, size: 21 }), ...makeRuns(b.t, { rtl, size: 21 })] }))
        break
      }
      case 'code':
        for (const line of b.t.split('\n')) children.push(new Paragraph({ bidirectional: false, alignment: AlignmentType.LEFT,
          keepLines: true, spacing: { before: 0, after: 0, line: 280 },
          shading: { type: ShadingType.CLEAR, fill: 'EEF1EF', color: 'auto' }, indent: { left: 200, right: 200 },
          children: [new TextRun({ text: ' ' + line, font: 'Consolas', size: 20, color: '202020' })] }))
        children.push(new Paragraph({ spacing: { after: 140 }, children: [] }))
        break
      case 'table': {
        const bd = { style: BorderStyle.SINGLE, size: 4, color: LINE }
        const borders = { top: bd, bottom: bd, left: bd, right: bd }
        const total = b.widths.reduce((a, c) => a + c, 0)
        const cell = (txt, w, head) => new TableCell({
          width: { size: w, type: WidthType.DXA }, borders, verticalAlign: VerticalAlign.TOP,
          margins: { top: 70, bottom: 70, left: 110, right: 110 },
          shading: head ? { type: ShadingType.CLEAR, fill: GREEN, color: 'auto' } : undefined,
          children: String(txt).split('\n').map((line) => new Paragraph({ ...bi, spacing: { after: 40, line: 270 },
            children: makeRuns(line, { rtl, size: 20, bold: head, color: head ? 'FFFFFF' : INK }) })),
        })
        children.push(new Table({ width: { size: total, type: WidthType.DXA }, columnWidths: b.widths, visuallyRightToLeft: rtl,
          rows: [
            new TableRow({ tableHeader: true, cantSplit: true, children: b.head.map((h, i) => cell(h, b.widths[i], true)) }),
            ...b.rows.map((r) => new TableRow({ cantSplit: true, children: r.map((c, i) => cell(c, b.widths[i], false)) })),
          ] }))
        children.push(new Paragraph({ spacing: { after: 160 }, children: [] }))
        break
      }
      case 'img': {
        const file = path.join(IMG_DIR, b.file)
        if (!fs.existsSync(file)) { missing.push(b.file); children.push(para('[missing image: ' + b.file + ']', { color: 'C0392B' })); break }
        const { w, h } = pngSize(file)
        const scale = Math.min(1, MAX_IMG_PX / w)
        const tw = Math.round(w * scale), th = Math.round(h * scale)
        figure++
        children.push(new Paragraph({ alignment: AlignmentType.CENTER, keepNext: true, spacing: { before: 120, after: 60 },
          border: { top: { style: BorderStyle.SINGLE, size: 4, color: LINE, space: 4 }, bottom: { style: BorderStyle.SINGLE, size: 4, color: LINE, space: 4 },
            left: { style: BorderStyle.SINGLE, size: 4, color: LINE, space: 4 }, right: { style: BorderStyle.SINGLE, size: 4, color: LINE, space: 4 } },
          children: [new ImageRun({ type: 'png', data: fs.readFileSync(file), transformation: { width: tw, height: th },
            altText: { title: b.caption, description: b.caption, name: b.file } })] }))
        children.push(new Paragraph({ ...bi, alignment: AlignmentType.CENTER, keepNext: !!b.legend, spacing: { after: b.legend ? 40 : 200 },
          children: makeRuns(`${(spec.labels || {}).figure || 'Figure'} ${figure} – ${b.caption}`, { rtl, size: 18, italics: true, color: GREY }) }))
        if (b.legend) children.push(new Paragraph({ ...bi, alignment: AlignmentType.CENTER, spacing: { after: 200 },
          children: makeRuns(b.legend, { rtl, size: 19, color: INK }) }))
        break
      }
      case 'cover': {
        const logo = path.join(IMG_DIR, 'logo.png')
        children.push(new Paragraph({ spacing: { before: 1400, after: 200 }, alignment: AlignmentType.CENTER,
          children: [new ImageRun({ type: 'png', data: fs.readFileSync(logo), transformation: { width: 150, height: 150 }, altText: { title: 'NBE', description: 'NBE logo', name: 'logo' } })] }))
        children.push(new Paragraph({ bidirectional: rtl, alignment: AlignmentType.CENTER, spacing: { after: 120 }, children: makeRuns(b.title, { rtl, size: 64, bold: true, color: GREEN }) }))
        children.push(new Paragraph({ bidirectional: rtl, alignment: AlignmentType.CENTER, spacing: { after: 100 }, children: makeRuns(b.subtitle, { rtl, size: 36, color: INK }) }))
        children.push(new Paragraph({ bidirectional: rtl, alignment: AlignmentType.CENTER, spacing: { after: 700 },
          border: { bottom: { style: BorderStyle.SINGLE, size: 18, color: ORANGE, space: 12 } }, children: makeRuns(b.tagline, { rtl, size: 24, color: GREY }) }))
        for (const line of b.contents || []) children.push(new Paragraph({ bidirectional: rtl, alignment: AlignmentType.CENTER, spacing: { after: 100 }, children: makeRuns(line, { rtl, size: 26 }) }))
        children.push(new Paragraph({ bidirectional: rtl, alignment: AlignmentType.CENTER, spacing: { before: 1200 }, children: makeRuns(b.version, { rtl, size: 20, color: GREY }) }))
        break
      }
      case 'break': children.push(new Paragraph({ children: [new PageBreak()] })); break
      case 'gap': children.push(new Paragraph({ spacing: { after: 200 }, children: [] })); break
      default: throw new Error('unknown block ' + b.k)
    }
  }

  const footer = new Footer({ children: [new Paragraph({ bidirectional: rtl, alignment: AlignmentType.CENTER, border: { top: { style: BorderStyle.SINGLE, size: 4, color: LINE, space: 6 } },
    children: [
      new TextRun({ text: spec.footer + '   ·   ' + (spec.labels || {}).page + ' ', size: 17, color: GREY, font: 'Arial', rightToLeft: rtl && hasArabic(spec.footer) }),
      new TextRun({ children: [PageNumber.CURRENT], size: 17, color: GREY, font: 'Arial' }),
    ] })] })

  const doc = new Document({
    creator: 'NBE Call Log', title: spec.title, description: spec.title,
    styles: { default: { document: { run: { font: 'Arial', size: 22 } } },
      paragraphStyles: [
        { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 36, bold: true, color: GREEN }, paragraph: { outlineLevel: 0 } },
        { id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 28, bold: true, color: GREEN }, paragraph: { outlineLevel: 1 } },
        { id: 'Heading3', name: 'Heading 3', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 24, bold: true, color: INK }, paragraph: { outlineLevel: 2 } },
      ] },
    numbering,
    sections: [{ properties: { page: { size: { width: PAGE.w, height: PAGE.h }, margin: { top: PAGE.mt, bottom: PAGE.mb, left: PAGE.ml, right: PAGE.mr } },
      ...(rtl ? { bidi: true } : {}) }, footers: { default: footer }, children }],
  })
  return { doc, missing }
}

async function writeDoc(spec, outFile) {
  const { doc, missing } = buildDocument(spec)
  fs.writeFileSync(outFile, await Packer.toBuffer(doc))
  return missing
}

module.exports = { COVER, H1, H2, H3, P, UL, OL, NOTE, TIP, WARN, CODE, TABLE, IMG, BREAK, GAP, writeDoc, CONTENT_W, GREEN, ORANGE, makeRuns, IMG_DIR, pngSize, D }
