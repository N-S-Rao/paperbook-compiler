"""
Paper-book compilation engine (v2 — matches the user's actual filed Master
Index format: cause title, MASTER INDEX with continuous S.No and inline
VOLUME dividers, then per-volume INDEX sections with S.No restarting at 1,
each followed by a filing/verification block. Items may split across a
volume boundary — the continuation is labelled "(Cont.)").
"""
import os
import io
import math
import shutil
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4, LEGAL
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdfcanvas

FORUM_CAPS = {"NCLAT": 190, "NCLT": 150}
MAX_ITERATIONS = 12


# ---------------------------------------------------------------- data model

@dataclass
class Item:
    """A single document going into the paper book — a pleading, IA,
    memo, or annexure. If `heading` is empty the particulars text is
    shown plain (e.g. 'Memo of Parties'); if set, heading is bold and
    particulars is the description below it (e.g. 'ANNEXURE A1' / 'Certified
    copy of ...')."""
    id: str
    heading: str
    particulars: str
    filepath: str
    order: int
    page_count: int = 0


@dataclass
class Chunk:
    item: Item
    part_index: int         # 0 = first/only part of this item
    src_start: int           # 0-indexed inclusive, page range within item's own PDF
    src_end: int
    volume_no: int = 0
    physical_start_in_volume: int = 0   # 1-indexed physical page within the assembled volume PDF (incl. index pages) — for bookmarks
    content_start_in_volume: int = 0    # 1-indexed among CONTENT pages only, restarts at 1 each volume — used when numbering restarts per volume
    content_start_global: int = 0       # 1-indexed among CONTENT pages only, across the whole book — used when numbering is continuous

    @property
    def page_count(self) -> int:
        return self.src_end - self.src_start + 1

    @property
    def is_continuation(self) -> bool:
        return self.part_index > 0

    @property
    def display_heading(self) -> Optional[str]:
        if not self.item.heading:
            return None
        return self.item.heading + (" (Cont.)" if self.is_continuation else "")

    @property
    def display_particulars(self) -> str:
        if not self.item.heading:
            return self.item.particulars + (" (Cont.)" if self.is_continuation else "")
        return self.item.particulars


@dataclass
class Volume:
    number: int
    chunks: List[Chunk] = field(default_factory=list)
    index_pages: int = 1
    total_pages: int = 0


def load_items(manifest: List[dict]) -> List[Item]:
    items = []
    for row in sorted(manifest, key=lambda r: r["order"]):
        pages = PdfReader(row["filepath"]).get_num_pages()
        items.append(Item(
            id=row["id"], heading=row.get("heading", "") or "",
            particulars=row.get("particulars", ""), filepath=row["filepath"],
            order=row["order"], page_count=pages,
        ))
    return items


# ---------------------------------------------------------------- packing

def _pack_chunks(items: List[Item], cap: int, index_estimates: List[int],
                  master_reserve_first_volume: int) -> Tuple[List[Chunk], int]:
    """Page-level bin-pack, balanced. First finds the minimum number of
    volumes k that can hold everything under the cap, then evenly divides
    the content across those k volumes (each within a page or two of the
    others) rather than greedily filling each volume to the cap — greedy
    fill routinely leaves a near-empty last volume (e.g. 1 page), which is
    not acceptable. Splits an item's pages across a volume boundary when a
    target is reached — never refuses to place anything."""
    total_content = sum(it.page_count for it in items)

    def capacity_for(v: int) -> int:
        est = index_estimates[v - 1] if v - 1 < len(index_estimates) else (index_estimates[-1] if index_estimates else 1)
        reserve = est + (master_reserve_first_volume if v == 1 else 0)
        return max(cap - reserve, 1)

    if total_content == 0:
        return [], 1

    k = max(1, math.ceil(total_content / cap))
    while True:
        capacities = [capacity_for(v) for v in range(1, k + 1)]
        if sum(capacities) >= total_content:
            break
        k += 1

    # Evenly divide total_content across k volumes (differ by at most 1 page),
    # then repair any volume whose target exceeds its own capacity (only
    # volume 1 is typically smaller, due to the Master Index reserve) by
    # pushing the overflow forward into the next volume's target.
    base = total_content // k
    rem = total_content % k
    targets = [base + (1 if i < rem else 0) for i in range(k)]
    carry = 0
    for i in range(k):
        targets[i] += carry
        carry = 0
        if targets[i] > capacities[i]:
            carry = targets[i] - capacities[i]
            targets[i] = capacities[i]
    if carry > 0:
        targets[-1] += carry  # true pathological overflow — QC will flag it downstream

    chunks: List[Chunk] = []
    vol_no = 1
    remaining_in_vol = targets[0]
    for item in items:
        remaining = item.page_count
        src = 0
        part = 0
        if remaining == 0:
            continue
        while remaining > 0:
            if remaining_in_vol <= 0:
                vol_no += 1
                remaining_in_vol = targets[vol_no - 1] if vol_no - 1 < len(targets) else capacities[-1]
            take = min(remaining, remaining_in_vol) or 1
            chunks.append(Chunk(item=item, part_index=part, src_start=src, src_end=src + take - 1, volume_no=vol_no))
            remaining_in_vol -= take
            remaining -= take
            src += take
            part += 1
    return chunks, vol_no


# ---------------------------------------------------------------- rendering

def _styles():
    base = getSampleStyleSheet()
    return {
        "cause_bold": ParagraphStyle("cause_bold", parent=base["Normal"], fontName="Times-Bold",
                                      fontSize=12, leading=16, alignment=TA_CENTER),
        "cause_plain": ParagraphStyle("cause_plain", parent=base["Normal"], fontName="Times-Roman",
                                       fontSize=12, leading=16, alignment=TA_LEFT),
        "party_name": ParagraphStyle("party_name", parent=base["Normal"], fontName="Times-Roman",
                                      fontSize=12, leading=16, alignment=TA_LEFT),
        "versus": ParagraphStyle("versus", parent=base["Normal"], fontName="Times-BoldItalic",
                                  fontSize=12, leading=16, alignment=TA_CENTER),
        "designation": ParagraphStyle("designation", parent=base["Normal"], fontName="Times-Roman",
                                       fontSize=12, leading=16, alignment=TA_RIGHT),
        "row_plain": ParagraphStyle("row_plain", parent=base["Normal"], fontName="Times-Roman",
                                     fontSize=11, leading=14.5),
        "divider": ParagraphStyle("divider", parent=base["Normal"], fontName="Times-Bold",
                                   fontSize=12, leading=16, alignment=TA_CENTER),
        "section_title": ParagraphStyle("section_title", parent=base["Normal"], fontName="Times-Bold",
                                         fontSize=13, leading=17, alignment=TA_CENTER),
        "filing_center": ParagraphStyle("filing_center", parent=base["Normal"], fontName="Times-Roman",
                                         fontSize=11, leading=15, alignment=TA_CENTER),
        "filing_center_bold": ParagraphStyle("filing_center_bold", parent=base["Normal"], fontName="Times-Bold",
                                              fontSize=11, leading=15, alignment=TA_CENTER),
        "filing_email": ParagraphStyle("filing_email", parent=base["Normal"], fontName="Times-Roman",
                                        fontSize=11, leading=15, alignment=TA_CENTER, textColor=colors.blue,
                                        underlineWidth=0.5),
        "filing_left": ParagraphStyle("filing_left", parent=base["Normal"], fontName="Times-Roman",
                                       fontSize=11, leading=15, alignment=TA_LEFT),
    }


def _cause_title_flowable(case_info: dict, styles):
    cause_lines = case_info.get("cause_lines", [])
    matter_heading = case_info.get("matter_heading", "IN THE MATTER OF:")
    parties = case_info.get("parties", [])

    flow = []
    for line in cause_lines:
        flow.append(Paragraph(line, styles["cause_bold"]))
    flow.append(Spacer(1, 4))
    flow.append(Paragraph(matter_heading, styles["cause_plain"]))
    flow.append(Spacer(1, 6))

    rows = []
    for i, (name, desig) in enumerate(parties):
        if i > 0:
            rows.append([Paragraph("Versus", styles["versus"]), ""])
        rows.append([Paragraph(name, styles["party_name"]), Paragraph(desig, styles["designation"])])
    if rows:
        tbl = Table(rows, colWidths=[130 * mm, 40 * mm])
        style_cmds = [
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]
        span_idx = []
        rr = 0
        for i in range(len(parties)):
            if i > 0:
                span_idx.append(rr)
                rr += 1
            rr += 1
        for sr in span_idx:
            style_cmds.append(("SPAN", (0, sr), (1, sr)))
        tbl.setStyle(TableStyle(style_cmds))
        flow.append(tbl)
    return flow


def _signature_image_flowable(path: str, max_w: float = 45 * mm, max_h: float = 20 * mm):
    try:
        ir = ImageReader(path)
        iw, ih = ir.getSize()
        scale = min(max_w / iw, max_h / ih)
        img = Image(path, width=iw * scale, height=ih * scale)
        img.hAlign = "CENTER"
        return img
    except Exception:
        return None


def _filing_block_flowable(case_info: dict, styles):
    fb = case_info.get("filing_block", {})
    filed_on = fb.get("filed_on", "")
    place = fb.get("place", "")
    through_heading = fb.get("through_heading", "THROUGH")
    counsel_lines = fb.get("counsel_lines", [])
    signature_path = fb.get("signature_path")

    left_cell = [
        Paragraph(f"Filed on: {filed_on}", styles["filing_left"]),
        Spacer(1, 14),
        Paragraph(f"Place: {place}", styles["filing_left"]),
    ]

    right_cell = []
    if signature_path and os.path.exists(signature_path):
        sig_img = _signature_image_flowable(signature_path)
        if sig_img is not None:
            right_cell.append(sig_img)
            right_cell.append(Spacer(1, 8))
    right_cell.append(Paragraph(f"<b>{through_heading}</b>", styles["filing_center_bold"]))
    for i, line in enumerate(counsel_lines):
        is_email = "@" in line
        if is_email:
            right_cell.append(Paragraph(f"<u>{line}</u>", styles["filing_email"]))
        elif i == 0:
            right_cell.append(Paragraph(f"<b>{line}</b>", styles["filing_center_bold"]))
        else:
            right_cell.append(Paragraph(line, styles["filing_center"]))

    tbl = Table([[left_cell, right_cell]], colWidths=[55 * mm, 115 * mm])
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (0, 0), "BOTTOM"),
        ("VALIGN", (1, 0), (1, 0), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return tbl


def _render_index_pdf(out_path: str, case_info: dict, section_title_lines: List[str],
                       rows: List[Tuple[Optional[str], str, str]],
                       dividers: dict, sno_start: int = 1,
                       col_widths=(14 * mm, 122 * mm, 32 * mm)) -> int:
    """rows: list of (heading_or_None, particulars, page_str).
    dividers: {row_index (0-based, position in `rows`): "VOLUME N"} — inserted
    as a full-width row immediately before that row."""
    styles = _styles()
    doc = SimpleDocTemplate(out_path, pagesize=LEGAL,
                             topMargin=18 * mm, bottomMargin=16 * mm,
                             leftMargin=30 * mm, rightMargin=15 * mm)
    story = list(_cause_title_flowable(case_info, styles)) + [Spacer(1, 10)]
    for line in section_title_lines:
        story.append(Paragraph(line, styles["section_title"]))
    story.append(Spacer(1, 8))

    header = ["S.No.", "Particulars", "Pages"]
    data = [header]
    span_cmds = []
    row_i = 1  # data row index (0 = header)
    sno = sno_start
    for i, (heading, particulars, page_str) in enumerate(rows):
        if i in dividers:
            data.append([Paragraph(dividers[i], styles["divider"]), "", ""])
            span_cmds.append(("SPAN", (0, row_i), (-1, row_i)))
            row_i += 1
        if heading:
            cell = Paragraph(f"<b>{heading}</b><br/><br/>{particulars}" if particulars else f"<b>{heading}</b>",
                              styles["row_plain"])
        else:
            cell = Paragraph(particulars, styles["row_plain"])
        data.append([str(sno) + ".", cell, page_str])
        sno += 1
        row_i += 1

    header_style = ParagraphStyle("hdr", parent=styles["row_plain"], alignment=TA_CENTER)
    data[0] = [Paragraph(h, header_style) for h in header]

    table = Table(data, colWidths=list(col_widths), repeatRows=1)
    style_cmds = [
        ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ] + span_cmds
    table.setStyle(TableStyle(style_cmds))
    story.append(table)
    # Keep the filing block glued to the table rather than letting it fall
    # onto its own page — only spills over if it genuinely can't fit at all.
    story.append(KeepTogether([Spacer(1, 10), _filing_block_flowable(case_info, styles)]))

    doc.build(story)
    return PdfReader(out_path).get_num_pages()


def _stamp_page_numbers(reader: PdfReader, writer: PdfWriter, start_number: int, skip_count: int = 0):
    """Top-centre page number, Times New Roman Bold 28pt, boxed in a square —
    matches the court's stamping convention. The first `skip_count` pages
    (Master Index / Volume Index front matter) are left unnumbered, exactly
    like the sample filing — page 1 is the first page of actual content."""
    for i, page in enumerate(reader.pages):
        if i < skip_count:
            writer.add_page(page)
            continue
        content_i = i - skip_count
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        text = f"{start_number + content_i}"
        font_name, font_size = "Times-Bold", 28
        buf = io.BytesIO()
        c = pdfcanvas.Canvas(buf, pagesize=(w, h))
        c.setFont(font_name, font_size)
        text_width = c.stringWidth(text, font_name, font_size)
        box_h = font_size * 1.35
        box_w = max(text_width + 16, box_h)  # keep it square-ish even for 1-2 digit numbers
        center_y = h - 20 * mm
        box_x = w / 2 - box_w / 2
        box_y = center_y - box_h / 2
        c.setLineWidth(1.2)
        c.rect(box_x, box_y, box_w, box_h, stroke=1, fill=0)
        c.drawCentredString(w / 2, box_y + box_h * 0.28, text)
        c.save()
        buf.seek(0)
        overlay_page = PdfReader(buf).pages[0]
        page.merge_page(overlay_page)
        writer.add_page(page)


MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # NCLAT efiling per-file upload cap


def _compress_pdf_if_needed(path: str, limit_bytes: int = MAX_FILE_SIZE_BYTES) -> dict:
    """If the assembled PDF exceeds limit_bytes (the NCLAT efiling upload
    cap), downsamples and recompresses its embedded images in place until
    it's under the limit or further compression stops helping. Vector
    content — the page-number stamp, bookmarks, table/index text — is
    untouched; only raster images (scanned annexure pages) are affected.
    Returns a report dict; never raises — compression is best-effort and a
    failure here should not break the rest of the pipeline."""
    orig_size = os.path.getsize(path)
    if orig_size <= limit_bytes:
        return {"attempted": False, "orig_mb": orig_size / 1e6, "final_mb": orig_size / 1e6, "under_limit": True}

    try:
        import fitz  # PyMuPDF
        from PIL import Image as PILImage
    except Exception as e:
        return {"attempted": False, "orig_mb": orig_size / 1e6, "final_mb": orig_size / 1e6,
                "under_limit": False, "error": f"compression libraries unavailable ({e})"}

    backup_path = path + ".precompress"
    shutil.copyfile(path, backup_path)

    # Progressively more aggressive passes; each pass starts fresh from the
    # untouched backup so quality loss never compounds across attempts.
    steps = [(78, 2000), (68, 1700), (58, 1450), (48, 1250), (38, 1050), (30, 900), (24, 750)]
    final_size = orig_size
    success = False
    try:
        for quality, max_dim in steps:
            doc = fitz.open(backup_path)
            for page in doc:
                for img in page.get_images(full=True):
                    xref = img[0]
                    try:
                        base = doc.extract_image(xref)
                        pil = PILImage.open(io.BytesIO(base["image"]))
                        if pil.mode not in ("RGB", "L"):
                            pil = pil.convert("RGB")
                        w, h = pil.size
                        scale = min(1.0, max_dim / max(w, h))
                        if scale < 1.0:
                            pil = pil.resize((max(1, int(w * scale)), max(1, int(h * scale))), PILImage.LANCZOS)
                        buf = io.BytesIO()
                        pil.save(buf, format="JPEG", quality=quality, optimize=True)
                        new_bytes = buf.getvalue()
                        if len(new_bytes) < len(base["image"]):
                            page.replace_image(xref, stream=new_bytes)
                    except Exception:
                        continue  # skip any image that can't be processed; leave it as-is
            tmp_path = path + ".tmp"
            doc.save(tmp_path, garbage=4, deflate=True, clean=True)
            doc.close()
            final_size = os.path.getsize(tmp_path)
            os.replace(tmp_path, path)
            if final_size <= limit_bytes:
                success = True
                break
    finally:
        if os.path.exists(backup_path):
            os.remove(backup_path)

    return {"attempted": True, "orig_mb": orig_size / 1e6, "final_mb": final_size / 1e6, "under_limit": success}


# ---------------------------------------------------------------- orchestration

def build_paper_book(case_info: dict, manifest: List[dict], forum: str,
                      out_dir: str, signature_path: Optional[str] = None,
                      continuous_numbering: bool = True) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    cap = FORUM_CAPS[forum]
    if signature_path:
        case_info = dict(case_info)
        case_info["filing_block"] = dict(case_info.get("filing_block", {}))
        case_info["filing_block"]["signature_path"] = signature_path
    items = load_items(manifest)

    index_estimates = [1]
    master_estimate = 1
    volumes_map = {}

    for _ in range(MAX_ITERATIONS):
        chunks, n_vols = _pack_chunks(items, cap, index_estimates, master_estimate)
        while len(index_estimates) < n_vols:
            index_estimates.append(index_estimates[-1])

        volumes_map = {}
        for ch in chunks:
            volumes_map.setdefault(ch.volume_no, Volume(number=ch.volume_no)).chunks.append(ch)

        new_index_estimates = list(index_estimates)
        for v in volumes_map.values():
            rows = [(c.display_heading, c.display_particulars, "") for c in v.chunks]
            tmp_path = os.path.join(out_dir, f"_tmp_idx_v{v.number}.pdf")
            pages = _render_index_pdf(tmp_path, case_info,
                                       ["INDEX", f"VOLUME {v.number}"], rows, dividers={}, sno_start=1)
            new_index_estimates[v.number - 1] = pages
            os.remove(tmp_path)

        ordered = []
        for vno in sorted(volumes_map.keys()):
            ordered.extend(volumes_map[vno].chunks)
        master_rows = [(c.display_heading, c.display_particulars, "") for c in ordered]
        master_dividers = {}
        seen_vol = None
        for i, c in enumerate(ordered):
            if c.volume_no != seen_vol:
                master_dividers[i] = f"VOLUME {c.volume_no}"
                seen_vol = c.volume_no
        tmp_master = os.path.join(out_dir, "_tmp_master.pdf")
        new_master_estimate = _render_index_pdf(tmp_master, case_info, ["MASTER INDEX"],
                                                  master_rows, master_dividers, sno_start=1)
        os.remove(tmp_master)

        if new_index_estimates == index_estimates and new_master_estimate == master_estimate:
            index_estimates = new_index_estimates
            master_estimate = new_master_estimate
            break
        index_estimates = new_index_estimates
        master_estimate = new_master_estimate

    # ---- finalize page numbers ----
    # Page numbers count CONTENT pages only — Master Index / Volume Index
    # pages are unnumbered front matter, so "page 1" is the first page of
    # actual content (e.g. Memo of Parties), matching the sample exactly.
    volumes = [volumes_map[k] for k in sorted(volumes_map.keys())]
    running_global_content = 1
    for v in volumes:
        idx_pages = index_estimates[v.number - 1] if v.number - 1 < len(index_estimates) else index_estimates[-1]
        v.index_pages = idx_pages
        master_here = master_estimate if v.number == 1 else 0
        physical_cursor = master_here + idx_pages + 1
        local_content_cursor = 1
        for c in v.chunks:
            c.physical_start_in_volume = physical_cursor
            c.content_start_in_volume = local_content_cursor
            c.content_start_global = running_global_content
            physical_cursor += c.page_count
            local_content_cursor += c.page_count
            running_global_content += c.page_count
        v.total_pages = physical_cursor - 1

    def page_str(c: Chunk) -> str:
        # Page references always match the number actually stamped on the
        # physical page — global continuous content-page number if
        # continuous_numbering is on, else per-volume-local content number.
        start = c.content_start_global if continuous_numbering else c.content_start_in_volume
        end = start + c.page_count - 1
        return f"{start}" if c.page_count == 1 else f"{start} - {end}"

    ordered = []
    for v in volumes:
        ordered.extend(v.chunks)
    master_rows = [(c.display_heading, c.display_particulars, page_str(c)) for c in ordered]
    master_dividers = {}
    seen_vol = None
    for i, c in enumerate(ordered):
        if c.volume_no != seen_vol:
            master_dividers[i] = f"VOLUME {c.volume_no}"
            seen_vol = c.volume_no
    master_path = os.path.join(out_dir, "Master_Index.pdf")
    master_pages_final = _render_index_pdf(master_path, case_info, ["MASTER INDEX"],
                                            master_rows, master_dividers, sno_start=1)

    qc_lines = []
    volume_files = []
    manual_review = []
    for v in volumes:
        rows = [(c.display_heading, c.display_particulars, page_str(c)) for c in v.chunks]
        index_path = os.path.join(out_dir, f"_final_idx_v{v.number}.pdf")
        idx_pages_final = _render_index_pdf(index_path, case_info,
                                             ["INDEX", f"VOLUME {v.number}"], rows, dividers={}, sno_start=1)

        merged_pages = []
        master_embedded = 0
        if v.number == 1:
            mreader = PdfReader(master_path)
            master_embedded = len(mreader.pages)
            merged_pages.extend(mreader.pages)

        ireader = PdfReader(index_path)
        merged_pages.extend(ireader.pages)

        for c in v.chunks:
            r = PdfReader(c.item.filepath)
            merged_pages.extend(r.pages[c.src_start:c.src_end + 1])

        tmp_writer = PdfWriter()
        for p in merged_pages:
            tmp_writer.add_page(p)
        raw_path = os.path.join(out_dir, f"_raw_v{v.number}.pdf")
        with open(raw_path, "wb") as f:
            tmp_writer.write(f)

        reader = PdfReader(raw_path)
        start_num = (v.chunks[0].content_start_global if continuous_numbering else 1) if v.chunks else 1
        skip_count = master_embedded + idx_pages_final
        stamped = PdfWriter()
        _stamp_page_numbers(reader, stamped, start_num, skip_count=skip_count)

        offset = 0
        if v.number == 1:
            stamped.add_outline_item("MASTER INDEX", 0)
            offset = master_embedded
        stamped.add_outline_item("INDEX", offset)
        for c in v.chunks:
            label = c.display_heading or c.display_particulars[:60]
            stamped.add_outline_item(label, c.physical_start_in_volume - 1)

        out_path = os.path.join(out_dir, f"Volume_{v.number}.pdf")
        with open(out_path, "wb") as f:
            stamped.write(f)
        volume_files.append(out_path)

        compress_report = _compress_pdf_if_needed(out_path)

        for tmp in (index_path, raw_path):
            if os.path.exists(tmp):
                os.remove(tmp)

        expected_total = master_embedded + idx_pages_final + sum(c.page_count for c in v.chunks)
        qc_lines.append(
            f"Volume {v.number}: {v.total_pages} pages (cap {cap}) — "
            f"{'OK' if v.total_pages <= cap else 'EXCEEDS CAP'}"
            + (f"  [Master Index: {master_embedded}p + Volume Index: {idx_pages_final}p]"
               if v.number == 1 else f"  [Volume Index: {idx_pages_final}p]")
        )
        if compress_report["attempted"]:
            qc_lines.append(
                f"Volume {v.number} file size: {compress_report['orig_mb']:.1f}MB -> "
                f"{compress_report['final_mb']:.1f}MB after compression "
                f"(50MB NCLAT efiling cap) — {'OK' if compress_report['under_limit'] else 'STILL OVER LIMIT'}"
            )
            if not compress_report["under_limit"]:
                qc_lines.append(
                    f"FAIL: Volume {v.number} is {compress_report['final_mb']:.1f}MB even after maximum "
                    f"compression — exceeds the 50MB NCLAT efiling upload cap. Consider re-scanning oversized "
                    f"annexures at a lower resolution, or splitting this volume further."
                )
                manual_review.append(("FILE_SIZE_LIMIT", v.number))
        elif "error" in compress_report:
            qc_lines.append(f"Volume {v.number} file size: {compress_report['orig_mb']:.1f}MB, "
                             f"exceeds 50MB cap, but automatic compression could not run "
                             f"({compress_report['error']}) — check file size manually before filing.")
        else:
            qc_lines.append(f"Volume {v.number} file size: {compress_report['orig_mb']:.1f}MB — under the 50MB cap.")
        if expected_total != v.total_pages or idx_pages_final != v.index_pages:
            qc_lines.append(f"FAIL: Volume {v.number} page-count mismatch — planned {v.total_pages}, "
                             f"assembled {expected_total}. Do not file.")
            manual_review.append(("ASSEMBLY_MISMATCH", v.number))

    if master_pages_final != master_estimate:
        qc_lines.append(f"FAIL: Master Index length mismatch — planned {master_estimate}p, "
                         f"final render {master_pages_final}p. Volume I pagination may be off — do not file.")
        manual_review.append(("MASTER_MISMATCH",))

    qc_pass = len(manual_review) == 0
    for v in volumes:
        if v.total_pages > cap:
            qc_pass = False
            qc_lines.append(f"FAIL: Volume {v.number} exceeds the {cap}-page cap.")

    qc_report = os.path.join(out_dir, "qc_report.txt")
    with open(qc_report, "w") as f:
        f.write(f"PAPER-BOOK QC REPORT — {forum} (cap {cap} pages/volume)\n")
        f.write("=" * 60 + "\n")
        f.write("\n".join(qc_lines))
        f.write(f"\n\nOVERALL: {'PASS' if qc_pass else 'FAIL — DO NOT FILE, see failures above'}\n")

    zip_path = os.path.join(out_dir, "paper_book_output.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for vf in volume_files:
            z.write(vf, os.path.basename(vf))
        z.write(master_path, os.path.basename(master_path))
        z.write(qc_report, os.path.basename(qc_report))

    return {
        "qc_pass": qc_pass,
        "qc_report": qc_report,
        "zip_path": zip_path,
        "volumes": [{"number": v.number, "pages": v.total_pages} for v in volumes],
    }
