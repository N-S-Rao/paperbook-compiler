# Paper Book Compiler

A local GUI tool: drop in annexure PDFs, type the particulars for each, click
Generate — get back paginated, bookmarked, signed, volume-split filing PDFs
(Volume_I.pdf, Volume_II.pdf, …) plus a Master_Index.pdf, respecting the
NCLAT (190 pages/volume) or NCLT (150 pages/volume) cap.

## Run it

```bash
cd paperbook
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 in a browser. Runs entirely on your machine —
nothing leaves it.

## How it works

1. **Fill in the cause title** — court/tribunal line, jurisdiction, case
   number, "in the matter of" heading, and the two party blocks (name +
   designation, e.g. "...Appellant" / "...Respondents"). This is rendered
   at the top of the Master Index and every Volume Index, exactly like a
   filed cause title.
2. **Fill in the counsel/filing block** — counsel name, address, email(s),
   phone, plus Date and Place. Rendered as the "Filed on / Place / THROUGH
   / counsel details" block at the foot of every index section. An optional
   signature image can be overlaid near it for a scanned wet signature.
3. **Drop annexures** — drag & drop or browse. Page counts are read
   automatically. For each item, set:
   - **Heading** — the bold heading shown in the index (e.g. `ANNEXURE A1`,
     `IA No. 152 of 2025`). Leave blank for a plain item like "Memo of
     Parties" or "Vakalatnama" — its Particulars text is then shown plain,
     with no bold heading.
   - **Particulars** — the description below the heading (or the whole
     line, if Heading is blank).
   - **Order** — filing sequence.
4. **Set forum & numbering** — NCLAT (190 pages/volume) or NCLT (150), and
   whether page numbers continue across volumes or restart each volume.
5. **Generate** — the engine:
   - packs items into volumes **at the page level**, not per-document — if
     an item doesn't fit in the remaining space of a volume, it's
     physically split there and the continuation is labelled `(Cont.)` in
     both indexes, with its own page range, exactly like a real filed
     Master Index (verified against a real one — annexures spanning 2–3
     volumes render correctly, see engine.py's `_pack_chunks`);
   - solves the index/pagination circularity by iterating: pack → render
     the real index and Master Index PDFs → remeasure their actual page
     counts → repack if either changed — until stable;
   - the Master Index is bound into the very front of Volume 1 (before
     Volume 1's own index); every other volume gets only its own index;
   - **S.No. and page numbers**: Master Index numbers items continuously
     across the whole book; each Volume's own index restarts at 1 — but
     the *page* column always shows the number physically stamped on that
     page (global continuous, or per-volume if you turned continuous
     numbering off) — both match what's actually on the page;
   - stamps page numbers on every page, adds a PDF bookmark for the Master
     Index, each volume's own Index, and every item/chunk;
   - runs a QC pass — every item placed, no volume over cap, index lengths
     match what was actually assembled — and marks the run FAIL rather
     than shipping something silently wrong if anything doesn't reconcile
     (see `qc_report.txt` in the output ZIP).
6. **Download** the ZIP: `Volume_1.pdf`, `Volume_2.pdf`, …,
   `Master_Index.pdf`, `qc_report.txt`.
7. **Size check**: after assembly, each volume PDF is checked against the
   **50MB NCLAT efiling upload cap**. If a volume comes out over 50MB
   (common with heavily scanned annexures), it's automatically
   recompressed — images are downsampled and re-encoded in progressively
   more aggressive passes until the file is under 50MB or compression
   stops helping. Only raster images are touched; the page-number stamp,
   bookmarks, and all index/table text are vector content and untouched.
   The QC report shows the before/after size for every volume. In the
   rare case a volume still can't be brought under 50MB (e.g. genuinely
   incompressible content), the QC report FAILs that volume explicitly
   rather than shipping an oversized file.

## Where the assumptions are, if you need to change them

All in `engine.py`:
- `FORUM_CAPS` — the two page caps (NCLAT 190 / NCLT 150).
- `MAX_FILE_SIZE_BYTES` — the 50MB efiling size cap; change here if a
  different portal has a different limit.
- `_compress_pdf_if_needed()` — the compression passes (quality/max
  dimension steps) if you want it to compress harder or lighter before
  giving up.
- Master Index is bound into the front of Volume 1 only (before Volume
  1's own index); every other volume gets just its own index.
- Annexures **do** split across a volume boundary when they don't fit —
  the continuation is labelled "(Cont.)" in both indexes, matching real
  filed Master Index conventions. See `_pack_chunks()`.
- Index pages (Master Index and every Volume Index) carry no page number
  at all — only content pages are numbered, starting at 1 from the first
  content page.


## Hosting it online (to share access with a colleague)

Everything above runs on your own machine only. To give a colleague their
own URL to open in a browser, deploy it to **Render** (free web service
tier, no credit card at the time of writing — double-check that on
Render's signup page, since free-tier terms do change). Free tier tradeoff:
it sleeps after 15 minutes with no traffic and takes 30-60 seconds to
wake up on the next visit — fine for occasional use, not for
instant-always-on access.

**This app now has a password gate** (see `app.py` /
`BasicAuthMiddleware`) — required once it's reachable on the open
internet, since it handles real case documents. Locally it stays
disabled automatically; only setting the two environment variables below
turns it on.

1. Push this folder to a GitHub repo (skip if already done):
   ```bash
   git init
   git add .
   git commit -m "Paper Book Compiler"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo-name>.git
   git push -u origin main
   ```
2. Go to [render.com](https://render.com), sign up, click **New +** →
   **Blueprint**, and point it at your repo. Render reads `render.yaml`
   (included) and configures the service automatically.
3. When prompted for the two environment variables `PAPERBOOK_USER` and
   `PAPERBOOK_PASS`, set them to a username/password you and your
   colleague will share — this is what protects the app once it's public.
4. Click **Apply** / **Deploy**. After the build finishes (a few
   minutes), Render gives you a URL like
   `https://paperbook-compiler.onrender.com` — send that plus the
   username/password to your colleague.
5. First load after any period of inactivity will be slow (cold start) —
   that's normal for the free tier, not a fault.

**Note on data:** the free tier's disk is wiped on redeploys/restarts, so
generated paper books should be downloaded promptly rather than left
sitting in a browser tab — nothing is meant to persist there long-term.


## Files

- `app.py` — FastAPI server (upload, generate, download endpoints).
- `engine.py` — the actual compilation logic; usable standalone/headless
  if you ever want to script it without the GUI.
- `static/index.html` — the GUI (single file, no build step).
- `render.yaml` — deployment config for hosting on Render (see "Hosting
  it online" above).
