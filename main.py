# main.py – BN News DOCX generátor (FastAPI)
# ------------------------------------------
from fastapi import FastAPI, Response, HTTPException
from pydantic import BaseModel
from urllib.parse import quote, urlparse
from readability import Document as ReadabilityDoc
from lxml import html
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt  # betűméret
from docx.enum.text import WD_BREAK  # oldaltörés
import os, io, re, datetime, requests, pandas as pd
import trafilatura
import google.generativeai as genai
import json

# ===== Konfiguráció =====
TEMPLATE_PATH = "ceges_sablon.docx"
REQUIRED_COLS = {"Rovat", "Link"}
APP_SECRET = "007"  # egyezzen az Apps Scriptben
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

app = FastAPI()

# ===== Payload osztályok (Elöl a hiba elkerülése végett) =====
class Payload(BaseModel):
    sheet_id: str
    worksheet: str
    rovat: str
    secret: str | None = None

class ChatPayload(BaseModel):
    sheet_id: str
    worksheet: str
    rovat: str
    query: str | None = ""
    secret: str | None = None

# ===== Segédek (EREDETI KÓDOD) =====
def csv_url(sheet_id: str, sheet_name: str) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?"
        f"tqx=out:csv&sheet={quote(sheet_name)}"
    )

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

SENT_END_RE = re.compile(r'[.!?…]"?$')

# reklám/junk minták - EREDETI + ECONOMX KIEGÉSZÍTÉS
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
    r"^érdekesnek találta.*hírlevelünkre",
    r"^\s*hírlev[ée]l",
    r"^\s*kapcsol[óo]d[óo] cikk(ek)?\b",
    r"^\s*fot[óo]gal[ée]ria\b",
    r"^\s*tov[áa]bbi (h[íi]reink|cikkek)\b",
    r"^\s*Csapjunk bele a közepébe",
    r"A cikk elkészítésében .* Alrite .* alkalmazás támogatta a munkánkat\.?$",
    r"A gazdaság és az üzleti élet legfrissebb hírei az Economx.hu hírlevelében",
    r"Küldtünk Önnek egy emailt!",
    r"feliratkozása megerősítéséhez"
]
JUNK_RE = re.compile("|".join(AD_PATTERNS), flags=re.IGNORECASE)

def is_sentence_like(s: str) -> bool:
    s = s.strip()
    # Felsorolás felismerése, hogy ne vesszen el
    if s.startswith("•") or s.startswith("- "):
        return True
    return bool(SENT_END_RE.search(s)) or len(s) > 200

def clean_and_merge(paras: list[str]) -> list[str]:
    lines = []
    for p in paras:
        t = norm_space(p)
        if not t or JUNK_RE.search(t):
            continue
        # Felsorolásokat és rövid alcímeket megtartunk
        if len(t) < 35 and not t.endswith(":") and not (t.startswith("•") or t.startswith("- ")):
            continue
        lines.append(t)

    merged, buf = [], ""
    for t in lines:
        if t.startswith("•") or t.startswith("- "):
            if buf: merged.append(buf)
            merged.append(t)
            buf = ""
            continue
        buf = f"{buf} {t}".strip() if buf else t
        if is_sentence_like(buf):
            merged.append(buf)
            buf = ""
    if buf and len(buf) > 60:
        merged.append(buf)
    return [m for m in merged if not JUNK_RE.search(m)]

# ===== Cikk kinyerés (EREDETI LOGIKÁD + felsorolás-fix) =====
def read_paras(url: str):
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        rd = ReadabilityDoc(r.text)
        title = (rd.short_title() or "").strip()
        root = html.fromstring(rd.summary())
        # Itt kinyerjük a p és li (lista) elemeket is
        paras = []
        for el in root.xpath(".//p | .//li"):
            text = norm_space(el.text_content())
            if el.tag == "li": text = f"• {text}"
            paras.append(text)
        
        cleaned = clean_and_merge(paras)
        if cleaned: return title, cleaned
    except: pass

    try:
        dl = trafilatura.fetch_url(url)
        text = trafilatura.extract(dl, include_comments=False, include_tables=True, favor_recall=True)
        if text:
            blocks = [norm_space(b) for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n"))]
            return "", clean_and_merge(blocks)
    except: pass
    return "", []

def pick_lead(paras: list[str]) -> str:
    if not paras: return ""
    text = next((p for p in paras if not p.startswith("•")), paras[0])
    parts = re.split(r"(?<=[.!?…])\s+", text)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts: return text
    lead = parts[0]
    if len(parts) >= 2 and len(lead) < 220:
        lead = f"{lead} {parts[1]}"
    return lead.strip()

# (add_bm, add_link, hu_date függvények az eredeti kódból változatlanul...)
def add_bm(paragraph, name: str):
    run = paragraph.add_run()
    r = run._r
    bs, be = OxmlElement("w:bookmarkStart"), OxmlElement("w:bookmarkEnd")
    bs.set(qn("w:id"), "1"); bs.set(qn("w:name"), name); be.set(qn("w:id"), "1")
    r.append(bs); r.append(be)

def add_link(paragraph, text: str, anchor: str):
    h = OxmlElement("w:hyperlink"); h.set(qn("w:anchor"), anchor)
    r = OxmlElement("w:r"); rPr = OxmlElement("w:rPr")
    u, c = OxmlElement("w:u"), OxmlElement("w:color")
    u.set(qn("w:val"), "single"); c.set(qn("w:val"), "0000FF")
    rPr.append(u); rPr.append(c); t = OxmlElement("w:t"); t.text = text
    r.append(rPr); r.append(t); h.append(r); paragraph._p.append(h)

def hu_date(d: datetime.date) -> str: return d.strftime("%Y.%m.%d.")

# ==========================================================
# 4. ÚJ AI KERESŐ ENDPOINT (Külön kezelve)
# ==========================================================
@app.post("/chat")
def chat_endpoint(p: ChatPayload):
    if APP_SECRET and (p.secret != APP_SECRET): raise HTTPException(status_code=401)
    if not GEMINI_API_KEY: return {"sources": []}
    
    try:
        ref = datetime.datetime.strptime(p.worksheet, "%Y-%m-%d")
        start = (ref - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        end = (ref - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    except: start, end = "last 7 days", "today"

    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"Adj egy JSON listát ([{{'title': '...', 'url': '...'}}]) releváns magyar gazdasági hírekről a(z) {p.rovat} témában {start} és {end} között. Csak a JSON legyen a válaszban."
    
    try:
        response = model.generate_content(prompt)
        match = re.search(r'\[.*\]', response.text, re.DOTALL)
        return {"sources": json.loads(match.group()) if match else []}
    except: return {"sources": []}

# ==========================================================
# 5. GENERATE ENDPOINT (EREDETI LOGIKÁD)
# ==========================================================
@app.post("/generate")
def generate(p: Payload):
    if APP_SECRET and (p.secret != APP_SECRET): raise HTTPException(status_code=401)
    try:
        df = pd.read_csv(csv_url(p.sheet_id, p.worksheet)).dropna(subset=["Rovat", "Link"])
        df = df[df["Rovat"].astype(str).str.strip() == p.rovat]
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

    doc = Document(TEMPLATE_PATH) if os.path.exists(TEMPLATE_PATH) else Document()
    # Főcím
    title_p = doc.add_paragraph()
    try: title_p.style = "Heading 1"
    except: pass
    title_p.add_run(f"Weekly News | {p.rovat}").bold = True
    
    # Dátum kezelés az eredeti módodon
    try:
        y, m, d = [int(x) for x in p.worksheet.split("-")]
        monday = datetime.date(y, m, d)
    except: monday = datetime.date.today()
    doc.add_paragraph(hu_date(monday))
    
    add_bm(doc.add_paragraph(), "INTRO")
    rows = df.reset_index(drop=True)

    for i, row in rows.iterrows():
        url = str(row["Link"]).strip()
        title, paras = read_paras(url)
        if not title: title = urlparse(url).netloc
        
        intro_line = doc.add_paragraph()
        intro_line.add_run(f"{i+1}. {title}").bold = True
        lead = pick_lead(paras)
        if lead: doc.add_paragraph(lead)
        add_link(doc.add_paragraph(), "read article >>>", f"cikk_{i}")
        doc.add_paragraph("")

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    for i, row in rows.iterrows():
        url = str(row["Link"]).strip()
        title, paras = read_paras(url)
        if not title: title = urlparse(url).netloc
        
        ptitle = doc.add_paragraph()
        try: ptitle.style = "Heading 2"
        except: pass
        add_bm(ptitle, f"cikk_{i}")
        ptitle.add_run(title).bold = True
        doc.add_paragraph(f"Source: {urlparse(url).netloc}")
        for para in paras: doc.add_paragraph(para)
        add_link(doc.add_paragraph(), "back to intro >>>", "INTRO")
        if i != len(rows) - 1:
            try: doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            except: pass

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"X-Filename": f"BN_{p.rovat}.docx"}
    )
