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

# ===== FastAPI =====
app = FastAPI()

# ===== Segédek =====
def csv_url(sheet_id: str, sheet_name: str) -> str:
    # Publikus CSV export (Worksheet név URL-kódolva)
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={quote(sheet_name)}"

# „zajos” sorok (képforrások, címlapkép, ügynökségek stb.) – kiszűrjük
JUNK_RE = re.compile(
    r"(?:%s)" % "|".join([
        r"^forr[áa]s:.*", r"^source:.*", r"^kapcsol[óo]d[óo].*", r"^hirdet[ée]s.*",
        r".*appeared first on.*",
        r".*\bc[íi]mlapk[ée]p\b.*", r".*\bbor[íi]t[óo]k[ée]p\b.*",
        r".*\b(k[ée]p|fot[óo])\s+forr[áa]sa:.*",
        r".*\b(getty images|shutterstock|reuters|associated press|ap photo|afp|epa)\b.*",
    ]),
    flags=re.IGNORECASE
)

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

def read_paras(url: str):
    """Cikk kinyerése: Readability (HTML <p>), majd trafilatura fallback."""
    # 1) Readability
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        rd = ReadabilityDoc(r.text)
        title = (rd.short_title() or "").strip()
        root = html.fromstring(rd.summary())
        paras = []
        for el in root.xpath(".//p"):
            t = norm_space(el.text_content())
            if t and not JUNK_RE.search(t):
                paras.append(t)
        if paras:
            return title, paras
    except Exception:
        pass

    # 2) trafilatura fallback
    try:
        dl = trafilatura.fetch_url(url)
        text = trafilatura.extract(dl) if dl else None
        if text:
            blocks = [norm_space(b) for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if norm_space(b)]
            paras = [b for b in blocks if not JUNK_RE.search(b)]
        else:
            paras = []
        title = ""
        try:
            meta = trafilatura.extract_metadata(dl)
            if meta and getattr(meta, "title", None):
                title = meta.title.strip()
        except Exception:
            pass
        return title, paras
    except Exception:
        return "", []

def add_bm(paragraph, name: str):
    """Belső könyvjelző beszúrása a Wordbe."""
    run = paragraph.add_run(); r = run._r
    bs = OxmlElement('w:bookmarkStart'); bs.set(qn('w:id'), '1'); bs.set(qn('w:name'), name)
    be = OxmlElement('w:bookmarkEnd');   be.set(qn('w:id'), '1')
    r.append(bs); r.append(be)

def add_link(paragraph, text: str, anchor: str):
    """Belső hivatkozás beszúrása (anchor = könyvjelző neve)."""
    h = OxmlElement('w:hyperlink'); h.set(qn('w:anchor'), anchor)
    r = OxmlElement('w:r'); rPr = OxmlElement('w:rPr')
    u = OxmlElement('w:u'); u.set(qn('w:val'), 'single'); rPr.append(u)
    c = OxmlElement('w:color'); c.set(qn('w:val'), '0000FF'); rPr.append(c)
    t = OxmlElement('w:t'); t.text = text
    r.append(rPr); r.append(t); h.append(r); paragraph._p.append(h)

def hu_date(d: datetime.date) -> str:
    return d.strftime("%Y.%m.%d.")

# ===== Bejövő payload =====
class Payload(BaseModel):
    sheet_id: str
    worksheet: str   # pl. "2025-10-06"
    rovat: str       # pl. "Industrials"
    secret: str | None = None

# ===== API: /generate =====
@app.post("/generate")
def generate(p: Payload):
    # Simple auth
    if APP_SECRET and (p.secret != APP_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Sheet beolvasása
    try:
        df = pd.read_csv(csv_url(p.sheet_id, p.worksheet))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Sheet read error: {e}")

    miss = REQUIRED_COLS - set(df.columns)
    if miss:
        raise HTTPException(status_code=400, detail=f"Missing columns: {', '.join(miss)}")

    df = df.dropna(subset=["Rovat", "Link"])
    df = df[df["Rovat"].astype(str).str.strip() == p.rovat]
    df = df[df["Link"].astype(str).str.startswith(("http://", "https://"), na=False)]
    if df.empty:
        raise HTTPException(status_code=404, detail=f"No links for rovat '{p.rovat}' on sheet '{p.worksheet}'")

    # DOCX indulása sablonból (ha nincs meg, üresből)
    doc = Document(TEMPLATE_PATH) if os.path.exists(TEMPLATE_PATH) else Document()

    # Fejléc: Cím + dátum
    doc.add_paragraph(f"Weekly News | {p.rovat}")
    try:
        y, m, d = [int(x) for x in p.worksheet.split("-")]
        monday = datetime.date(y, m, d)
    except Exception:
        monday = datetime.date.today()
    doc.add_paragraph(hu_date(monday))

    # Intró horgony
    add_bm(doc.add_paragraph(), "INTRO")

    # Intró: cím + lead + külön 'read article >>>'
    rows = df.reset_index(drop=True)
    for i, row in rows.iterrows():
        url = str(row["Link"]).strip()
        title, paras = read_paras(url)
        if not title:
            u = urlparse(url)
            title = f"{u.netloc}{u.path}".strip("/") or "Cím nélkül"

        # cím
        doc.add_paragraph(f"{i+1}. {title}")

        # lead: első normális bekezdés, 1–2 mondatra rövidítve
        lead = ""
        for ptxt in paras:
            t = norm_space(ptxt)
            if not t.endswith(":") and len(t) >= 60:
                parts = re.split(r"(?<=[.!?])\s+", t)
                lead = " ".join(parts[:2]).strip()
                if len(lead) > 420:
                    lead = lead[:420].rsplit(" ", 1)[0] + "…"
                break
        if lead:
            doc.add_paragraph(lead)

        link_p = doc.add_paragraph()
        add_link(link_p, "read article >>>", f"cikk_{i}")
        doc.add_paragraph("")  # térköz

    # Articles szekció
    doc.add_paragraph("")
    doc.add_paragraph("Articles")
    doc.add_paragraph("")

    for i, row in rows.iterrows():
        url = str(row["Link"]).strip()
        title, paras = read_paras(url)
        if not title:
            u = urlparse(url)
            title = f"{u.netloc}{u.path}".strip("/") or "Cím nélkül"

        ptitle = doc.add_paragraph()
        add_bm(ptitle, f"cikk_{i}")
        ptitle.add_run(title).bold = True

        dom = urlparse(url).netloc.lower().replace("www.", "")
        doc.add_paragraph(f"Source: {dom}")

        for para in paras:
            doc.add_paragraph(para)

        back = doc.add_paragraph()
        add_link(back, "back to intro >>>", "INTRO")
        doc.add_page_break()

    # Memóriába mentés és visszaküldés
    fname = f"BN_{p.rovat} news_{monday.strftime('%Y%m%d')}.docx"
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"X-Filename": fname}
    )
