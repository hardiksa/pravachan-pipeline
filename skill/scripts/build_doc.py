#!/usr/bin/env python3
"""
Typeset a Gujarati pravachan transcript fragment as a print-ready A5 book
(PDF via WeasyPrint + DOCX via python-docx). A5 is the default and approved
layout; the older magazine layout is still available with --layout magazine.

Input is an HTML fragment using a small, fixed vocabulary of elements:

    <h1>topic name</h1>                     the document's own title (h1)
    <h2>૧. વિભાગનું નામ</h2>                 a numbered section heading
    <p>body paragraph</p>
    <p class="q"><b>પ્રશ્ન:</b> question</p>  listener line (પ્રશ્ન:/શ્રોતા:)
    <div class="verse">gatha<br>line</div>   centred verse block
    <div class="box"><p>list</p></div>        figure / enumeration box
    <p class="end">॥ closing ॥</p>

A5-only parts (authored from the talk itself, laid out by this script):

    <div class="summary">                     સારાંશ — ~5 paragraphs
      <p>...</p> ...
      <div class="stats"><p><b>એક નજરે મુખ્ય આંકડા</b><br>
          figure<br>figure</p></div>
    </div>
    <div class="mindmap">                      વિચારનકશો — 5 branches × 3 leaves
      <div class="branch" data-range="વિભાગ ૧–૩">થીમ નામ
        <div class="leaf" data-sec="૧">એક પંક્તિ</div>
        <div class="leaf" data-sec="૨">એક પંક્તિ</div>
        <div class="leaf" data-sec="૩">એક પંક્તિ</div>
      </div>
      ... exactly 5 branches ...
    </div>

p.sub / p.note / date lines in the fragment are IGNORED in A5 (the book prints
no subtitle, date or transcription note — only "number. topic").

Usage:
    python3 build_doc.py content.html --out NAME --number 17
    python3 build_doc.py content.html --out NAME --src-name "17_topic_23_7_24.docx"
    python3 build_doc.py content.html --out NAME --layout magazine --columns 2 --size 18
"""

import argparse
import html as _html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GUJ_SERIF = "Noto Serif Gujarati"
GUJ_SANS = "Noto Sans Gujarati"
BROWN_HEX = "7A2E00"        # title
RULE_HEX = "D9A066"         # brown-gold heading bar
VERSE_BG = "FAF4EC"
BOX_BG = "F6F2EE"
BOX_BORDER = "DDD0C0"
RUNHEAD_HEX = "9a8a7a"      # grey running head
TITLE_RULE_HEX = "E7D8C4"   # thin beige rule under the title

# mindmap branch colours, in fixed order
BRANCH_COLORS = ["8C3B12", "1F5673", "7A5C1E", "3F6B4A", "8A2E5B"]

GUJ_DIGITS = "૦૧૨૩૪૫૬૭૮૯"


def guj_num(n):
    """Arabic int -> Gujarati numeral string."""
    return "".join(GUJ_DIGITS[int(c)] for c in str(int(n)))


def leading_number(name):
    """Leading digits of a filename -> int, or None. Accepts arabic or Gujarati."""
    stem = Path(name).stem.strip()
    # arabic
    m = re.match(r"\s*(\d+)", stem)
    if m:
        return int(m.group(1))
    # gujarati
    m = re.match(r"\s*([" + GUJ_DIGITS + r"]+)", stem)
    if m:
        return int("".join(str(GUJ_DIGITS.index(c)) for c in m.group(1)))
    return None


def collapse_double_breaks(node, soup):
    """Collapse <br><br> to a single <br> inside a node (in place)."""
    brs = node.find_all("br")
    i = 0
    while i < len(brs) - 1:
        a, b = brs[i], brs[i + 1]
        # if b immediately follows a (only whitespace between), drop b
        nxt = a.next_sibling
        only_ws = True
        seen = None
        while nxt is not None and nxt is not b:
            if getattr(nxt, "name", None) or (isinstance(nxt, str) and nxt.strip()):
                only_ws = False
                break
            nxt = nxt.next_sibling
            seen = nxt
        if only_ws and nxt is b:
            b.extract()
            brs = node.find_all("br")
            continue
        i += 1


# ======================================================================
# Fragment parsing
# ======================================================================

class Parsed:
    def __init__(self):
        self.topic = ""
        self.body = []        # list of bs4 elements (h2/p/verse/box/end)
        self.summary = None   # bs4 div.summary or None
        self.mindmap = None   # bs4 div.mindmap or None


def parse_fragment(fragment):
    from bs4 import BeautifulSoup

    # tolerate a whole HTML document
    m = re.search(r"<body[^>]*>(.*)</body>", fragment, re.S | re.I)
    if m:
        fragment = m.group(1)
    soup = BeautifulSoup(fragment, "html.parser")
    # unwrap layout wrappers from a reused magazine page
    for d in soup.find_all("div", class_=["cols", "masthead"]):
        d.unwrap()
    for r in soup.find_all("div", class_="rule"):
        r.decompose()

    p = Parsed()
    h1 = soup.find("h1")
    if h1:
        p.topic = h1.get_text(strip=True)

    p.summary = soup.find("div", class_="summary")
    p.mindmap = soup.find("div", class_="mindmap")

    for el in soup.find_all(["h1", "h2", "p", "div"], recursive=True):
        # only top-level-ish body elements; skip anything inside summary/mindmap
        # and anything inside container divs (box/verse/stats) — their content is
        # rendered BY the container, so picking up inner <p>s would duplicate it
        if el.find_parent("div", class_=["summary", "mindmap", "box", "verse", "stats"]):
            continue
        cls = el.get("class", [])
        if el.name == "h1":
            continue
        if el.name == "p" and ("sub" in cls or "note" in cls):
            continue  # never printed in A5
        if el.name == "div" and ("summary" in cls or "mindmap" in cls):
            continue
        if el.name == "div" and not ("verse" in cls or "box" in cls or "stats" in cls):
            continue
        if el.name == "div" and "stats" in cls:
            continue  # stats only live inside summary
        p.body.append(el)

    # collapse double breaks in verses/boxes
    for el in p.body:
        if el.name == "div":
            collapse_double_breaks(el, soup)
    if p.summary:
        for st in p.summary.find_all("div", class_="stats"):
            collapse_double_breaks(st, soup)

    return soup, p


def resolve_title(parsed, number, src_name):
    """Return the 'number. topic' title string, or raise for a missing number."""
    topic = parsed.topic.strip()
    # if the topic already begins with a Gujarati/arabic number + ". ", keep it
    m = re.match(r"^\s*([" + GUJ_DIGITS + r"\d]+)\.\s+(.*)$", topic)
    if m:
        n = leading_number(m.group(1))
        return f"{guj_num(n)}. {m.group(2).strip()}", n
    if number is None and src_name:
        number = leading_number(src_name)
    if number is None:
        raise SystemExit(
            "ERROR: no leading number for the title. Pass --number N or "
            "--src-name with a leading number, or start the <h1> with 'N. '."
        )
    return f"{guj_num(number)}. {topic}", int(number)


# ======================================================================
# Mindmap HTML (shared by PDF-inline and PNG-for-DOCX)
# ======================================================================

def mindmap_html(title, mm_div):
    branches = mm_div.find_all("div", class_="branch", recursive=False) if mm_div else []
    rows = []
    for i, br in enumerate(branches[:5]):
        color = BRANCH_COLORS[i % len(BRANCH_COLORS)]
        rng = br.get("data-range", "")
        # theme name = the branch's own text nodes (excluding leaves)
        name_parts = [t for t in br.find_all(string=True, recursive=False)]
        theme = "".join(name_parts).strip()
        leaves = br.find_all("div", class_="leaf", recursive=False)
        leaf_html = []
        for lf in leaves[:3]:
            sec = lf.get("data-sec", "")
            txt = lf.get_text(" ", strip=True)
            leaf_html.append(
                f'<div class="mm-leaf">'
                f'<span class="mm-chip" style="color:#{color}">{_html.escape(sec)}</span>'
                f'<span class="mm-leaf-t">{_html.escape(txt)}</span></div>'
            )
        rows.append(
            f'<div class="mm-branch">'
            f'  <div class="mm-bar" style="background:#{color}">'
            f'    <span class="mm-theme">{_html.escape(theme)}</span>'
            f'    <span class="mm-range">{_html.escape(rng)}</span>'
            f'  </div>'
            f'  <div class="mm-leaves" style="border-color:#{color}">{"".join(leaf_html)}</div>'
            f'</div>'
        )
    banner = (
        f'<div class="mm-banner">'
        f'<div class="mm-title">{_html.escape(title)}</div>'
        f'<div class="mm-sub">વિચારનકશો</div></div>'
    )
    caption = '<div class="mm-caption">રંગીન અંક = વ્યાખ્યાનના વિભાગનો ક્રમાંક.</div>'
    return f'<div class="mm-wrap">{banner}{"".join(rows)}{caption}</div>'


MINDMAP_CSS = """
.mm-wrap{display:flex;flex-direction:column;gap:2.2mm;font-family:"%(sans)s",sans-serif;}
.mm-banner{background:#%(brown)s;border-radius:3mm;padding:2.4mm 3mm;text-align:center;color:#fff;}
.mm-title{font-weight:700;font-size:12pt;line-height:1.15;}
.mm-sub{font-weight:700;font-size:8.5pt;opacity:.9;margin-top:.6mm;}
.mm-branch{display:flex;flex-direction:column;}
.mm-bar{display:flex;justify-content:space-between;align-items:center;border-radius:2mm;
        padding:1.4mm 2.4mm;color:#fff;}
.mm-theme{font-weight:700;font-size:10.5pt;}
.mm-range{font-size:7.5pt;opacity:.92;font-weight:700;white-space:nowrap;padding-left:2mm;}
.mm-leaves{margin-left:2.4mm;border-left:1.2mm solid;padding-left:2.6mm;
           display:flex;flex-direction:column;gap:1mm;margin-top:1mm;}
.mm-leaf{display:flex;align-items:baseline;gap:1.6mm;background:#fff;
         border:0.3mm solid #%(beige)s;border-radius:1.6mm;padding:1mm 2mm;}
.mm-chip{font-weight:700;font-size:9pt;min-width:4mm;}
.mm-leaf-t{font-size:9pt;color:#222;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.mm-caption{text-align:center;font-size:7pt;color:#8a7a6a;margin-top:.8mm;}
""" % {"sans": GUJ_SANS, "brown": BROWN_HEX, "beige": BOX_BORDER}


# ======================================================================
# A5 PDF (WeasyPrint)
# ======================================================================

A5_CSS = """
@page {
  size: A5;
  margin: 10mm 9mm 10mm 14mm;
  @top-center { content: string(runhead); font-family: "%(sans)s", sans-serif;
                font-size: 7.5pt; color: #%(runhead)s; }
}
@page :right {
  margin-left: 14mm; margin-right: 9mm;
  @bottom-right { content: counter(page); font-family: "%(sans)s", sans-serif;
                  font-size: 8.5pt; color: #%(runhead)s; }
}
@page :left {
  margin-left: 9mm; margin-right: 14mm;
  @bottom-left { content: counter(page); font-family: "%(sans)s", sans-serif;
                 font-size: 8.5pt; color: #%(runhead)s; }
}
@page :first {
  @top-center { content: none; }
}
html { -weasy-hyphens: none; }
body { font-family: "%(serif)s", serif; font-size: 14pt; line-height: 1.35;
       color: #1a1a1a; text-align: justify;
       string-set: runhead "%(runhead_text)s"; }
h1.book-title { font-family: "%(sans)s", sans-serif; font-weight: 700;
                font-size: 20pt; text-align: center; color: #%(brown)s;
                margin: 0 0 2mm 0; line-height: 1.2; }
.title-rule { border-bottom: 0.5mm solid #%(titlerule)s; margin: 0 0 4mm 0; }
nav.toc { margin: 0 0 5mm 0; font-size: 13pt; }
nav.toc .toc-link { display: block; text-decoration: none; color: #2a2a2a;
                    margin: 0 0 1.2mm 0; }
nav.toc .toc-link::after {
  content: leader('.') " " target-counter(attr(href url), page);
  color: #6a5a4a; }
h2 { font-family: "%(sans)s", sans-serif; font-weight: 700; font-size: 15pt;
     color: #%(brown)s; margin: 4mm 0 1.5mm 0; border-left: 3.5px solid #%(rule)s;
     padding-left: 7px; line-height: 1.3; break-after: avoid; }
p { margin: 0 0 3pt 0; }
p.q { font-style: italic; padding-left: 10px; color: #5a4636; }
p.q b { font-style: normal; font-weight: 700; color: #%(brown)s; }
div.verse { text-align: center; background: #%(versebg)s; border-left: 3.5px solid #%(rule)s;
            line-height: 1.3; padding: 2mm; margin: 0 0 3pt 0; break-inside: avoid; }
div.box { background: #%(boxbg)s; border: 1px solid #%(boxbd)s; padding: 2mm;
          margin: 0 0 3pt 0; line-height: 1.2; break-inside: avoid; }
div.box p { margin: 0; }
p.end { text-align: center; color: #%(brown)s; margin-top: 4mm; }

section.part { break-before: page; }
section.part h2.part-h { border-left: 3.5px solid #%(rule)s; }
div.stats { background: #%(boxbg)s; border: 1px solid #%(boxbd)s; padding: 2mm;
            line-height: 1.2; margin: 3mm 0 0 0; font-size: 13pt; break-inside: avoid; }
div.stats p { margin: 0; }
div.summary p { font-size: 13pt; }

section.mindmap-page { break-before: page; break-inside: avoid; }
%(mmcss)s
"""


def _assemble_a5_html(parsed, title):
    """Build the full A5 HTML and the ordered list of index (anchor_id, label)."""
    runhead_text = title.replace("\\", "\\\\").replace('"', '\\"')
    css = A5_CSS % {
        "serif": GUJ_SERIF, "sans": GUJ_SANS, "brown": BROWN_HEX, "rule": RULE_HEX,
        "versebg": VERSE_BG, "boxbg": BOX_BG, "boxbd": BOX_BORDER,
        "runhead": RUNHEAD_HEX, "titlerule": TITLE_RULE_HEX,
        "runhead_text": runhead_text, "mmcss": MINDMAP_CSS,
    }

    index = []  # (anchor_id, label)
    toc_rows = []
    sec_i = 0
    body_html = []
    for el in parsed.body:
        if el.name == "h2":
            sec_i += 1
            sid = f"sec{sec_i}"
            el["id"] = sid
            label = el.get_text(strip=True)
            index.append((sid, label))
            toc_rows.append(f'<a class="toc-link" href="#{sid}">{label}</a>')
        body_html.append(str(el))

    summary_html = ""
    if parsed.summary:
        index.append(("sec-summary", "સારાંશ"))
        toc_rows.append('<a class="toc-link" href="#sec-summary">સારાંશ</a>')
        inner = "".join(str(c) for c in parsed.summary.children)
        summary_html = (
            '<section class="part" id="sec-summary">'
            '<h2 class="part-h">સારાંશ</h2>'
            f'<div class="summary">{inner}</div></section>'
        )

    mindmap_html_block = ""
    if parsed.mindmap:
        index.append(("sec-mindmap", "વિચારનકશો"))
        toc_rows.append('<a class="toc-link" href="#sec-mindmap">વિચારનકશો</a>')
        mindmap_html_block = (
            '<section class="mindmap-page" id="sec-mindmap">'
            + mindmap_html(title, parsed.mindmap) + '</section>'
        )

    toc = '<nav class="toc">અનુક્રમણિકા' + "".join(toc_rows) + "</nav>"
    doc = (
        "<!DOCTYPE html><html lang='gu'><head><meta charset='utf-8'>"
        f"<style>{css}</style></head><body>"
        f'<h1 class="book-title">{_html.escape(title)}</h1>'
        '<div class="title-rule"></div>'
        f"{toc}"
        + "".join(body_html)
        + summary_html
        + mindmap_html_block
        + "</body></html>"
    )
    return doc, index


def render_a5(parsed, title, out, want_pdf):
    """Render the A5 layout once. Write the PDF if requested. Return the page map
    {anchor_id: page_number} taken straight from the WeasyPrint layout — this is
    how both the PDF and DOCX index get their (identical) page numbers, with no
    external tools."""
    from weasyprint import HTML

    doc_html, index = _assemble_a5_html(parsed, title)
    document = HTML(string=doc_html).render()
    page_map = {}
    for pno, page in enumerate(document.pages, start=1):
        for anchor in page.anchors:
            page_map.setdefault(anchor, pno)
    if want_pdf:
        document.write_pdf(f"{out}.pdf")
    return page_map, [aid for aid, _ in index], len(document.pages)


def render_mindmap_png(title, mm_div, out_png, width_px=1230):
    """Render just the mindmap to a tight PNG (via WeasyPrint PDF + sips)."""
    if mm_div is None:
        return None
    from weasyprint import HTML
    css = (
        "@page{size:125mm 188mm;margin:0;}"
        f"body{{margin:3mm;font-family:'{GUJ_SANS}',sans-serif;}}" + MINDMAP_CSS
    )
    doc = (
        "<!DOCTYPE html><html lang='gu'><head><meta charset='utf-8'>"
        f"<style>{css}</style></head><body>{mindmap_html(title, mm_div)}</body></html>"
    )
    tmp_pdf = out_png.replace(".png", "._mm.pdf")
    HTML(string=doc).write_pdf(tmp_pdf)
    if shutil.which("sips"):
        subprocess.run(
            ["sips", "-s", "format", "png", "--resampleWidth", str(width_px),
             tmp_pdf, "--out", out_png],
            check=True, capture_output=True,
        )
        Path(tmp_pdf).unlink(missing_ok=True)
        return out_png
    Path(tmp_pdf).unlink(missing_ok=True)
    return None


# ======================================================================
# A5 DOCX (python-docx) — with the Word/Gujarati bug fixes
# ======================================================================

def build_a5_docx(parsed, title, out, source_text, page_map):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor, Emu

    brown = RGBColor(0x7A, 0x2E, 0x00)
    runhead_rgb = RGBColor(0x9A, 0x8A, 0x7A)

    def _set(el, **attrs):
        for k, v in attrs.items():
            el.set(qn(k), str(v))

    def font(run, pt, bold=False, italic=False, color=None, sans=False):
        run.font.size = Pt(pt)
        run.bold = bold
        run.italic = italic
        if color is not None:
            run.font.color.rgb = color
        rpr = run._element.get_or_add_rPr()
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = OxmlElement("w:rFonts")
            rpr.insert(0, rf)
        fam = GUJ_SANS if sans else GUJ_SERIF
        for a in ("w:ascii", "w:hAnsi", "w:cs"):
            rf.set(qn(a), fam)
        # complex-script counterparts — Word sizes Gujarati from these
        szcs = OxmlElement("w:szCs"); szcs.set(qn("w:val"), str(int(pt * 2))); rpr.append(szcs)
        if bold:
            bcs = OxmlElement("w:bCs"); rpr.append(bcs)
        if italic:
            ics = OxmlElement("w:iCs"); rpr.append(ics)

    def exact_spacing(par, pts):
        pf = par.paragraph_format
        pPr = par._p.get_or_add_pPr()
        spc = pPr.find(qn("w:spacing"))
        if spc is None:
            spc = OxmlElement("w:spacing"); pPr.append(spc)
        spc.set(qn("w:line"), str(int(round(pts * 20))))
        spc.set(qn("w:lineRule"), "exact")

    def shade(par, fill):
        sh = OxmlElement("w:shd"); _set(sh, **{"w:val": "clear", "w:fill": fill})
        par._p.get_or_add_pPr().append(sh)

    def left_bar(par, color, sz="28"):
        bd = OxmlElement("w:pBdr"); lb = OxmlElement("w:left")
        _set(lb, **{"w:val": "single", "w:sz": sz, "w:space": "6", "w:color": color})
        bd.append(lb); par._p.get_or_add_pPr().append(bd)

    def add_text(par, text, pt, **kw):
        # never pass "\n" to add_run — turn it into real line breaks
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if i:
                par.add_run().add_break()
            if part:
                font(par.add_run(part), pt, **kw)

    def inline(par, node, pt, italic=False):
        for child in node.children:
            nm = getattr(child, "name", None)
            if nm == "br":
                par.add_run().add_break()
            elif nm in ("b", "strong"):
                add_text(par, child.get_text(), pt, bold=True, italic=italic, color=brown if italic else None)
            elif nm in ("i", "em"):
                add_text(par, child.get_text(), pt, italic=True)
            else:
                txt = child if isinstance(child, str) else child.get_text()
                if txt:
                    add_text(par, txt, pt, italic=italic)

    doc = Document()

    # --- mirrored margins in settings ---
    settings = doc.settings.element
    mm = OxmlElement("w:mirrorMargins"); settings.append(mm)
    eao = OxmlElement("w:evenAndOddHeaders"); settings.append(eao)

    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(14.8), Cm(21.0)
    sec.top_margin = sec.bottom_margin = Cm(1.0)
    sec.left_margin = Cm(1.4)   # inner (gutter)
    sec.right_margin = Cm(0.9)  # outer
    sec.header_distance = Cm(0.6)
    sec.footer_distance = Cm(0.6)
    sec.different_first_page_header_footer = True

    def page_field(par):
        r = par.add_run()
        fb = OxmlElement("w:fldChar"); fb.set(qn("w:fldCharType"), "begin")
        it = OxmlElement("w:instrText"); it.set(qn("xml:space"), "preserve"); it.text = "PAGE"
        fe = OxmlElement("w:fldChar"); fe.set(qn("w:fldCharType"), "end")
        r._r.append(fb); r._r.append(it); r._r.append(fe)
        font(r, 8.5, color=runhead_rgb, sans=True)

    def runhead_par(par):
        par.alignment = WD_ALIGN_PARAGRAPH.CENTER
        font(par.add_run(title), 7.5, color=runhead_rgb, sans=True)

    # headers: running head centred (odd + even), first page empty
    runhead_par(sec.header.paragraphs[0])
    runhead_par(sec.even_page_header.paragraphs[0])
    sec.first_page_header.paragraphs[0].text = ""
    # footers: page number outer corner
    of = sec.footer.paragraphs[0]; of.alignment = WD_ALIGN_PARAGRAPH.RIGHT; page_field(of)
    ef = sec.even_page_footer.paragraphs[0]; ef.alignment = WD_ALIGN_PARAGRAPH.LEFT; page_field(ef)
    ff = sec.first_page_footer.paragraphs[0]; ff.alignment = WD_ALIGN_PARAGRAPH.RIGHT; page_field(ff)

    # ---- Page 1: title, rule, index ----
    tp = doc.add_paragraph(); tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    exact_spacing(tp, 26); font(tp.add_run(title), 20, bold=True, color=brown, sans=True)
    tp.paragraph_format.space_after = Pt(3)

    rule = doc.add_paragraph()
    bd = OxmlElement("w:pBdr"); bt = OxmlElement("w:bottom")
    _set(bt, **{"w:val": "single", "w:sz": "6", "w:space": "1", "w:color": TITLE_RULE_HEX})
    bd.append(bt); rule._p.get_or_add_pPr().append(bd)

    idx_hd = doc.add_paragraph(); font(idx_hd.add_run("અનુક્રમણિકા"), 15, bold=True, color=brown, sans=True)
    exact_spacing(idx_hd, 21); idx_hd.paragraph_format.space_after = Pt(2)

    # collect index entries (id, heading text) in order — ids match the page_map
    index_entries = []
    sec_i = 0
    for el in parsed.body:
        if el.name == "h2":
            sec_i += 1
            index_entries.append((f"sec{sec_i}", el.get_text(strip=True)))
    if parsed.summary:
        index_entries.append(("sec-summary", "સારાંશ"))
    if parsed.mindmap:
        index_entries.append(("sec-mindmap", "વિચારનકશો"))

    return _finish_docx(
        doc, parsed, title, out, source_text, index_entries, page_map,
        helpers=dict(font=font, exact_spacing=exact_spacing, shade=shade,
                     left_bar=left_bar, inline=inline, add_text=add_text,
                     page_field=page_field, brown=brown),
        Pt=Pt, Cm=Cm, Emu=Emu, RGBColor=RGBColor,
        WD_ALIGN_PARAGRAPH=WD_ALIGN_PARAGRAPH, OxmlElement=OxmlElement, qn=qn,
    )


def _finish_docx(doc, parsed, title, out, source_text, index_entries, page_map, helpers,
                 Pt, Cm, Emu, RGBColor, WD_ALIGN_PARAGRAPH, OxmlElement, qn):
    font = helpers["font"]; exact_spacing = helpers["exact_spacing"]
    shade = helpers["shade"]; left_bar = helpers["left_bar"]; inline = helpers["inline"]
    add_text = helpers["add_text"]; brown = helpers["brown"]

    def render_index():
        """Build the index with real page numbers from the WeasyPrint layout."""
        for anchor_id, name in index_entries:
            p = doc.add_paragraph()
            exact_spacing(p, 18)
            p.paragraph_format.space_after = Pt(1)
            # dot-leader tab to a right tab stop near the outer margin (RIGHT=2, DOTS=1)
            p.paragraph_format.tab_stops.add_tab_stop(Cm(12.0), 2, 1)
            font(p.add_run(name), 13)
            p.add_run("\t")
            pno = page_map.get(anchor_id)
            if pno is not None:
                font(p.add_run(guj_num(pno)), 13)

    def render_body():
        for el in parsed.body:
            cls = el.get("class", [])
            if el.name == "h2":
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(4)
                p.paragraph_format.space_after = Pt(1.5)
                p.paragraph_format.keep_with_next = True
                exact_spacing(p, 15 * 1.3)
                left_bar(p, RULE_HEX)
                font(p.add_run(el.get_text(strip=True)), 15, bold=True, color=brown, sans=True)
            elif el.name == "div" and "verse" in cls:
                _render_lines(el, bg=VERSE_BG, bar=RULE_HEX, pt=13, lh=1.36, center=True)
            elif el.name == "div" and "box" in cls:
                inner = el.find("p") or el
                _render_lines(inner, bg=BOX_BG, bar=BOX_BORDER, pt=13, lh=1.26, center=False)
            elif el.name == "p" and "end" in cls:
                p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_before = Pt(4)
                exact_spacing(p, 14 * 1.48)
                font(p.add_run(el.get_text(strip=True)), 13, color=brown)
            else:
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(3)
                exact_spacing(p, 14 * 1.48)
                if "q" in cls:
                    p.paragraph_format.left_indent = Cm(0.4)
                    inline(p, el, 14, italic=True)
                else:
                    inline(p, el, 14)

    def _render_lines(node, bg, bar, pt, lh, center):
        # one paragraph per <br>-separated line, zero space before/after, tight leading
        segments = _split_br(node)
        for j, seg in enumerate(segments):
            p = doc.add_paragraph()
            if center:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            shade(p, bg)
            if j == 0:
                left_bar(p, bar)
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            exact_spacing(p, pt * lh)
            for run_node in seg:
                nm = getattr(run_node, "name", None)
                if nm in ("b", "strong"):
                    add_text(p, run_node.get_text(), pt, bold=True)
                elif nm in ("i", "em"):
                    add_text(p, run_node.get_text(), pt, italic=True)
                else:
                    txt = run_node if isinstance(run_node, str) else run_node.get_text()
                    if txt:
                        add_text(p, txt, pt)

    def _split_br(node):
        segments, cur = [], []
        for child in node.children:
            if getattr(child, "name", None) == "br":
                segments.append(cur); cur = []
            else:
                cur.append(child)
        segments.append(cur)
        return [s for s in segments if any(
            (getattr(x, "name", None) or (isinstance(x, str) and x.strip())) for x in s)]

    def render_summary():
        first = doc.add_paragraph()
        _page_break_before(first)
        first.paragraph_format.space_before = Pt(0)
        exact_spacing(first, 15 * 1.3)
        left_bar(first, RULE_HEX)
        font(first.add_run("સારાંશ"), 15, bold=True, color=brown, sans=True)
        for child in parsed.summary.find_all(["p", "div"], recursive=False):
            if child.name == "div" and "stats" in child.get("class", []):
                inner = child.find("p") or child
                _render_lines(inner, bg=BOX_BG, bar=BOX_BORDER, pt=13, lh=1.26, center=False)
            elif child.name == "p":
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(3)
                exact_spacing(p, 13 * 1.48)
                inline(p, child, 13)

    def render_mindmap():
        png = f"{out}._mindmap.png"
        made = render_mindmap_png(title, parsed.mindmap, png)
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _page_break_before(p)
        if made:
            p.add_run().add_picture(made, height=Emu(int(188 / 25.4 * 914400)))
            Path(made).unlink(missing_ok=True)
        else:
            font(p.add_run("વિચારનકશો"), 15, bold=True, color=brown, sans=True)

    def _page_break_before(par):
        pPr = par._p.get_or_add_pPr()
        pbb = OxmlElement("w:pageBreakBefore"); pPr.append(pbb)

    render_index()
    render_body()
    if parsed.summary:
        render_summary()
    if parsed.mindmap:
        render_mindmap()
    doc.save(f"{out}.docx")

    ok, detail = _verify_text(f"{out}.docx", source_text)
    return f"{out}.docx", ok, detail


def _verify_text(docx_path, source_text):
    from docx import Document
    doc = Document(docx_path)
    allrun = "".join(p.text for p in doc.paragraphs)
    strip = lambda s: re.sub(r"\s+", "", s)
    src = strip(source_text)
    got = strip(allrun)
    if src and src in got:
        return True, f"body text intact ({len(src)} chars found verbatim in DOCX)"
    # find first divergence for the report
    n = min(len(src), len(got))
    i = 0
    while i < n and src[i] == got[i]:
        i += 1
    return False, (f"MISMATCH: source body {len(src)} chars not found verbatim; "
                   f"first divergence near char {i}: src=…{src[max(0,i-15):i+15]}… "
                   f"got=…{got[max(0,i-15):i+15]}…")


def body_source_text(parsed):
    """Whitespace-joined vyakhyan body text (excludes title/index/summary/mindmap)."""
    return " ".join(el.get_text(" ", strip=True) for el in parsed.body)


# ======================================================================
# Old magazine layout (kept for --layout magazine)
# ======================================================================

MAG_CSS = """
@page {{ size: A4 {orient}; margin: {mv}mm {mh}mm; }}
body {{ font-family: "{serif}", "{sans}", serif;
        font-size: {size}pt; line-height: {lh}; color: #1a1a1a;
        text-align: {align}; }}
h1 {{ font-family: "{sans}", sans-serif; font-size: {h1}pt; text-align: center;
      margin: 0 0 4px 0; color: #{brown}; line-height: 1.3; }}
.sub {{ text-align: center; font-size: {sub}pt; color: #666; margin: 0 0 6px 0; }}
.note {{ font-size: {note}pt; color: #555; text-align: center; margin: 0 0 10px 0; }}
.rule {{ border-bottom: 3px double #B8763E; margin: 8px 0 16px 0; }}
.cols {{ column-count: {cols}; column-gap: 9mm; column-rule: 1px solid #{boxbd}; }}
h2 {{ font-family: "{sans}", sans-serif; font-size: {h2}pt; color: #{brown};
      margin: 16px 0 7px 0; border-left: 4px solid #{rule}; padding-left: 8px;
      line-height: 1.35; break-after: avoid; }}
p {{ margin: 0 0 10px 0; }}
.q {{ margin: 0 0 8px 0; padding-left: 12px; color: #5a4636; font-style: italic; }}
.q b {{ font-style: normal; color: #{brown}; }}
.verse {{ background: #{versebg}; border-left: 3px solid #{rule}; padding: 8px 10px;
          margin: 0 0 11px 0; text-align: center; line-height: 1.6;
          font-size: {verse}pt; break-inside: avoid; }}
.box {{ background: #{boxbg}; border: 1px solid #{boxbd}; padding: 8px 11px;
        margin: 0 0 11px 0; break-inside: avoid; font-size: {box}pt; }}
.end {{ text-align: center; color: #{brown}; margin-top: 16px; font-size: {end}pt; }}
"""


def split_masthead(fragment):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(fragment, "html.parser")
    mast, body = [], []
    seen_body = False
    for el in soup.find_all(["h1", "h2", "p", "div"], recursive=False):
        cls = el.get("class", [])
        is_mast = el.name == "h1" or "sub" in cls or "note" in cls
        if is_mast and not seen_body:
            mast.append(el)
        else:
            seen_body = True
            body.append(el)
    return soup, mast, body


def build_pdf_magazine(fragment, out, size, cols, landscape):
    from weasyprint import HTML
    align = "left" if cols > 1 else "justify"
    css = MAG_CSS.format(
        orient="landscape" if landscape else "portrait",
        mv=13 if landscape else 14, mh=14 if landscape else 13,
        serif=GUJ_SERIF, sans=GUJ_SANS, size=size, lh=1.62, align=align,
        h1=round(size * 1.6), sub=round(size * 0.85), note=round(size * 0.72),
        h2=round(size * 1.06), verse=round(size * 0.9), box=round(size * 0.95),
        end=round(size * 0.95), cols=cols,
        brown=BROWN_HEX, rule=RULE_HEX, versebg=VERSE_BG,
        boxbg=BOX_BG, boxbd=BOX_BORDER,
    )
    _, mast, body = split_masthead(fragment)
    html = (
        "<!DOCTYPE html><html lang='gu'><head><meta charset='utf-8'>"
        f"<style>{css}</style></head><body>"
        + "".join(str(e) for e in mast)
        + "<div class='rule'></div>"
        + f"<div class='cols'>{''.join(str(e) for e in body)}</div>"
        + "</body></html>"
    )
    HTML(string=html).write_pdf(f"{out}.pdf")
    return f"{out}.pdf"


def build_docx_magazine(fragment, out, size, cols, landscape):
    from docx import Document
    from docx.enum.section import WD_ORIENT, WD_SECTION
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    brown = RGBColor(0x7A, 0x2E, 0x00)
    grey = RGBColor(0x66, 0x66, 0x66)

    def font(run, pt, bold=False, italic=False, color=None):
        run.font.size = Pt(pt); run.bold = bold; run.italic = italic
        if color is not None:
            run.font.color.rgb = color
        rpr = run._element.get_or_add_rPr()
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = OxmlElement("w:rFonts"); rpr.insert(0, rf)
        for a in ("w:ascii", "w:hAnsi", "w:cs"):
            rf.set(qn(a), GUJ_SERIF)

    def shade(par, fill):
        sh = OxmlElement("w:shd"); sh.set(qn("w:val"), "clear"); sh.set(qn("w:fill"), fill)
        par._p.get_or_add_pPr().append(sh)

    def bar(par, color):
        bd = OxmlElement("w:pBdr"); lb = OxmlElement("w:left")
        lb.set(qn("w:val"), "single"); lb.set(qn("w:sz"), "18")
        lb.set(qn("w:space"), "6"); lb.set(qn("w:color"), color)
        bd.append(lb); par._p.get_or_add_pPr().append(bd)

    def inline(par, node, pt, italic=False):
        for child in node.children:
            nm = getattr(child, "name", None)
            if nm == "br":
                par.add_run().add_break()
            elif nm in ("b", "strong"):
                font(par.add_run(child.get_text()), pt, bold=True, italic=italic)
            elif nm in ("i", "em"):
                font(par.add_run(child.get_text()), pt, italic=True)
            elif nm == "span":
                font(par.add_run(child.get_text()), pt - 2, italic=True, color=grey)
            else:
                txt = child if isinstance(child, str) else child.get_text()
                if txt:
                    font(par.add_run(txt), pt, italic=italic)

    doc = Document()
    sec = doc.sections[0]
    if landscape:
        sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width, sec.page_height = Cm(29.7), Cm(21.0)
    sec.top_margin = sec.bottom_margin = Cm(1.4)
    sec.left_margin = sec.right_margin = Cm(1.5)

    _, mast, body = split_masthead(fragment)
    for el in mast:
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if el.name == "h1":
            font(p.add_run(el.get_text(strip=True)), round(size * 1.6), True, color=brown)
        elif "sub" in el.get("class", []):
            font(p.add_run(el.get_text(strip=True)), round(size * 0.85), color=grey)
        else:
            font(p.add_run(el.get_text(strip=True)), round(size * 0.72), color=RGBColor(0x55, 0x55, 0x55))

    rule = doc.add_paragraph()
    bd = OxmlElement("w:pBdr"); bt = OxmlElement("w:bottom")
    bt.set(qn("w:val"), "double"); bt.set(qn("w:sz"), "12")
    bt.set(qn("w:space"), "1"); bt.set(qn("w:color"), "B8763E")
    bd.append(bt); rule._p.get_or_add_pPr().append(bd)

    if cols > 1:
        bsec = doc.add_section(WD_SECTION.CONTINUOUS)
        if landscape:
            bsec.orientation = WD_ORIENT.LANDSCAPE
            bsec.page_width, bsec.page_height = Cm(29.7), Cm(21.0)
        bsec.top_margin = bsec.bottom_margin = Cm(1.4)
        bsec.left_margin = bsec.right_margin = Cm(1.5)
        c = bsec._sectPr.xpath("./w:cols")[0]
        c.set(qn("w:num"), str(cols)); c.set(qn("w:space"), "480"); c.set(qn("w:sep"), "1")

    for el in body:
        cls = el.get("class", [])
        if el.name == "h2":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(12); p.paragraph_format.space_after = Pt(5)
            p.paragraph_format.keep_with_next = True; bar(p, RULE_HEX)
            font(p.add_run(el.get_text(strip=True)), round(size * 1.06), True, color=brown)
        elif "verse" in cls:
            p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            shade(p, VERSE_BG); bar(p, RULE_HEX)
            p.paragraph_format.keep_together = True; p.paragraph_format.space_after = Pt(10)
            inline(p, el, round(size * 0.9))
        elif "box" in cls:
            p = doc.add_paragraph(); shade(p, BOX_BG); bar(p, BOX_BORDER)
            p.paragraph_format.keep_together = True; p.paragraph_format.space_after = Pt(10)
            inline(p, el.find("p") or el, round(size * 0.95))
        elif "end" in cls:
            p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(14)
            font(p.add_run(el.get_text(strip=True)), round(size * 0.95), color=brown)
        else:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(8); p.paragraph_format.line_spacing = 1.15
            if "q" in cls:
                p.paragraph_format.left_indent = Cm(0.4); inline(p, el, size, italic=True)
            else:
                inline(p, el, size)

    doc.save(f"{out}.docx")
    return f"{out}.docx"


# ======================================================================
# main
# ======================================================================

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("content", help="HTML fragment file")
    ap.add_argument("--out", required=True, help="output base name, no extension")
    ap.add_argument("--format", choices=["pdf", "docx", "both"], default="both")
    ap.add_argument("--layout", choices=["a5", "magazine"], default="a5",
                    help="a5 = print-ready A5 book (default); magazine = old A4 layout")
    ap.add_argument("--number", type=int, default=None,
                    help="A5: vyakhyan number for the title (else derived from --src-name)")
    ap.add_argument("--src-name", default=None,
                    help="A5: original source filename, to derive the leading number")
    # magazine-only flags (kept)
    ap.add_argument("--size", type=int, default=18, help="magazine: body point size")
    ap.add_argument("--columns", type=int, default=2, choices=[1, 2, 3], help="magazine: columns")
    ap.add_argument("--portrait", action="store_true", help="magazine: A4 vertical")
    args = ap.parse_args()

    raw = Path(args.content).read_text(encoding="utf-8")

    if args.layout == "magazine":
        m = re.search(r"<body[^>]*>(.*)</body>", raw, re.S | re.I)
        fragment = m.group(1) if m else raw
        from bs4 import BeautifulSoup
        _s = BeautifulSoup(fragment, "html.parser")
        for d in _s.find_all("div", class_=["cols", "masthead"]):
            d.unwrap()
        for r in _s.find_all("div", class_="rule"):
            r.decompose()
        fragment = str(_s).strip()
        landscape = not args.portrait
        made = []
        if args.format in ("pdf", "both"):
            made.append(build_pdf_magazine(fragment, args.out, args.size, args.columns, landscape))
        if args.format in ("docx", "both"):
            made.append(build_docx_magazine(fragment, args.out, args.size, args.columns, landscape))
        for f in made:
            print(f"wrote {f}")
        return

    # ---- A5 ----
    _, parsed = parse_fragment(raw)
    title, number = resolve_title(parsed, args.number, args.src_name or args.content)
    src_text = body_source_text(parsed)

    want_pdf = args.format in ("pdf", "both")
    want_docx = args.format in ("docx", "both")

    # render the A5 layout once: writes the PDF (if wanted) and yields the page map
    # that feeds BOTH the PDF and DOCX index — no external tools needed.
    page_map, _ids, n_pages = render_a5(parsed, title, args.out, want_pdf)

    made = []
    if want_pdf:
        made.append(f"{args.out}.pdf")
    docx_ok = docx_detail = None
    if want_docx:
        path, docx_ok, docx_detail = build_a5_docx(parsed, title, args.out, src_text, page_map)
        made.append(path)

    for f in made:
        print(f"wrote {f}")
    print(f"title: {title}")
    print(f"pages: {n_pages}; સારાંશ p{page_map.get('sec-summary', '-')}, "
          f"વિચારનકશો p{page_map.get('sec-mindmap', '-')}")
    if docx_ok is not None:
        print(f"text-integrity (DOCX): {'PASS' if docx_ok else 'FAIL'} — {docx_detail}")


if __name__ == "__main__":
    try:
        main()
    except ImportError as e:
        sys.exit(f"missing dependency: {e}\nrun scripts/setup.sh first")
