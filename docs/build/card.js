'use strict'
// One-page bilingual Employee Quick Card (English left, Arabic right).
const fs = require('fs')
const path = require('path')
const L = require('./guide_lib')
const { D, makeRuns, GREEN, ORANGE, IMG_DIR } = L
const { Document, Packer, Paragraph, TextRun, ImageRun, Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle, AlignmentType, VerticalAlign } = D

const W = 9906                 // content width (A4, 0.7in margins)
const COL = [4953, 4953]
const bd = { style: BorderStyle.SINGLE, size: 4, color: 'C9D3CE' }
const borders = { top: bd, bottom: bd, left: bd, right: bd }
const none = { style: BorderStyle.NONE, size: 0, color: 'FFFFFF' }

function paras(text, rtl, o = {}) {
  return String(text).split('\n').map((line) => new Paragraph({ bidirectional: rtl, spacing: { after: 50, line: 270 }, alignment: o.align,
    children: makeRuns(line, { rtl, size: o.size || 24, bold: o.bold, color: o.color }) }))
}
function cell(text, rtl, o = {}) {
  return new TableCell({ width: { size: COL[0], type: WidthType.DXA }, borders, verticalAlign: VerticalAlign.CENTER,
    margins: { top: 130, bottom: 130, left: 160, right: 160 },
    shading: o.fill ? { type: ShadingType.CLEAR, fill: o.fill, color: 'auto' } : undefined, children: paras(text, rtl, o) })
}
function row(en, ar, o = {}) { return new TableRow({ cantSplit: true, children: [cell(en, false, o), cell(ar, true, o)] }) }

const STEPS = [
  ['**1.** Sign in with your **User ID** and **password**.', '**1.** سجّل الدخول بـ **User ID** (رقمك الوظيفي) و**كلمة المرور**.'],
  ['**2.** Type the **IP phone** that received the call (digits only).', '**2.** اكتب رقم الـ **IP Phone** الذي استقبل المكالمة (أرقام فقط).'],
  ['**3. Branch** – only if the caller is a branch: type its number (for example `100`) and press **Tab**.\nCaller is a sector or anyone else? **Leave Branch empty.**', '**3. الفرع** – فقط إذا كان المتصل فرعاً: اكتب رقمه (مثل `100`) واضغط **Tab**.\nالمتصل قطاع أو جهة أخرى؟ **اترك خانة الفرع فارغة.**'],
  ['**4.** Click the **reason** – the call is saved at once.', '**4.** اضغط **سبب المكالمة** – تُحفظ المكالمة فوراً.'],
  ['**5.** Check that your call is **at the top** of the list. A mistake? Use **Edit** or the red **bin** on that row.', '**5.** تأكد أن مكالمتك ظهرت **في أعلى** القائمة. خطأ؟ استخدم **Edit** أو زر **السلة الحمراء** في نفس الصف.'],
  ['**6.** Leaving? Click **Sign out**.\nForgot your password? Ask your administrator.', '**6.** عند المغادرة اضغط **Sign out**.\nنسيت كلمة المرور؟ اطلب من مسؤول النظام.'],
]
const REASONS = [
  ['تم الرد على الاستفسار – Inquiry answered', '→ Resolved'],
  ['تم حل المشكلة – Problem solved', '→ Resolved'],
  ['تم التحويل للجهة المختصة – Transferred', '→ Transferred'],
  ['متابعة طلب قائم – Follow-up on an existing request', '→ Ticket Inquiry'],
]

async function writeCard(outFile) {
  const logo = fs.readFileSync(path.join(IMG_DIR, 'logo.png'))
  const head = new Table({ width: { size: W, type: WidthType.DXA }, columnWidths: [1300, 8606], rows: [new TableRow({ children: [
    new TableCell({ width: { size: 1300, type: WidthType.DXA }, borders: { top: none, bottom: none, left: none, right: none }, children: [new Paragraph({ children: [new ImageRun({ type: 'png', data: logo, transformation: { width: 70, height: 70 }, altText: { title: 'NBE', description: 'NBE logo', name: 'logo' } })] })] }),
    new TableCell({ width: { size: 8606, type: WidthType.DXA }, borders: { top: none, bottom: none, left: none, right: none }, verticalAlign: VerticalAlign.CENTER, children: [
      new Paragraph({ spacing: { after: 20 }, children: [new TextRun({ text: 'NBE Call Log – Employee Quick Card', bold: true, size: 36, color: GREEN, font: 'Arial' })] }),
      new Paragraph({ bidirectional: true, spacing: { after: 0 }, children: [new TextRun({ text: 'NBE Call Log – بطاقة الموظف السريعة', bold: true, size: 32, color: GREEN, font: 'Arial', rightToLeft: true })] }),
    ] }) ] })] })

  const address = new Table({ width: { size: W, type: WidthType.DXA }, columnWidths: COL, rows: [row(
    '**Open this address in your browser:**\n`http://____________________:8080`\n(your administrator writes it here)',
    '**افتح هذا العنوان في المتصفح:**\n`http://____________________:8080`\n(يكتبه مسؤول النظام هنا)', { fill: 'FFF4E0' })] })

  const steps = new Table({ width: { size: W, type: WidthType.DXA }, columnWidths: COL, rows: [
    new TableRow({ tableHeader: true, children: [cell('**Log a call – 6 steps**', false, { fill: GREEN, color: 'FFFFFF', size: 24 }), cell('**تسجيل مكالمة – 6 خطوات**', true, { fill: GREEN, color: 'FFFFFF', size: 24 })] }),
    ...STEPS.map(([en, ar]) => row(en, ar)) ] })

  const RW = [6600, 3306]
  const rcell = (t, w, rtl, fill, bold) => new TableCell({ width: { size: w, type: WidthType.DXA }, borders, margins: { top: 90, bottom: 90, left: 140, right: 140 }, verticalAlign: VerticalAlign.CENTER,
    shading: fill ? { type: ShadingType.CLEAR, fill, color: 'auto' } : undefined, children: paras(t, rtl, { size: 22, bold, color: fill === GREEN ? 'FFFFFF' : undefined }) })
  const reasons = new Table({ width: { size: W, type: WidthType.DXA }, columnWidths: RW, rows: [
    new TableRow({ children: [rcell('Reason button  ·  زر السبب', RW[0], false, GREEN, true), rcell('Counted as  ·  يُحتسب كـ', RW[1], false, GREEN, true)] }),
    ...REASONS.map(([a, b]) => new TableRow({ cantSplit: true, children: [rcell(a, RW[0], false), rcell(b, RW[1], false)] })) ] })

  const gap = () => new Paragraph({ spacing: { after: 160 }, children: [] })
  const doc = new Document({ creator: 'NBE Call Log', title: 'NBE Call Log – Employee Quick Card',
    styles: { default: { document: { run: { font: 'Arial', size: 21 } } } },
    sections: [{ properties: { page: { size: { width: 11906, height: 16838 }, margin: { top: 800, bottom: 700, left: 1000, right: 1000 } } },
      children: [head, gap(), address, gap(), steps, gap(), reasons,
        new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 160 }, children: [new TextRun({ text: 'Problems? Ask your administrator  ·  ', size: 18, color: '5A5A58', font: 'Arial' }), new TextRun({ text: 'مشكلة؟ اسأل مسؤول النظام', size: 18, color: '5A5A58', font: 'Arial', rightToLeft: true })] })] }] })
  fs.writeFileSync(outFile, await Packer.toBuffer(doc))
}
module.exports = { writeCard }
