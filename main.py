# main.py – BN News DOCX generátor (FastAPI)
# ------------------------------------------
# /generate JSON:
# { "sheet_id":"<GOOGLE_SHEET_ID>", "worksheet":"YYYY-MM-DD", "rovat":"Industrials", "secret":"007" }

from fastapi import FastAPI, Response, HTTPException
from pydantic import BaseModel
import os, io, re, datetime, requests, pandas as pd
from urllib.parse import quote, urlparse
from readability import Document as ReadabilityDoc
from lxml import html
import trafilatura

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt              # betűméret
from docx.enum.text import WD_BREAK     # oldaltörés

# ===== Konfiguráció =====
TEMPLATE_PATH = "ceges_sablon.docx"
REQUIRED_COLS = {"Rovat", "Link"}
APP_SECRET = "007"     # egyezzen az Apps Scriptben

app = FastAPI()

# ===== Segédek =====
def csv_url(sheet_id: str, sheet_name: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={quote(sheet_name)}"

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

# mondatzáró jel
SENT_END_RE = re.compile(r'[.!?…]"?$')

# reklám/junk minták
AD_PATTERNS = [
    r"^\s*hirdet[ée]s\b", r"^\s*szponzor[áa]lt\b", r"^\s*t[áa]mogatott tartalom\b",
    r"^\s*aj[áa]nl[oó]\b", r"^\s*kapcsol[óo]d[óo].*", r"^\s*olvasta m[áa]r\??",
    r"^\s*promo", r"^\s*advertisement\b", r"^\s*sponsored\b",
    r"^source:\s*.+$", r".*\bc[íi]mlapk[ée]p\b.*", r".*\bbor[íi]t[óo]k[ée]p\b.*",
    r".*\b(getty images|shutterstock|reuters|associated press|ap photo|afp|epa)\b.*",
    r"^\s*back to intro\b", r"^\s*read article\b",
    r"^érdekesnek találta.*hírlevelünkre", r"^\s*hírlev[ée]l",
    r"^\s*kapcsol[óo]d[óo] cikk(ek)?\b", r"^\s*fot[óo]gal[ée]ria\b",
    r"^\s*tov[áa]bbi (h[íi]reink|cikkek)\b",
    r"^\s*Csapjunk bele a közepébe",   # Portfolio "hirtelen kezdés"
    r"A cikk elkészítésében .* Alrite .* alkalmazás támogatta a munkánkat\.?$",
]

]
JUNK_RE = re.compile("|".join(AD_PATTERNS), flags=re.IGNORECASE)

def is_sentence_like(s: str) -> bool:
    s = s.strip()
    return bool(SENT_END_RE.search(s)) or len(s) > 200

def clean_and_merge(paras: list[str]) -> list[str]:
    """Bekezdések tisztítása és teljes mondatokra fűzése."""
    lines = []
    for p in paras:
        t = norm_space(p)
        if not t:
            continue
        if JUNK_RE.search(t):
            continue
        # nagyon rövid, feltehetően alcím – kihagyjuk
        if len(t) < 35 and not t.endswith(":"):
            continue
        lines.append(t)

    merged, buf = [], ""
    for t in lines:
        buf = f"{buf} {t}".strip() if buf else t
        if is_sentence_like(buf):
            merged.append(buf)
            buf = ""
    # ha maradt valami hosszabb a pufferben, engedjük át
    if buf and len(buf) > 60:
        merged.append(buf)

    merged = [m for m in merged if not JUNK_RE.search(m)]
    return merged

def add_bm(paragraph, name: str):
    """Belső könyvjelző beszúrása."""
    run = paragraph.add_run()
    r = run._r
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

# ===== Cikk kinyerés =====
def read_paras(url: str):
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

    # 2) trafilatura
    try:
        dl = trafilatura.fetch_url(url)
        text = trafilatura.extract(
            dl,
            include_comments=False, include_tables=False,
            favor_recall=True, no_fallback=False
        ) if dl else None

        paras = []
        if text:
            blocks = [norm_space(b) for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n"))]
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

def pick_lead(paras: list[str]) -> str:
    """Az első 1–2 TELJES mondat a bevezetőhöz – félmondatot nem hagyunk."""
    if not paras:
        return ""
    text = paras[0]
    parts = re.split(r"(?<=[.!?…])\s+", text)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return text
    lead = parts[0]
    if len(parts) >= 2 and len(lead) < 220:
        lead = f"{lead} {parts[1]}"
    return lead.strip()

# ===== Payload =====
class Payload(BaseModel):
    sheet_id: str
    worksheet: str
    rovat: str
    secret: str | None = None

# ===== Endpoint =====
@app.post("/generate")
def generate(p: Payload):
    # auth
    if APP_SECRET and (p.secret != APP_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # sheet beolvasás
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

    # DOCX indul
    doc = Document(TEMPLATE_PATH) if os.path.exists(TEMPLATE_PATH) else Document()

    # Főcím – félkövér + Heading 1
    title_p = doc.add_paragraph()
    try:
        title_p.style = 'Heading 1'  # legyen a sablonban!
    except Exception:
        pass
    run = title_p.add_run(f"Weekly News | {p.rovat}")
    run.bold = True
    try:
        run.font.size = Pt(12.5)
    except Exception:
        pass

    # Dátum
    try:
        y, m, d = [int(x) for x in p.worksheet.split("-")]
        monday = datetime.date(y, m, d)
    except Exception:
        monday = datetime.date.today()
    doc.add_paragraph(hu_date(monday))

    # Intro horgony
    add_bm(doc.add_paragraph(), "INTRO")

    rows = df.reset_index(drop=True)

    # Rövid intro blokkok
    for i, row in rows.iterrows():
        url = str(row["Link"]).strip()
        title, paras = read_paras(url)
        if not title:
            u = urlparse(url)
            title = f"{u.netloc}{u.path}".strip("/") or "Cím nélkül"

        # félkövér cím sor
        intro_line = doc.add_paragraph()
        r = intro_line.add_run(f"{i+1}. {title}")
        r.bold = True

        # lead (1–2 teljes mondat)
        lead = pick_lead(paras)
        if lead:
            doc.add_paragraph(lead)

        link_p = doc.add_paragraph()
        add_link(link_p, "read article >>>", f"cikk_{i}")
        doc.add_paragraph("")

    # Intro után oldaltörés
    try:
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    except Exception:
        pass

    # „Articles” cím – félkövér + Heading 2
    sec = doc.add_paragraph()
    try:
        sec.style = 'Heading 2'
    except Exception:
        pass
    sr = sec.add_run("Articles")
    sr.bold = True

    # Cikkek
    for i, row in rows.iterrows():
        url = str(row["Link"]).strip()
        title, paras = read_paras(url)
        if not title:
            u = urlparse(url)
            title = f"{u.netloc}{u.path}".strip("/") or "Cím nélkül"

        # Cikk cím – félkövér + Heading 2 + könyvjelző
        ptitle = doc.add_paragraph()
        try:
            ptitle.style = 'Heading 2'
        except Exception:
            pass
        add_bm(ptitle, f"cikk_{i}")
        rr = ptitle.add_run(title)
        rr.bold = True

        # Forrás
        dom = urlparse(url).netloc.lower().replace("www.", "")
        doc.add_paragraph(f"Source: {dom}")

        # Törzs
        for para in paras:
            doc.add_paragraph(para)

        # Vissza az intróhoz
        back = doc.add_paragraph()
        add_link(back, "back to intro >>>", "INTRO")

        # Oldaltörés cikkek között
        if i != len(rows) - 1:
            try:
                doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            except Exception:
                pass

    # Visszaküldés
    fname = f"BN_{p.rovat} news_{monday.strftime('%Y%m%d')}.docx"
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"X-Filename": fname}
    )
