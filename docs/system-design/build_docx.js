// Builds the .docx files in this folder from source/*.md.
// Usage (needs the `docx` npm package): npm install docx && node build_docx.js source .
// Supports: # / ## / ### headings, paragraphs, **bold**, `code`, - bullets (2-space nesting),
// 1. numbered lists, | tables |, ``` code blocks ```, > callouts, --- page break.
// The first "# " line is the document title; a following "_..._" line is its subtitle.
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell, WidthType, ShadingType,
  BorderStyle, AlignmentType, LevelFormat, Footer, Header, PageNumber, PageBreak, TableLayoutType,
} = require("docx");

const FONT = "Calibri", MONO = "Consolas";
const ACCENT = "7A4B2A", INK = "1F1D1A", MUTED = "6F6A63", LINE = "D9D3CB", HEAD_BG = "F1E7DE", CODE_BG = "F5F3F0";
const PAGE_W = 12240, MARGIN = 1080, CONTENT_W = PAGE_W - 2 * MARGIN;   // US Letter, 0.75" margins

function runs(text, base = {}) {
  // **bold** and `code` spans
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|(?<![\w*])\*[^*\s][^*]*\*(?![\w*]))/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), ...base }));
    const tok = m[0];
    if (tok.startsWith("**")) out.push(...runs(tok.slice(2, -2), { ...base, bold: true }));
    else if (tok.startsWith("*")) out.push(new TextRun({ text: tok.slice(1, -1), italics: true, ...base }));
    else out.push(new TextRun({ text: tok.slice(1, -1), font: MONO, size: (base.size || 21) - 2, color: "5A3A22", ...base, bold: base.bold }));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), ...base }));
  return out;
}

function cell(text, width, header, shade) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: header ? { type: ShadingType.CLEAR, fill: HEAD_BG, color: "auto" } : shade ? { type: ShadingType.CLEAR, fill: "FAF8F6", color: "auto" } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: text.split("<br>").map((t) => new Paragraph({ spacing: { after: 0 }, children: runs(t.trim(), { size: 18, bold: header || undefined }) })),
  });
}

function table(lines) {
  const rows = lines.map((l) => l.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.replace(/\\\|/g, "|").trim()));
  const header = rows[0];
  const body = rows.slice(2);
  const n = header.length;
  // width by content length, clamped
  const lens = header.map((_, i) => Math.max(...rows.filter((_, r) => r !== 1).map((r) => (r[i] || "").length)));
  const weights = lens.map((l) => Math.min(Math.max(l, 12), 42));
  const total = weights.reduce((a, b) => a + b, 0);
  let widths = weights.map((w) => Math.floor((CONTENT_W * w) / total));
  widths[n - 1] += CONTENT_W - widths.reduce((a, b) => a + b, 0);
  const border = { style: BorderStyle.SINGLE, size: 4, color: LINE };
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: widths, layout: TableLayoutType.FIXED,
    borders: { top: border, bottom: border, left: border, right: border, insideHorizontal: border, insideVertical: border },
    rows: [
      new TableRow({ tableHeader: true, children: header.map((h, i) => cell(h, widths[i], true)) }),
      ...body.map((r, ri) => new TableRow({ children: header.map((_, i) => cell(r[i] || "", widths[i], false, ri % 2 === 1)) })),
    ],
  });
}

function codeBlock(lines) {
  return lines.map((l, i) => new Paragraph({
    shading: { type: ShadingType.CLEAR, fill: CODE_BG, color: "auto" },
    spacing: { after: i === lines.length - 1 ? 160 : 0, line: 240 },
    indent: { left: 120, right: 120 },
    children: [new TextRun({ text: l.length ? l : " ", font: MONO, size: 16, color: "2B2724" })],
  }));
}

function convert(md, { docLabel: fileLabel }) {
  const docLabel = (md.match(/^# (.+)/m) || [null, fileLabel])[1];
  const lines = md.replace(/\r/g, "").split("\n");
  const children = [];
  let title = null, i = 0, numberedInstance = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const block = []; i++;
      while (i < lines.length && !lines[i].startsWith("```")) block.push(lines[i++]);
      i++; children.push(...codeBlock(block)); continue;
    }
    if (/^\s*\|/.test(line)) {
      const block = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) block.push(lines[i++]);
      children.push(table(block), new Paragraph({ spacing: { after: 120 }, children: [] })); continue;
    }
    if (line.trim() === "---") { children.push(new Paragraph({ children: [new PageBreak()] })); i++; continue; }
    let m;
    if ((m = line.match(/^# (.+)/)) && !title) {
      title = m[1];
      children.push(new Paragraph({ spacing: { before: 1800, after: 120 }, children: [new TextRun({ text: docLabel.toUpperCase(), size: 20, color: ACCENT, bold: true, characterSpacing: 40 })] }));
      children.push(new Paragraph({ spacing: { after: 160 }, children: [new TextRun({ text: title, size: 52, bold: true, color: INK })] }));
      if (lines[i + 1] && /^_.+_$/.test(lines[i + 1].trim())) {
        children.push(new Paragraph({ spacing: { after: 240 }, border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: ACCENT, space: 8 } },
          children: runs(lines[i + 1].trim().slice(1, -1), { size: 24, color: MUTED, italics: true }) }));
        i++;
      }
      i++; continue;
    }
    if ((m = line.match(/^(#{2,4}) (.+)/))) {
      const lvl = { 2: HeadingLevel.HEADING_1, 3: HeadingLevel.HEADING_2, 4: HeadingLevel.HEADING_3 }[m[1].length];
      children.push(new Paragraph({ heading: lvl, children: runs(m[2]) })); i++; continue;
    }
    if ((m = line.match(/^(\s*)- (.+)/))) {
      const level = Math.min(Math.floor(m[1].length / 2), 2);
      children.push(new Paragraph({ numbering: { reference: "bullets", level }, spacing: { after: 40 }, children: runs(m[2]) })); i++; continue;
    }
    if ((m = line.match(/^(\s*)\d+\. (.+)/))) {
      // a run of numbered items forms one list instance
      if (i === 0 || !/^\s*\d+\. /.test(lines[i - 1]) && !/^\s+- /.test(lines[i - 1])) numberedInstance++;
      const level = Math.min(Math.floor(m[1].length / 3), 1);
      children.push(new Paragraph({ numbering: { reference: "numbers", level, instance: numberedInstance }, spacing: { after: 40 }, children: runs(m[2]) })); i++; continue;
    }
    if ((m = line.match(/^> (.+)/))) {
      const block = [m[1]]; i++;
      while (i < lines.length && lines[i].startsWith("> ")) block.push(lines[i++].slice(2));
      children.push(new Paragraph({ shading: { type: ShadingType.CLEAR, fill: "FBF3EC", color: "auto" },
        border: { left: { style: BorderStyle.SINGLE, size: 18, color: ACCENT, space: 8 } }, indent: { left: 200 },
        spacing: { before: 80, after: 160 }, children: runs(block.join(" "), { size: 20 }) }));
      continue;
    }
    if (!line.trim()) { i++; continue; }
    // paragraph: join consecutive plain lines
    const block = [line.trim()]; i++;
    while (i < lines.length && lines[i].trim() && !/^(#|\s*- |\s*\d+\. |\s*\||```|> |---)/.test(lines[i])) block.push(lines[i++].trim());
    children.push(new Paragraph({ spacing: { after: 140 }, children: runs(block.join(" ")) }));
  }
  return new Document({
    creator: "Custom Closet Marketing Agent", title: title || docLabel,
    styles: {
      default: { document: { run: { font: FONT, size: 21, color: INK }, paragraph: { spacing: { line: 276 } } } },
      paragraphStyles: [
        { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 32, bold: true, color: ACCENT }, paragraph: { spacing: { before: 360, after: 120 }, outlineLevel: 0, keepNext: true } },
        { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 25, bold: true, color: INK }, paragraph: { spacing: { before: 240, after: 80 }, outlineLevel: 1, keepNext: true } },
        { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 22, bold: true, color: "4A4640" }, paragraph: { spacing: { before: 180, after: 60 }, outlineLevel: 2, keepNext: true } },
      ],
    },
    numbering: { config: [
      { reference: "bullets", levels: [0, 1, 2].map((level) => ({ level, format: LevelFormat.BULLET, text: ["•", "◦", "▪"][level], alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 360 * (level + 1), hanging: 260 } } } })) },
      { reference: "numbers", levels: [0, 1].map((level) => ({ level, format: level ? LevelFormat.LOWER_LETTER : LevelFormat.DECIMAL, text: `%${level + 1}.`, alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 360 * (level + 1), hanging: 300 } } } })) },
    ] },
    sections: [{
      properties: { page: { size: { width: PAGE_W, height: 15840 }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
      headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new TextRun({ text: `Custom Closet Marketing Agent · ${docLabel}`, size: 16, color: MUTED })] })] }) },
      footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ children: ["Page ", PageNumber.CURRENT, " of ", PageNumber.TOTAL_PAGES], size: 16, color: MUTED })] })] }) },
      children,
    }],
  });
}

async function main() {
  const [srcDir, outDir] = process.argv.slice(2);
  fs.mkdirSync(outDir, { recursive: true });
  for (const f of fs.readdirSync(srcDir).filter((f) => f.endsWith(".md")).sort()) {
    const md = fs.readFileSync(path.join(srcDir, f), "utf8");
    const label = f.replace(/^\d+-/, "").replace(/\.md$/, "").replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    const buf = await Packer.toBuffer(convert(md, { docLabel: label }));
    fs.writeFileSync(path.join(outDir, f.replace(/\.md$/, ".docx")), buf);
    console.log("wrote", f.replace(/\.md$/, ".docx"));
  }
}
main().catch((e) => { console.error(e); process.exit(1); });
