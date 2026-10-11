'use strict'
// Builds the Word guides into docs/out (then convert to PDF with build_pdfs.sh).
const path = require('path')
const L = require('./guide_lib')
const { writeCard } = require('./card')
const OUT = path.join(__dirname, '..', 'out')
;(async () => {
  const jobs = [
    [require('./guide_en'), 'NBE-CallLog-User-Guide-EN.docx'],
    [require('./guide_ar'), 'NBE-CallLog-User-Guide-AR.docx'],
  ]
  for (const [spec, name] of jobs) {
    const missing = await L.writeDoc(spec, path.join(OUT, name))
    console.log('wrote', name, missing.length ? 'MISSING IMAGES: ' + missing.join(', ') : '(all images found)')
  }
  await writeCard(path.join(OUT, 'Employee-Quick-Card.docx'))
  console.log('wrote Employee-Quick-Card.docx')
})().catch((e) => { console.error(e); process.exit(1) })
