from fastapi import FastAPI, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from urllib.parse import quote, urlparse
from datetime import date, timedelta
import datetime
import os, io, re, requests, json
import pandas as pd
import trafilatura
from lxml import html
from readability import Document as ReadabilityDoc
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from docx.enum.text import WD_BREAK

# Biztonságos import az új SDK-hoz
try:
    from google import genai
except ImportError:
    genai = None

# ===================== Gemini Konfiguráció =====================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_GENAI_CLIENT = None
# A Gemini 2.0 Flash jelenleg a leggyorsabb és legjobb erre a célra
_GEMINI_MODEL_ID = "gemini-2.0-flash" 

if GEMINI_API_KEY and genai:
    try:
        _GENAI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"Hiba a Gemini kliens indításakor: {e}")

# ===================== Konfiguráció ==========================
TEMPLATE_PATH = "ceges_sablon.docx"
REQUIRED_COLS = {"Rovat", "Link"}
APP_SECRET = "007"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================== Segédfüggvények =======================
def csv_url(sheet_id: str, sheet_name: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={quote(sheet_name)}"

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

SENT_END_RE = re.compile(r'[.!?…]"?$')
AD_PATTERNS = [
    r"^\s*hirdet[ée]s\b", r"^\s*szponzor[áa]lt\b", r"^\s*t[áa]mogatott tartalom\b",
    r"^\s*aj[áa]nl[oó]\b", r"^\s*kapcsol[óo]d[óo].*", r"^\s*olvasta m[áa]r\??",
    r"^\s*promo", r"^\s*advertisement\b", r"^\s*sponsored\b",
    r"^source:\s*.+$", r".*\bc[íi]mlapk[ée]p\b.*", r".*\bbor[íi]t[óo]k[ée]p\b.*",
    r"^\s*hírlev[ée]l", r"^\s*fot[óo]gal[ée]ria\b"
]
JUNK_RE = re.compile("|".join(AD_PATTERNS), flags=re.IGNORECASE)

def is_sentence_like(s: str) -> bool:
    s = s.strip()
    return bool(SENT_END_RE.search(s)) or len(s) > 200

def clean_and_merge(paras: list[str]) -> list[str]:
    lines = []
    for p in paras:
        t = norm_space(p)
        if not t or JUNK_RE.search(t): continue
        if len(t) < 35 and not t.endswith(":"): continue
        lines.append(t)
    merged, buf = [], ""
    for t in lines:
        buf = f"{buf} {t}".strip() if buf else t
        if is_sentence_like(buf):
            merged.append(buf); buf = ""
    if buf and len(buf) > 60: merged.append(buf)
    return merged

def add_bm(paragraph, name: str):
    run = paragraph.add_run()
    r = run._r
    bs = OxmlElement('w:bookmarkStart'); bs.set(qn('w:id'), '1'); bs.set(qn('w:name'), name)
    be = OxmlElement('w:bookmarkEnd'); be.set(qn('w:id'), '1')
    r.append(bs); r.append(be)

def add_link(paragraph, text: str, anchor: str):
    h = OxmlElement('w:hyperlink'); h.set(qn('w:anchor'), anchor)
    r = OxmlElement('w:r'); rPr = OxmlElement('w:rPr')
    u = OxmlElement('w:u'); u.set(qn('w:val'), 'single'); rPr.append(u)
    c = OxmlElement('w:color'); c.set(qn('w:val'), '0000FF'); rPr.append(c)
    t = OxmlElement('w:t'); t.text = text
    r.append(rPr); r.append(t); h.append(r); paragraph._p.append(h)

def hu_date(d: datetime.date) -> str:
    return d.strftime("%Y.%m.%d.")

def monday_of(isodate_str: str) -> date:
    try:
        y, m, d = [int(x) for x in isodate_str.split("-")]
        dt = date(y, m, d)
        return dt if dt.weekday() == 0 else (dt - timedelta(days=dt.weekday()))
    except:
        return date.today()

def week_range_from_monday(monday: date):
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()

def last_7_days():
    today = date.today()
    return (today - timedelta(days=6)).isoformat(), today.isoformat()

# ===================== Cikk kinyerés =========================
def read_paras(url: str):
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        rd = ReadabilityDoc(r.text)
        title = (rd.short_title() or "").strip()
        root = html.fromstring(rd.summary())
        paras = [norm_space(el.text_content()) for el in root.xpath(".//p")]
        cleaned = clean_and_merge(paras)
        if cleaned: return title, cleaned
    except: pass
    return "", []

def pick_lead(paras: list[str]) -> str:
    if not paras: return ""
    text = paras[0]
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    lead = parts[0] if parts else text
    if len(parts) >= 2 and len(lead) < 200: lead = f"{lead} {parts[1]}"
    return lead

# ===================== API Modellek =========================
class GeneratePayload(BaseModel):
    sheet_id: str
    worksheet: str
    rovat: str
    secret: str | None = None

class ChatPayload(BaseModel):
    sheet_id: str | None = None
    worksheet: str | None = None
    rovat: str
    query: str | None = ""
    n: int | None = 10
    date_from: str | None = None
    date_to: str | None = None

# ===================== Végpontok ============================
@app.get("/health")
def health():
    return {"ok": True, "gemini": bool(_GENAI_CLIENT), "lib": bool(genai)}

@app.post("/generate")
def generate(p: GeneratePayload):
    if APP_SECRET and (p.secret != APP_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        df = pd.read_csv(csv_url(p.sheet_id, p.worksheet))
        df = df.dropna(subset=["Rovat", "Link"])
        df = df[df["Rovat"].astype(str).str.strip() == p.rovat]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Sheet error: {e}")

    doc = Document(TEMPLATE_PATH) if os.path.exists(TEMPLATE_PATH) else Document()
    # (A DOCX generálás többi része változatlan marad...)
    # ... (Itt a korábbi generáló kódod fut tovább)
    return Response(content=b"", media_type="application/octet-stream") # Példa visszatérés

@app.post("/chat")
def chat(p: ChatPayload):
    if not _GENAI_CLIENT:
        raise HTTPException(503, "Gemini nincs konfigurálva. Hiányzó API kulcs vagy csomag.")

    d_from, d_to = (p.date_from, p.date_to) if p.date_from else last_7_days()
    if p.worksheet and not p.date_from:
        d_from, d_to = week_range_from_monday(monday_of(p.worksheet))

    n = max(1, min(int(p.n or 10), 15))
    prompt = f"""Adj vissza egy JSON listát hírekről: [{{"title": "...", "url": "...", "source": "..."}}].
    Rovat: {p.rovat}. Időszak: {d_from} - {d_to}. Extra: {p.query}. 
    Csak a JSON-t küldd, semmi mást! Pontosan {n} db hírt."""

    try:
        resp = _GENAI_CLIENT.models.generate_content(model=_GEMINI_MODEL_ID, contents=prompt)
        txt = (resp.text or "").strip()
        if "```" in txt:
            txt = re.sub(r"```(?:json)?|```", "", txt).strip()
        items = json.loads(txt)
        return {"ok": True, "sources": items, "items": items}
    except Exception as e:
        raise HTTPException(500, f"Gemini hiba: {e}")
