# main.py – BN News DOCX generátor (FastAPI)
# ------------------------------------------------------------------
# /generate  JSON payload:
#   { "sheet_id": "<GOOGLE_SHEET_ID>", "worksheet": "YYYY-MM-DD", "rovat": "Industrials", "secret": "CHANGE_ME" }
# Válasz: application/vnd.openxmlformats-officedocument.wordprocessingml.document  (DOCX bináris)
# ------------------------------------------------------------------

from fastapi import FastAPI, Response, HTTPException
from pydantic import BaseModel
import os, io, re, datetime, inspect, requests, pandas as pd
from urllib.parse import quote, urlparse
from readability import Document as ReadabilityDoc
from lxml import html
import trafilatura
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

# ===== Konfiguráció =====
TEMPLATE_PATH = "ceges_sablon.docx"   # tedd ezt a fájlt a main.py mellé
REQUIRED_COLS = {"Rovat", "Link"}
APP_SECRET = '007'              # állíts be erős jelszót, és ugyanazt tedd a Code.gs payloadjába

app = FastAPI(title="BN News API", version="1.1")

# ===== Utilities =====
def csv_url(sheet_id: str, sheet_name: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={quote(sheet_name)}"

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0"," ")).strip()

# Ad/boilerplate filters
SENT_END_RE = re.compile(r'[.!?…]"?$')

AD_PATTERNS = [
    r"^\s*hirdet[ée]s\b",
    r"^\s*szponzor[áa]lt\b",
    r"^\s*t[áa]mogatott tartalom\b",
    r"^\s*aj[áa]nl[oó]\b",
    r"^\s*kapcsol[óo]d[óo].*",
    r"^\s*olvasta m[áa]r\??",
    r"^\s*promo",
    r"^\s*advertisement\b",
    r"^\s*sponsored\b",
    r"^source:\s*.+$",
    r".*\bc[íi]mlapk[ée]p\b.*",
    r".*\bbor[íi]t[óo]k[ée]p\b.*",
    r".*\b(getty images|shutterstock|reuters|associated press|ap photo|afp|epa)\b.*",
    r"^\s*back to intro\b",
    r"^\s*read article\b",
]
JUNK_RE = re.compile("|".join(AD_PATTERNS), flags=re.IGNORECASE)

def is_sentence_like(s: str) -> bool:
    s = s.strip()
    return bool(SENT_END_RE.search(s)) or len(s) > 200  # nagyon hosszú bekezdés is „lezárt”-nak vehető

def clean_and_merge(paras: list[str]) -> list[str]:
    """Reklám/ajánló kiszűrése + félmondatok összefűzése teljes mondattá."""
    # 1) alap takarítás
    lines = []
    for p in paras:
        t = norm_space(p)
        if not t:
            continue
        if JUNK_RE.search(t):
            continue
        # nagyon rövid szemét darabokat lökjük el (pl. magányos 'Kép:' stb.)
        if len(t) < 35 and not t.endswith(":"):
            continue
        lines.append(t)

    # 2) félmondatok összefűzése
    merged, buf = [], ""
    for t in lines:
        buf = (buf + " " + t).strip() if buf else t
        if is_sentence_like(buf):
            merged.append(buf)
            buf = ""
    if buf and len(buf) > 60:   # maradék, ha értelmes hosszú
        merged.append(buf)

    # 3) biztos ami biztos: utótakarítás
    merged = [m for m in merged if not JUNK_RE.search(m)]
    return merged

def add_bm(paragraph, name: str):
    run = paragraph.add_run(); r = run._r
    bs = OxmlElement('w:bookmarkStart'); bs.set(_qn('w:id'), '1'); bs.set(_qn('w:name'), name)
    be = OxmlElement('w:bookmarkEnd');   be.set(_qn('w:id'), '1')
    r.append(bs); r.append(be)

def add_link(paragraph, text: str, anchor: str):
    h = OxmlElement('w:hyperlink'); h.set(_qn('w:anchor'), anchor)
    r = OxmlElement('w:r'); rPr = OxmlElement('w:rPr')
    u = OxmlElement('w:u'); u.set(_qn('w:val'), 'single'); rPr.append(u)
    c = OxmlElement('w:color'); c.set(_qn('w:val'), '0000FF'); rPr.append(c)
    t = OxmlElement('w:t'); t.text = text
    r.append(rPr); r.append(t); h.append(r); paragraph._p.append(h)

def hu_date(d: datetime.date) -> str:
    return d.strftime("%Y.%m.%d.")

# ===== Extraction =====
def read_paras(url: str):
    """Cikk kinyerése: Readability, majd Trafi; tisztítás + félmondat-összefűzés; (title, paragraphs)."""
    # 1) Readability
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        rd = ReadabilityDoc(r.text)
        title = (rd.short_title() or "").strip()
        root = html.fromstring(rd.summary())
        paras = [norm_space(el.text_content()) for el in root.xpath(".//p")]
        paras = [p for p in paras if p and not JUNK_RE.search(p)]
        cleaned = clean_and_merge(paras)
        if cleaned:
            return title, cleaned
    except Exception:
        pass

    # 2) Trafi fallback
    try:
        dl = trafilatura.fetch_url(url)
        text = trafilatura.extract(
            dl,
            include_comments=False,
            include_tables=False,
            favor_recall=True,   # több szöveg
            no_fallback=False
        ) if dl else None
        paras = []
        if text:
            blocks = [norm_space(b) for b in re.split(r"\n\s*\n", text.replace("\r\n","\n"))]
            paras = [b for b in blocks if b and not JUNK_RE.search(b)]
        title = ""
        try:
            meta = trafilatura.extract_metadata(dl)
            if meta and getattr(meta, "title", None):
                title = meta.title.strip()
        except Exception:
            pass
        cleaned = clean_and_merge(paras)
        return title, cleaned
    except Exception:
        return "", []

# ===== API Models =====
class Payload(BaseModel):
    sheet_id: str
    worksheet: str   # "YYYY-MM-DD"
    rovat: str       # "Industrials"
    secret: str | None = None

# ===== Endpoint =====
@app.post("/generate")
def generate(p: Payload):
    # Auth
    if APP_SECRET and (p.secret != APP_SECRET):
        raise HTTPException(401, "Unauthorized")

    # Read sheet
    try:
        df = pd.read_csv(csv_url(p.sheet_id, p.worksheet))
    except Exception as e:
        raise HTTPException(400, f"Sheet read error: {e}")

    miss = REQUIRED_COLS - set(df.columns)
    if miss:
        raise HTTPException(400, f"Missing columns: {', '.join(miss)}")

    df = df.dropna(subset=["Rovat","Link"])
    df = df[df["Rovat"].astype(str).str.strip() == p.rovat]
    df = df[df["Link"].astype(str).str.startswith(("http://","https://"), na=False)]
    if df.empty:
        raise HTTPException(404, f"No links for rovat '{p.rovat}' on sheet '{p.worksheet}'")

    # Start doc (use template if exists)
    doc = Document(TEMPLATE_PATH) if os.path.exists(TEMPLATE_PATH) else Document()

    # Top heading: "Weekly News | {rovat}" — bold
    t = doc.add_paragraph()
    r = t.add_run(f"Weekly News | {p.rovat}")
    r.bold = True
    r.font.size = Pt(12.5)

    # Date line
    try:
        y,m,d = [int(x) for x in p.worksheet.split("-")]
        monday = datetime.date(y,m,d)
    except Exception:
        monday = datetime.date.today()
    doc.add_paragraph(hu_date(monday))

    # Intro anchor + index
    add_bm(doc.add_paragraph(), "INTRO")

    rows = df.reset_index(drop=True)
    for i, row in rows.iterrows():
        url = str(row["Link"]).strip()
        title, paras = read_paras(url)
        if not title:
            u = urlparse(url); title = f"{u.netloc}{u.path}".strip("/") or "Cím nélkül"

        # numbered title in intro
        intro_line = doc.add_paragraph()
        intro_line.add_run(f"{i+1}. {title}")

        # choose a lead (first longish paragraph)
        lead = ""
        for ptxt in paras:
            if len(ptxt) >= 60 and not ptxt.endswith(":"):
                lead = ptxt
                break
        if lead:
            doc.add_paragraph(lead)

        link_p = doc.add_paragraph()
        add_link(link_p, "read article >>>", f"cikk_{i}")
        doc.add_paragraph("")

    # Articles
    doc.add_paragraph("")
    sec = doc.add_paragraph()
    sr = sec.add_run("Articles")
    sr.bold = True

    for i, row in rows.iterrows():
        url = str(row["Link"]).strip()
        title, paras = read_paras(url)
        if not title:
            u = urlparse(url); title = f"{u.netloc}{u.path}".strip("/") or "Cím nélkül"

        # Title (bold)
        ptitle = doc.add_paragraph()
        add_bm(ptitle, f"cikk_{i}")
        rr = ptitle.add_run(title)
        rr.bold = True

        # Source line
        dom = urlparse(url).netloc.lower().replace("www.","")
        doc.add_paragraph(f"Source: {dom}")

        # Lead + body
        lead_written = False
        for j, para in enumerate(paras):
            if not lead_written:
                if len(para) >= 60 and not para.endswith(":"):
                    doc.add_paragraph(para)
                    lead_written = True
                elif j == 0:
                    doc.add_paragraph(para)
                    lead_written = True
            else:
                doc.add_paragraph(para)

        # Back link
        back = doc.add_paragraph()
        add_link(back, "back to intro >>>", "INTRO")

        # Page break after each article except last
        if i != len(rows) - 1:
            br = doc.add_paragraph().add_run()
            br.add_break(WD_BREAK.PAGE)

    # Save to bytes and return
    fname = f"BN_{p.rovat} news_{monday.strftime('%Y%m%d')}.docx"
    buf = io.BytesIO()
    doc.save(buf); buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"X-Filename": fname}
    )


