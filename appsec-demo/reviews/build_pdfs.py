#!/usr/bin/env python3
"""Build the demo AppSec PDFs into ./pdf and write ./pdf/index.json for the showcase page.

  python3 build_pdfs.py          (needs: pip install reportlab; run pipeline/run_pipeline.py first)

Review findings are tied to the real demo code: each finding's `loc` is resolved to a file, line
and code excerpt under ../apps/<slug>/, and the pipeline evidence tables come from
../pipeline/sample-run/pipeline-run.json. Nothing is typed twice.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageBreak, PageTemplate, Paragraph, Preformatted, Spacer, Table, TableStyle)

import content as C
from cvss import base_score, severity as cvss_severity

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "pdf"
SEV_ORDER = ["Critical", "High", "Medium", "Low", "Info"]
NAVY, INK, MUTED, RULE, PAPER = colors.HexColor("#14213d"), colors.HexColor("#1f2937"), colors.HexColor("#64748b"), colors.HexColor("#d5dbe5"), colors.HexColor("#f4f6fa")
SEV = {"Critical": colors.HexColor("#991b1b"), "High": colors.HexColor("#c2410c"), "Medium": colors.HexColor("#a16207"), "Low": colors.HexColor("#1d4ed8"), "Info": colors.HexColor("#475569")}
OK, WARN, BAD = colors.HexColor("#15803d"), colors.HexColor("#a16207"), colors.HexColor("#b91c1c")
DECISION = {"Not approved": BAD, "Approved with conditions": WARN, "Approved": OK}
ANSWER = colors.HexColor("#1e3a8a")
W = A4[0] - 2 * 18 * mm

ss = getSampleStyleSheet()
S = {
    "body": ParagraphStyle("body", parent=ss["Normal"], fontName="Helvetica", fontSize=9.2, leading=13, textColor=INK, spaceAfter=5),
    "small": ParagraphStyle("small", parent=ss["Normal"], fontName="Helvetica", fontSize=8, leading=10.5, textColor=INK),
    "muted": ParagraphStyle("muted", parent=ss["Normal"], fontName="Helvetica", fontSize=8, leading=10.5, textColor=MUTED),
    "h1": ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=NAVY, spaceBefore=12, spaceAfter=6),
    "h2": ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=NAVY, spaceBefore=9, spaceAfter=4),
    "label": ParagraphStyle("label", parent=ss["Normal"], fontName="Helvetica-Bold", fontSize=7.5, leading=10, textColor=MUTED),
    "answer": ParagraphStyle("answer", parent=ss["Normal"], fontName="Helvetica-Oblique", fontSize=9, leading=12, textColor=ANSWER),
    "cell": ParagraphStyle("cell", parent=ss["Normal"], fontName="Helvetica", fontSize=8, leading=10.5, textColor=INK),
    "cellb": ParagraphStyle("cellb", parent=ss["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=10.5, textColor=INK),
    "chip": ParagraphStyle("chip", parent=ss["Normal"], fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=colors.white, alignment=TA_CENTER),
    "code": ParagraphStyle("code", fontName="Courier", fontSize=7, leading=8.6, textColor=INK),
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=26, leading=31, textColor=colors.white),
    "sub": ParagraphStyle("sub", fontName="Helvetica", fontSize=12, leading=16, textColor=colors.HexColor("#cbd5e1")),
}


def P(text, style="body"):
    return Paragraph(text, S[style])


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Numbered(canvas.Canvas):
    """Two-pass canvas giving 'Page x of y' plus the running header and demo footer."""
    title = ""
    ref = ""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.pages = []

    def showPage(self):
        self.pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self.pages)
        for state in self.pages:
            self.__dict__.update(state)
            if self._pageNumber > 1:
                self.setStrokeColor(RULE)
                self.line(18 * mm, A4[1] - 14 * mm, A4[0] - 18 * mm, A4[1] - 14 * mm)
                self.setFont("Helvetica", 7.5)
                self.setFillColor(MUTED)
                self.drawString(18 * mm, A4[1] - 11.5 * mm, self.title)
                self.drawRightString(A4[0] - 18 * mm, A4[1] - 11.5 * mm, self.ref)
            self.setFont("Helvetica", 7)
            self.setFillColor(MUTED)
            self.drawString(18 * mm, 10 * mm, "DEMO DOCUMENT - Acme Corp is fictional and every finding, name and date is invented for a proof of concept.")
            self.drawRightString(A4[0] - 18 * mm, 10 * mm, "Page %d of %d" % (self._pageNumber, total))
            super().showPage()
        super().save()


def make_doc(path, title, ref, subject):
    Numbered.title, Numbered.ref = title, ref
    doc = BaseDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=20 * mm, bottomMargin=17 * mm,
                          title=title, author="Acme Corp Application Security (demo)", subject=subject, creator="appsec-demo/reviews/build_pdfs.py")
    doc.addPageTemplates([PageTemplate(id="p", frames=[Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)])])
    return doc


def cover(kind, title, subtitle, banner=None, banner_color=NAVY, meta=()):
    head = Table([[P(kind.upper(), "chip")], [Paragraph(title, S["title"])], [Paragraph(subtitle, S["sub"])]], colWidths=[W])
    head.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("LEFTPADDING", (0, 0), (-1, -1), 16), ("TOPPADDING", (0, 0), (0, 0), 22), ("TOPPADDING", (0, 1), (0, 1), 26),
                              ("BOTTOMPADDING", (0, 2), (0, 2), 34), ("ALIGN", (0, 0), (0, 0), "LEFT")]))
    out = [Spacer(1, 6), head]
    if banner:
        b = Table([[Paragraph("<font color='white'><b>%s</b></font>" % banner[0], ParagraphStyle("b", fontName="Helvetica-Bold", fontSize=14, leading=18)), Paragraph("<font color='white'>%s</font>" % banner[1], S["small"])]], colWidths=[W * 0.34, W * 0.66])
        b.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), banner_color), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 16), ("TOPPADDING", (0, 0), (-1, -1), 11), ("BOTTOMPADDING", (0, 0), (-1, -1), 11)]))
        out += [b]
    out.append(Spacer(1, 14))
    if meta:
        out.append(kv_table(meta))
    return out


def kv_table(rows, widths=(W * 0.27, W * 0.73)):
    t = Table([[P(k, "label"), P(v, "cell")] for k, v in rows], colWidths=widths)
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 2)]))
    return t


def grid(data, widths, header=True, zebra=True, extra=()):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.4, RULE), ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), NAVY)]
    if zebra:
        style += [("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, PAPER])]
    t.setStyle(TableStyle(style + list(extra)))
    return t


def th(text):
    return Paragraph("<font color='white'><b>%s</b></font>" % text, S["cell"])


def chip(label, color, width=52):
    t = Table([[P(label, "chip")]], colWidths=[width], rowHeights=[13])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), color), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1)]))
    return t


# --------------------------------------------------------------------------- drawings
def severity_chart(counts, width=W * 0.62):
    d = Drawing(width, 16 * len(SEV_ORDER) + 4)
    top = max(max(counts.values()), 1)
    for i, name in enumerate(SEV_ORDER):
        y = 16 * (len(SEV_ORDER) - 1 - i) + 4
        d.add(String(0, y + 3, name, fontName="Helvetica", fontSize=8, fillColor=INK))
        length = (width - 80) * counts.get(name, 0) / top
        d.add(Rect(52, y, max(length, 0.5), 11, fillColor=SEV[name], strokeColor=None))
        d.add(String(56 + length, y + 3, str(counts.get(name, 0)), fontName="Helvetica-Bold", fontSize=8, fillColor=INK))
    return d


def arrow(d, x1, y1, x2, y2, color=MUTED):
    import math
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=1))
    ang = math.atan2(y2 - y1, x2 - x1)
    tip = [x2, y2, x2 - 6 * math.cos(ang - 0.4), y2 - 6 * math.sin(ang - 0.4), x2 - 6 * math.cos(ang + 0.4), y2 - 6 * math.sin(ang + 0.4)]
    d.add(Polygon(tip, fillColor=color, strokeColor=color, strokeWidth=0.5))


def arch_diagram(arch):
    nodes = {n[0]: n for n in arch["nodes"]}
    rows = max(n[3] for n in arch["nodes"]) + 1
    bw, bh, gx, gy = 100, 32, 187, 54
    height = rows * gy + 34
    d = Drawing(W, height)
    pos = {k: (8 + n[2] * gx, height - 40 - n[3] * gy) for k, n in nodes.items()}
    zone_fill = {"ext": colors.HexColor("#fef2f2"), "dmz": colors.HexColor("#fffbeb"), "int": colors.HexColor("#f0fdf4")}
    for zone, label in arch["zones"].items():
        members = [pos[k] for k, n in nodes.items() if n[4] == zone]
        if not members:
            continue
        x0, x1 = min(m[0] for m in members) - 10, max(m[0] for m in members) + bw + 10
        y0, y1 = min(m[1] for m in members) - 10, max(m[1] for m in members) + bh + 16
        d.add(Rect(x0, y0, x1 - x0, y1 - y0, fillColor=zone_fill[zone], strokeColor=MUTED, strokeDashArray=[3, 2], strokeWidth=0.7))
        d.add(String(x0 + 4, y1 - 9, label.upper(), fontName="Helvetica-Bold", fontSize=6, fillColor=MUTED))
    for a, b, label in arch["edges"]:
        (ax, ay), (bx, by) = pos[a], pos[b]
        sx, sy, tx, ty = (ax + bw, ay + bh / 2, bx, by + bh / 2) if bx > ax else ((ax + bw / 2, ay, bx + bw / 2, by + bh) if by < ay else (ax + bw / 2, ay + bh, bx + bw / 2, by))
        arrow(d, sx, sy, tx, ty)
        mx, my = (sx + tx) / 2, (sy + ty) / 2
        d.add(Rect(mx - 3 - len(label) * 1.7, my - 1, len(label) * 3.4 + 6, 9, fillColor=colors.white, strokeColor=None))
        d.add(String(mx, my + 1, label, fontName="Helvetica", fontSize=6, fillColor=INK, textAnchor="middle"))
    for key, n in nodes.items():
        x, y = pos[key]
        hostile = key == "attacker"
        d.add(Rect(x, y, bw, bh, rx=4, ry=4, fillColor=colors.white, strokeColor=BAD if hostile else NAVY, strokeWidth=1.2))
        d.add(String(x + bw / 2, y + bh / 2 - 3, n[1], fontName="Helvetica-Bold", fontSize=7, fillColor=BAD if hostile else NAVY, textAnchor="middle"))
    return d


def flow_diagram(labels, status_colors=None, box=None):
    n = len(labels)
    gap = 4
    bw = (W - gap * (n - 1)) / n if box is None else box
    d = Drawing(W, 40)
    for i, label in enumerate(labels):
        x = i * (bw + gap)
        fill = (status_colors or [NAVY] * n)[i]
        d.add(Rect(x, 6, bw, 28, rx=3, ry=3, fillColor=fill, strokeColor=None))
        words = label.split(" ")
        lines = [" ".join(words[:len(words) // 2 + len(words) % 2]), " ".join(words[len(words) // 2 + len(words) % 2:])] if len(words) > 1 else [label]
        for j, line in enumerate(lines):
            d.add(String(x + bw / 2, 21 - j * 8 + (4 if len(lines) == 1 else 0) - 4 + 4 * (len(lines) - 1), line, fontName="Helvetica-Bold", fontSize=6.4, fillColor=colors.white, textAnchor="middle"))
        if i < n - 1:
            d.add(Polygon([x + bw + 0.5, 17, x + bw + gap - 0.5, 20, x + bw + 0.5, 23], fillColor=MUTED, strokeColor=None))
    return d


def stacked_chart(rows, width=W, bar=14):
    d = Drawing(width, (bar + 10) * len(rows) + 8)
    top = max(sum(c.values()) for _, c in rows) or 1
    for i, (name, counts) in enumerate(rows):
        y = (bar + 10) * (len(rows) - 1 - i) + 6
        d.add(String(0, y + 4, name, fontName="Helvetica", fontSize=8, fillColor=INK))
        x = 130
        for sev in SEV_ORDER:
            w = (width - 175) * counts.get(sev, 0) / top
            if w:
                d.add(Rect(x, y, w, bar, fillColor=SEV[sev], strokeColor=colors.white, strokeWidth=0.5))
                d.add(String(x + w / 2, y + 4, str(counts[sev]), fontName="Helvetica-Bold", fontSize=7, fillColor=colors.white, textAnchor="middle"))
                x += w
        d.add(String(x + 5, y + 4, str(sum(counts.values())), fontName="Helvetica-Bold", fontSize=8, fillColor=INK))
    return d


# --------------------------------------------------------------------------- data helpers
def load_pipeline():
    path = ROOT / "pipeline" / "sample-run" / "pipeline-run.json"
    if not path.exists():
        sys.exit("Run pipeline/run_pipeline.py first: %s is missing." % path)
    return json.loads(path.read_text())


def enrich(slug, app):
    """Score each finding with CVSS and resolve its code location."""
    out = []
    for f in app["findings"]:
        f = dict(f)
        f["score"] = base_score("CVSS:3.1/" + f["vector"]) if f["vector"] else None
        f["severity"] = cvss_severity(f["score"])
        f["excerpt"] = excerpt(slug, f)
        out.append(f)
    return out


def excerpt(slug, finding):
    name, needle = finding["loc"]
    lines = (ROOT / "apps" / slug / name).read_text().splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            break
    else:
        raise SystemExit("%s: needle %r not found in %s" % (finding["id"], needle, name))
    first, last = max(i - 1, 0), min(i + 1, len(lines) - 1)
    redact = any(r.startswith("SEC-") for r in finding["rules"])
    rows = []
    for n in range(first, last + 1):
        text = re.sub(r'(["\'])[^"\']{6,}\1', r'\1********\1', lines[n]) if redact else lines[n]
        rows.append("%3d | %s" % (n + 1, text[:96]))
    return {"file": name, "line": i + 1, "text": "\n".join(rows)}


def counts_of(findings):
    c = Counter(f["severity"] for f in findings)
    return {s: c.get(s, 0) for s in SEV_ORDER}


STATUS_COLOR = {"pass": OK, "warn": WARN, "fail": BAD, "skipped": MUTED}


def pipeline_app(report, slug):
    return next(a for a in report["apps"] if a["slug"] == slug)


# --------------------------------------------------------------------------- review report
def build_review(slug, report):
    app = C.APPS[slug]
    meta = pipeline_app(report, slug)
    findings = enrich(slug, app)
    counts = counts_of(findings)
    name = meta["name"]
    path = OUT / ("%s-%s-security-review.pdf" % (app["number"], slug))
    doc = make_doc(path, "Application Security Review: %s" % name, app["ticket"], "Demo application security review")
    d = app["dates"]
    story = cover("Application Security Review", name, "Version %s  |  %s" % (app["version"], app["short"]),
                  banner=(app["decision"], app["decision_note"]), banner_color=DECISION[app["decision"]],
                  meta=[("Review ticket", app["ticket"]), ("Application tier", meta["tier"]), ("Data classification", meta["data_classification"]),
                        ("Product owner", app["product_owner"]), ("Technical lead", app["tech_lead"]), ("Hosting", app["hosting"]), ("Technology", app["stack"]),
                        ("Review window", "Requested %s; testing %s; report issued %s" % (d["requested"], d["testing"], d["report"])), ("Reviewer", C.REVIEWER),
                        ("Method", "Threat modelling, automated pipeline (secrets, SAST, SCA, IaC, container, DAST), manual code review and authenticated testing; OWASP ASVS 4.0.3 and OWASP Top 10 (2021).")])
    story += [Spacer(1, 10), P("This is a demonstration document. The application, company, people and findings are fictional; the code excerpts and pipeline results are real output from the demo apps in this repository.", "muted"), PageBreak()]

    # executive summary
    story += [P("1. Executive summary", "h1")] + [P(t) for t in app["summary"]]
    tiles = Table([[chip(s, SEV[s], 56) for s in SEV_ORDER], [P("<b>%d</b>" % counts[s], "chip") for s in SEV_ORDER]], colWidths=[W / 5] * 5)
    for i, s in enumerate(SEV_ORDER):
        tiles._cellvalues[1][i] = Paragraph("<font size=18 color='#%s'><b>%d</b></font>" % (SEV[s].hexval()[2:], counts[s]), ParagraphStyle("t", alignment=TA_CENTER, leading=22))
    tiles.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("BOX", (0, 0), (-1, -1), 0.5, RULE), ("BACKGROUND", (0, 0), (-1, -1), PAPER), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [Spacer(1, 4), tiles, Spacer(1, 6), P("Findings by severity (CVSS v3.1 base score)", "label")]
    gate = meta["gate"]
    story += [Spacer(1, 4), kv_table([("Top risk drivers", "; ".join(app["risk_drivers"])), ("Pipeline gate", "%s (expected: %s)" % (gate.upper(), meta["expected_gate"].upper())),
                                      ("Decision", "<b>%s</b>. %s" % (app["decision"], app["decision_note"])), ("Re-test", app["retest"])])]
    story += [P("Conditions of approval", "h2")] + [P("- " + c) for c in app["conditions"]]

    # scope & architecture
    story += [PageBreak(), P("2. Scope and approach", "h1"), P("<b>In scope</b>"), *[P("- " + s, "small") for s in app["scope"]], Spacer(1, 4), P("<b>Out of scope</b>"), *[P("- " + s, "small") for s in app["out_of_scope"]]]
    story += [P("3. Architecture and trust boundaries", "h1"), Spacer(1, 8), arch_diagram(app["arch"]), Spacer(1, 4), P(app["dataflow"])]
    story += [P("Good practices observed", "h2")] + [P("- " + s, "small") for s in app["positives"]]

    # threat model
    story += [P("4. Threat model (STRIDE)", "h1"), P("Threats were enumerated per trust boundary. 'Assessment' describes the control state found during the review.", "small"), Spacer(1, 4)]
    rows = [[th("Category"), th("Threat"), th("Existing control"), th("Assessment"), th("Findings")]]
    extra = []
    for i, (cat, threat, ctrl, state, refs) in enumerate(app["stride"], 1):
        rows.append([P(cat, "cellb"), P(threat, "cell"), P(ctrl, "cell"), P(state, "cellb"), P(refs, "cell")])
        extra.append(("TEXTCOLOR", (3, i), (3, i), {"Gap": BAD, "Partial": WARN, "Mitigated": OK}[state]))
    story.append(grid(rows, [W * .15, W * .36, W * .17, W * .12, W * .20]))

    # findings summary
    story += [PageBreak(), P("5. Findings summary", "h1"), severity_chart(counts), Spacer(1, 6)]
    rows = [[th("ID"), th("Finding"), th("Severity"), th("CVSS"), th("Priority"), th("Source"), th("Due")]]
    styles = []
    for i, f in enumerate(findings, 1):
        rows.append([P(f["id"], "cellb"), P(f["title"], "cell"), P(f["severity"], "chip"), P("%.1f" % f["score"] if f["score"] is not None else "n/a", "cell"), P(f["priority"], "cell"), P(f["source"], "cell"), P(f["due"], "cell")])
        styles.append(("BACKGROUND", (2, i), (2, i), SEV[f["severity"]]))
    story += [grid(rows, [W * .09, W * .31, W * .10, W * .07, W * .08, W * .22, W * .13], extra=styles),
              Spacer(1, 4), P("Priority reflects business urgency and can exceed what CVSS alone implies (P0 fix now, P1 before release/go-live, P2 within 30 days, P3 backlog).", "muted")]

    # detailed findings
    story += [PageBreak(), P("6. Detailed findings", "h1")]
    for f in findings:
        head = Table([[P("<font color='white'><b>%s  %s</b></font>" % (f["id"], esc(f["title"])), "cell"), P(f["severity"].upper(), "chip")]], colWidths=[W - 62, 62])
        head.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), NAVY), ("BACKGROUND", (1, 0), (1, 0), SEV[f["severity"]]), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LEFTPADDING", (0, 0), (0, 0), 7)]))
        score = "%.1f  (CVSS:3.1/%s)" % (f["score"], f["vector"]) if f["score"] is not None else "Not scored (informational)"
        body = [("CVSS", score), ("Classification", "%s  |  %s" % (f["cwe"], C.OWASP[f["owasp"]])), ("Priority / owner / due", "%s  |  %s  |  %s" % (f["priority"], f["owner"], f["due"])),
                ("Detected by", f["source"] + ("  (rules: %s)" % ", ".join(f["rules"]) if f["rules"] else "")), ("Description", f["description"]), ("Impact", f["impact"]), ("Recommendation", f["recommendation"])]
        t = Table([[P(k, "label"), P(v, "cell")] for k, v in body], colWidths=[W * 0.19, W * 0.81])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE), ("BOX", (0, 0), (-1, -1), 0.5, RULE), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        ex = f["excerpt"]
        code = Table([[P("Evidence: apps/%s/%s line %d" % (slug, ex["file"], ex["line"]), "label")], [Preformatted(ex["text"], S["code"])]], colWidths=[W])
        code.setStyle(TableStyle([("BACKGROUND", (0, 1), (0, 1), colors.HexColor("#eef1f6")), ("BOX", (0, 0), (-1, -1), 0.5, RULE), ("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story += [KeepTogether([head, t, code]), Spacer(1, 9)]

    # ASVS
    story += [P("7. OWASP ASVS 4.0.3 coverage", "h1"), P("Each chapter was assessed at ASVS level 2 for the controls relevant to this application.", "small"), Spacer(1, 4)]
    rows, styles = [[th("Chapter"), th("Result"), th("Notes")]], []
    for i, ch in enumerate(C.ASVS_CHAPTERS, 1):
        res, note = app["asvs"][ch]
        rows.append([P(ch, "cell"), P(res, "cellb"), P(note, "cell")])
        styles.append(("TEXTCOLOR", (1, i), (1, i), {"Pass": OK, "Partial": WARN, "Fail": BAD, "N/A": MUTED}[res]))
    story.append(grid(rows, [W * .30, W * .12, W * .58], extra=styles))

    # pipeline evidence
    story += [P("8. Automated pipeline evidence", "h1"),
              P("The demo pipeline ran against this application (run %s, commit %s). Policy: %s." % (report["run"]["id"], report["run"]["commit"], report["pipeline"]["policy"]["name"]), "small"), Spacer(1, 4),
              flow_diagram([s["label"] for s in meta["stages"]], [STATUS_COLOR[s["status"]] for s in meta["stages"]]), Spacer(1, 4)]
    rows = [[th("Stage"), th("Result"), th("Findings"), th("What it checks")]]
    for s in meta["stages"]:
        rows.append([P(s["label"], "cellb"), P(s["status"].upper(), "cellb"), P(str(s["findings"]), "cell"), P(s["description"], "cell")])
    story += [grid(rows, [W * .18, W * .12, W * .10, W * .60], extra=[("TEXTCOLOR", (1, i + 1), (1, i + 1), STATUS_COLOR[s["status"]]) for i, s in enumerate(meta["stages"])]),
              Spacer(1, 4), P("<b>Gate: %s.</b> %s" % (meta["gate"].upper(), "; ".join(meta["gate_reasons"]) or "No blocking findings."))]

    # roadmap & sign-off
    rows = [[th("When"), th("Action")]] + [[P(w, "cellb"), P(a, "cell")] for w, a in app["roadmap"]]
    story.append(KeepTogether([P("9. Remediation roadmap", "h1"), grid(rows, [W * .22, W * .78])]))
    story += [P("10. Sign-off", "h1")]
    rows = [[th("Role"), th("Name"), th("Decision / acknowledgement"), th("Date")],
            [P("AppSec reviewer", "cell"), P(C.REVIEWER, "cell"), P("<b>%s</b>" % app["decision"], "cell"), P(d["report"], "cell")],
            [P("Product owner", "cell"), P(app["product_owner"].split(" (")[0], "cell"), P("Acknowledged findings and roadmap", "cell"), P(d["report"], "cell")],
            [P("Technical lead", "cell"), P(app["tech_lead"], "cell"), P("Accepts remediation dates", "cell"), P(d["report"], "cell")],
            [P("Risk acceptance", "cell"), P("None recorded", "cell"), P("No residual risk was formally accepted", "cell"), P("-", "cell")]]
    story += [grid(rows, [W * .18, W * .24, W * .40, W * .18]), P("Appendix: rating scales", "h1"),
              grid([[th("CVSS v3.1 base score"), th("Severity"), th("Priority"), th("Meaning")],
                    [P("9.0 - 10.0", "cell"), P("Critical", "cellb"), P("P0", "cell"), P("Fix immediately; blocks release.", "cell")], [P("7.0 - 8.9", "cell"), P("High", "cellb"), P("P0 / P1", "cell"), P("Fix before release or go-live.", "cell")],
                    [P("4.0 - 6.9", "cell"), P("Medium", "cellb"), P("P1 / P2", "cell"), P("Fix within 30 days (earlier when data is sensitive).", "cell")], [P("0.1 - 3.9", "cell"), P("Low", "cellb"), P("P3", "cell"), P("Backlog.", "cell")],
                    [P("None", "cell"), P("Info", "cellb"), P("P3", "cell"), P("Observation or hardening advice.", "cell")]], [W * .24, W * .14, W * .14, W * .48])]
    doc.build(story, canvasmaker=Numbered)
    return {"slug": slug, "kind": "review", "title": "%s - security review" % name, "file": "pdf/" + path.name, "decision": app["decision"], "counts": counts, "total": len(findings), "ticket": app["ticket"]}, findings


# --------------------------------------------------------------------------- intake
def build_intake(slug=None):
    app = C.APPS[slug] if slug else None
    meta = json.loads((ROOT / "apps" / slug / "appsec.json").read_text()) if slug else None
    path = OUT / (("%s-%s-intake.pdf" % (app["number"], slug)) if slug else "00-intake-questionnaire-blank.pdf")
    title = "AppSec Intake Questionnaire" + (": %s" % meta["name"] if slug else " (blank template)")
    doc = make_doc(path, title, app["ticket"] if slug else "Template v1.0", "Application security review intake")
    story = cover("Application security review", "Intake Questionnaire", meta["name"] if slug else "Blank template for new review requests",
                  banner=("Submitted", "Ticket %s, received %s" % (app["ticket"], app["dates"]["requested"])) if slug else ("Template", "Complete every section and attach an architecture diagram."),
                  banner_color=colors.HexColor('#0f766e') if slug else MUTED)
    story += [P("Complete this form to request an application security review. The AppSec team uses it to set the review depth and to schedule testing. " +
                ("Answers below were completed by the requester (shown in blue italics)." if slug else "Write in the shaded boxes."), "body")]
    for heading, questions in C.INTAKE_SECTIONS:
        rows = []
        for qid, question in questions:
            answer = Paragraph(esc(app["intake"][qid]), S["answer"]) if slug else Spacer(1, 22)
            rows.append([P("<b>%s</b> %s" % (qid.upper(), question), "cell"), answer])
        t = Table(rows, colWidths=[W * .42, W * .58])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, RULE), ("BACKGROUND", (0, 0), (0, -1), PAPER), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        story += [KeepTogether([P(heading, "h2"), t])]
    if slug:
        review_depth = {"Tier 1 (critical)": "Full review: threat model, manual code review, authenticated testing", "Tier 2 (important)": "Standard review: threat model, targeted code review, pipeline results", "Tier 3 (standard)": "Light review: pipeline results and design check"}[meta["tier"]]
        story += [P("AppSec triage (completed by reviewer)", "h2"), kv_table([("Assigned tier", meta["tier"]), ("Review depth", review_depth), ("Triaged by", C.REVIEWER), ("Report ticket", app["ticket"])])]
    doc.build(story, canvasmaker=Numbered)
    return {"slug": slug or "template", "kind": "intake", "title": title, "file": "pdf/" + path.name}


# --------------------------------------------------------------------------- portfolio
def build_portfolio(results, report):
    path = OUT / "00-portfolio-summary.pdf"
    doc = make_doc(path, "Application Security Review: Portfolio Summary", "Q3 2026", "Portfolio summary")
    all_findings = [(slug, f) for slug, (_, fs) in results.items() for f in fs]
    totals = counts_of([f for _, f in all_findings])
    manual_only = [(s, f) for s, f in all_findings if not f["rules"]]
    gates_pass = sum(1 for a in report["apps"] if a["gate"] == "pass")
    story = cover("Portfolio summary", "Application Security Review", "Acme Corp  |  Q3 2026  |  3 applications reviewed",
                  banner=("%d of 3 approved" % sum(1 for s in results if C.APPS[s]["decision"] != "Not approved"), "1 blocked, 1 approved with conditions, 1 approved clean"), banner_color=NAVY)
    story += [P("Where we stand", "h1"),
              P("Three applications at different tiers went through the review process this quarter. They were chosen to show the full range of outcomes: one blocked for release, one approved with a condition, and one clean. "
                "<b>%d findings</b> in total (%d Critical, %d High, %d Medium, %d Low, %d Informational). The automated pipeline gate passed %d of 3 applications, matching the review outcome for all three on the Critical and High findings it can see." %
                (sum(totals.values()), totals["Critical"], totals["High"], totals["Medium"], totals["Low"], totals["Info"], gates_pass))]
    rows = [[th("Application"), th("Tier"), th("Decision"), th("Pipeline gate"), th("Findings (C/H/M/L/I)")]]
    styles = []
    for i, (slug, (res, fs)) in enumerate(results.items(), 1):
        meta = pipeline_app(report, slug)
        c = res["counts"]
        rows.append([P("<b>%s</b><br/>%s" % (meta["name"], C.APPS[slug]["ticket"]), "cell"), P(meta["tier"], "cell"), P(res["decision"], "cellb"), P(meta["gate"].upper(), "cellb"), P("%d / %d / %d / %d / %d" % tuple(c[s] for s in SEV_ORDER), "cell")])
        styles += [("TEXTCOLOR", (2, i), (2, i), DECISION[res["decision"]]), ("TEXTCOLOR", (3, i), (3, i), OK if meta["gate"] == "pass" else BAD)]
    story += [grid(rows, [W * .27, W * .17, W * .22, W * .14, W * .20], extra=styles), Spacer(1, 10), P("Findings by application and severity", "h2"),
              stacked_chart([(report_name(report, s), res["counts"]) for s, (res, _) in results.items()]),
              Spacer(1, 4), P("Legend: " + "  ".join("<font color='#%s'><b>%s</b></font>" % (SEV[s].hexval()[2:], s) for s in SEV_ORDER), "small")]

    story += [P("What the pipeline sees and what it does not", "h1"),
              P("%d of %d findings were detected or confirmed by the automated pipeline. The %d findings below were found <b>only by manual review</b>. They cluster in access control, logging and design, which pattern-matching tools cannot judge. The pipeline is the first filter; the review is still needed." %
                (len(all_findings) - len(manual_only), len(all_findings), len(manual_only)))]
    rows = [[th("Finding"), th("Application"), th("Class"), th("Severity")]]
    for slug, f in manual_only:
        rows.append([P("<b>%s</b> %s" % (f["id"], f["title"]), "cell"), P(report_name(report, slug), "cell"), P(C.OWASP[f["owasp"]], "cell"), P(f["severity"], "cellb")])
    story += [grid(rows, [W * .42, W * .20, W * .26, W * .12], extra=[("TEXTCOLOR", (3, i), (3, i), SEV[f["severity"]]) for i, (_, f) in enumerate(manual_only, 1)])]

    themes = Counter(C.OWASP[f["owasp"]] for _, f in all_findings)
    story += [P("Recurring themes", "h1")]
    rows = [[th("OWASP Top 10 (2021) category"), th("Findings")]] + [[P(k, "cell"), P(str(v), "cellb")] for k, v in sorted(themes.items())]
    story += [grid(rows, [W * .8, W * .2])]
    story += [PageBreak(), P("The review process", "h1"),
              flow_diagram(["Intake form", "Triage and tier", "Pipeline scans", "Manual review", "Report", "Remediate", "Re-test", "Sign-off"], [NAVY] * 8), Spacer(1, 6),
              grid([[th("Step"), th("What happens"), th("Owner")],
                    [P("Intake", "cellb"), P("Team submits the questionnaire; AppSec assigns a tier (1 critical, 2 important, 3 standard).", "cell"), P("Product team / AppSec", "cell")],
                    [P("Pipeline", "cellb"), P("Secrets, SAST, dependency, IaC, container, SBOM and DAST run on every push and on request.", "cell"), P("Pipeline (automated)", "cell")],
                    [P("Manual review", "cellb"), P("Threat model, access-control and logic review, authenticated testing. Depth set by tier.", "cell"), P("AppSec", "cell")],
                    [P("Report", "cellb"), P("Findings scored with CVSS, mapped to CWE/OWASP, with owner and due date.", "cell"), P("AppSec", "cell")],
                    [P("Remediate and re-test", "cellb"), P("Fixes verified by re-test; the pipeline gate must pass with no Critical or High waivers.", "cell"), P("Product team / AppSec", "cell")],
                    [P("Sign-off", "cellb"), P("Decision recorded: approved, approved with conditions, or not approved.", "cell"), P("AppSec / product owner", "cell")]], [W * .2, W * .55, W * .25]),
              P("Recommendations", "h1"),
              P("1. Make the pipeline gate a required check on the main branch of every Tier 1 and Tier 2 application."), P("2. Add a two-user authorisation test to every service that exposes objects by id, to catch the IDOR class automatically after the first review."),
              P("3. Centralise error handling and security headers in a shared library; both are repeated findings."), P("4. Enable dependency update automation and store an SBOM per release."), P("5. Move secrets to the platform secrets manager and add pre-commit secret scanning.")]
    doc.build(story, canvasmaker=Numbered)
    return {"slug": "portfolio", "kind": "portfolio", "title": "Portfolio summary", "file": "pdf/" + path.name}


def report_name(report, slug):
    return pipeline_app(report, slug)["name"].replace("Acme ", "")


# --------------------------------------------------------------------------- pipeline report
def build_pipeline_report(report):
    path = OUT / "00-pipeline-run-report.pdf"
    run = report["run"]
    doc = make_doc(path, "Demo AppSec Pipeline: Run Report", "Run " + run["id"], "Pipeline run report")
    story = cover("Pipeline run report", "AppSec Pipeline", "Run %s  |  commit %s  |  %s" % (run["id"], run["commit"], run["finished"]),
                  banner=("%d passed, %d blocked" % (sum(a["gate"] == "pass" for a in report["apps"]), sum(a["gate"] == "fail" for a in report["apps"])), "Policy: " + report["pipeline"]["policy"]["name"]), banner_color=NAVY)
    story += [P("Release policy", "h2"), P(esc(report["pipeline"]["policy"]["notes"])), P("Stages", "h2"),
              grid([[th("Stage"), th("What it does"), th("Production equivalent")]] + [[P(s["label"], "cellb"), P(s["description"].split(" (")[0], "cell"), P(s["description"].split(" (")[1].rstrip(")") if " (" in s["description"] else "-", "cell")] for s in report["apps"][0]["stages"]],
                   [W * .2, W * .5, W * .3])]
    for app in report["apps"]:
        c = app["counts"]
        story += [PageBreak(), P(app["name"], "h1"), flow_diagram([s["label"] for s in app["stages"]], [STATUS_COLOR[s["status"]] for s in app["stages"]]), Spacer(1, 6),
                  P("<b>Gate: <font color='#%s'>%s</font></b>. %s" % ((OK if app["gate"] == "pass" else BAD).hexval()[2:], app["gate"].upper(), esc("; ".join(app["gate_reasons"]) or "No blocking findings.")), "body"),
                  P("Open findings: %d critical, %d high, %d medium, %d low. Duration %d ms across %d stages." % (c["critical"], c["high"], c["medium"], c["low"], sum(s["duration_ms"] for s in app["stages"]), len(app["stages"])), "small"), Spacer(1, 6)]
        rows = [[th("Severity"), th("Stage"), th("Rule"), th("Location"), th("Detail")]]
        styles = []
        for i, f in enumerate(app["findings"], 1):
            rows.append([P(f["severity"].upper(), "chip"), P(f["stage"], "cell"), P(f["rule"], "cell"), P(esc("%s%s" % (f["file"], ":%d" % f["line"] if f["line"] else "")), "cell"), P(esc(f["detail"] or "-"), "cell")])
            styles.append(("BACKGROUND", (0, i), (0, i), SEV[f["severity"].capitalize()]))
        story += [grid(rows, [W * .11, W * .09, W * .25, W * .19, W * .36], extra=styles) if app["findings"] else P("No findings in any stage.")]
    doc.build(story, canvasmaker=Numbered)
    return {"slug": "pipeline-run", "kind": "pipeline", "title": "Pipeline run report", "file": "pdf/" + path.name}


def main():
    OUT.mkdir(exist_ok=True)
    report = load_pipeline()
    results = {}
    docs = [build_intake(None)]
    for slug in sorted(C.APPS):
        res, findings = build_review(slug, report)
        results[slug] = (res, findings)
        docs += [build_intake(slug), res]
    docs = [build_portfolio(results, report), build_pipeline_report(report)] + docs
    index = {"generated_from_run": report["run"]["id"], "documents": docs,
             "apps": [{"slug": s, "name": pipeline_app(report, s)["name"], "short": C.APPS[s]["short"], "tier": pipeline_app(report, s)["tier"], "decision": r["decision"], "gate": pipeline_app(report, s)["gate"],
                       "counts": r["counts"], "total": r["total"], "ticket": r["ticket"]} for s, (r, _) in results.items()]}
    (OUT / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    for d in docs:
        print("wrote", d["file"])


if __name__ == "__main__":
    main()
