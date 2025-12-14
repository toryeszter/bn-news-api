# main.py – BN News DOCX generátor + AI link-kereső (FastAPI)
# --------------------------------------------------------------------------------
# /generate  -> DOCX (a meglévő működésed változatlanul megmarad)
# /chat      -> Gemini 1.5 Flash segítségével JSON link-lista (csak ha van GEMINI_API_KEY)
# /health    -> állapot teszt
# --------------------------------------------------------------------------------

from fastapi import FastAPI, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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

# ======= Opcionális Gemini (link-keresőhöz) =======
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
try:
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        _GEMINI_MODEL = genai.GenerativeModel("gemini-1.5-flash")
    else:
        _GEMINI_MODEL = None
except Exception:
    _GEMINI_MODEL = None

# ===== Konfiguráció =====
TEMPLATE_PATH = "ceges_sablon.docx"
REQUIRED_COLS = {"Rovat", "Link"}
APP_SECRET = "007"     # egyezzen az Apps Scriptben

app = FastAPI()

# ===== CORS (Docs/Sheets oldalsáv miatt) =====
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # ha akarod, szűkítheted domainre
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Segédek =====
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
    r".*\b(getty images|shutterstock|reuters|associated press|ap photo|afp|epa)\b.*",
    r"^\s*back to intro\b", r"^\s*read article\b",
    r"^érdekesnek találta.*hírlevelünkre", r"^\s*hírlev[ée]l",
    r"^\s*kapcsol[óo]d[óo] cikk(ek)?\b", r"^\s*fot[óo]gal[ée]ria\b",
    r"^\s*tov[áa]bbi (h[íi]reink|cikkek)\b",
    r"^\s*Csapjunk bele a közepébe",
    r"A cikk elkészítésében .* Alrite .* alkalmazás támogatta a munkánkat\.?$",
]
JUNK_RE = re.compile("|".join(AD_PATTERNS), flags=re.IGNORECASE)

def is_sentence_like(s: str) -> bool:
    s = s.strip()
    return bool(SENT_END_RE.search(s)) or len(s) > 200

def clean_and_merge(paras: list[str]) -> list[str]:
    lines = []
    for p in paras:
        t = norm_space(p)
        if not t: 
            continue
        if JUNK_RE.search(t):
            continue
        if len(t) < 35 and not t.endswith(":"):
            continue
        lines.append(t)

    merged, buf = [], ""
    for t in lines:
        buf = f"{buf} {t}".strip() if buf else t
        if is_sentence_like(buf):
            merged.append(buf); buf = ""
    if buf and len(buf) > 60:
        merged.append(buf)

    merged = [m for m in merged if not JUNK_RE.search(m)]
    return merged

def add_bm(paragraph, name: str):
    run = paragraph.add_run()
    r = run._r
    bs = OxmlElement('w:bookmarkStart'); bs.set(qn('w:id'), '1'); bs.set(qn('w:name'), name)
    be = OxmlElement('w:bookmarkEnd');   be.set(qn('w:id'), '1')
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

from datetime import date, timedelta

def monday_of(isodate_str: str) -> date:
    y, m, d = [int(x) for x in isodate_str.split("-")]
    dt = date(y, m, d)
    # biztosítsuk, hogy tényleg hétfő legyen (ha valaki véletlen más napra nevezte el)
    return dt if dt.weekday() == 0 else (dt - timedelta(days=dt.weekday()))

def week_range_from_monday(monday: date):
    start = monday
    end = monday + timedelta(days=6)
    return start.isoformat(), end.isoformat()

def last_7_days():
    today = date.today()
    start = today - timedelta(days=6)
    return start.isoformat(), today.isoformat()

# ===== Cikk kinyerés (meglévő logika) =====
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

# ===== Payloadok =====
class GeneratePayload(BaseModel):
    sheet_id: str
    worksheet: str
    rovat: str
    secret: str | None = None

class ChatPayload(BaseModel):
    # a Sheets most ezeket küldi:
    sheet_id: str | None = None
    worksheet: str | None = None
    rovat: str
    query: str | None = ""
    use_emis: bool | None = False
    # opcionálisan küldhető közvetlen dátumablak is:
    date_from: str | None = None
    date_to: str | None = None
    n: int | None = 10

    # 1) dátumablak meghatározás
    if p.date_from and p.date_to:
        d_from, d_to = p.date_from.strip(), p.date_to.strip()
    elif p.worksheet:
        try:
            mon = monday_of(p.worksheet.strip())
            d_from, d_to = week_range_from_monday(mon)  # hétfő–vasárnap
        except Exception:
            d_from, d_to = last_7_days()
    else:
        d_from, d_to = last_7_days()

    # 2) elemszám
    n = max(1, min(int(p.n or 10), 20))

    # 3) prompt
    prompt = _build_links_prompt(
        rovat=p.rovat.strip(),
        q=(p.query or "").strip(),
        date_from=d_from,
        date_to=d_to,
        n=n
    )

    # 4) modell hívás
    try:
        resp = _GEMINI_MODEL.generate_content(prompt)
        txt = (resp.text or "").strip()
    except Exception as e:
        raise HTTPException(500, f"Gemini error: {e}")

    # 5) JSON értelmezés
    import json
    try:
        items = json.loads(txt)
        if not isinstance(items, list):
            raise ValueError("Not a list")
    except Exception:
        raise HTTPException(502, "Model returned non-JSON output.")

    # 6) szűrés/normalizálás és a kívánt kulcsnév: 'sources'
    out = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or "").strip()
        title = (it.get("title") or "").strip()
        source = (it.get("source") or "").strip()
        published = (it.get("published") or "").strip()
        if not url or not _URL_RE.match(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        if not source:
            try:
                source = urlparse(url).netloc.replace("www.", "")
            except Exception:
                source = ""
        out.append({
            "title": title,
            "url": url,
            "source": source,
            "published": published
        })
        if len(out) >= n:
            break

    if not out:
        raise HTTPException(404, "No links produced by model.")

    # kompatibilitás: az Apps Script 'json.sources'-t olvas
    return {"ok": True, "date_from": d_from, "date_to": d_to, "sources": out, "items": out}

# ===== Health =====
@app.get("/health")
def health():
    return {"ok": True, "gemini": bool(_GEMINI_MODEL)}

# ===== DOCX generátor (változatlan működés) =====
@app.post("/generate")
def generate(p: GeneratePayload):
    if APP_SECRET and (p.secret != APP_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        df = pd.read_csv(csv_url(p.sheet_id, p.worksheet))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Sheet read error: {e}")

    miss = REQUIRED_COLS - set(df.columns)
    if miss:
        raise HTTPException(status_code=400, detail=f"Missing columns: {', '.join(miss)}")

    df = df.dropna(subset=["Rovat", "Link"])
    df = df[df["Rovat"].astype(str).str.strip() == p.rovat]
    df = df[df["Link"].astype(str).str.startswith(("http://","https://"), na=False)]
    if df.empty:
        raise HTTPException(status_code=404, detail=f"No links for rovat '{p.rovat}' on sheet '{p.worksheet}'")

    doc = Document(TEMPLATE_PATH) if os.path.exists(TEMPLATE_PATH) else Document()

    # Főcím
    title_p = doc.add_paragraph()
    try:
        title_p.style = 'Heading 1'
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

        intro_line = doc.add_paragraph()
        r = intro_line.add_run(f"{i+1}. {title}")
        r.bold = True

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

    # „Articles”
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

        ptitle = doc.add_paragraph()
        try:
            ptitle.style = 'Heading 2'
        except Exception:
            pass
        add_bm(ptitle, f"cikk_{i}")
        rr = ptitle.add_run(title)
        rr.bold = True

        dom = urlparse(url).netloc.lower().replace("www.", "")
        doc.add_paragraph(f"Source: {dom}")

        for para in paras:
            doc.add_paragraph(para)

        back = doc.add_paragraph()
        add_link(back, "back to intro >>>", "INTRO")

        if i != len(rows) - 1:
            try:
                doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            except Exception:
                pass

    fname = f"BN_{p.rovat} news_{monday.strftime('%Y%m%d')}.docx"
    buf = io.BytesIO(); doc.save(buf); buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"X-Filename": fname}
    )

# ===== Gemini-alapú link-kereső (EGYETLEN, VÉGLEGES VERZIÓ) =====
def _build_links_prompt(rovat: str, q: str, date_from: str, date_to: str, n: int) -> str:
    return f"""
Feladat: Adj vissza **csak egy JSON tömböt** (kódblokk és kísérőszöveg nélkül) ebben a sémában:
[
  {{ "title": "cikk címe", "url": "https://...", "source": "domain.tld", "published": "YYYY-MM-DD" }},
  ...
]

Követelmények:
- Pontosan {n} különböző link.
- Időablak: CSAK a megadott tartományból (FROM..TO, ISO dátum).
- Fókusz: Magyarországon történt, vagy Magyarországot érdemben érintő hírek.
- Forrásminőség: preferált portálok (portfolio.hu, vg.hu, g7.hu, hvg.hu, telex.hu, index.hu,
  reuters.com, bloomberg.com, ft.com, apnews.com stb.).
- Kerüld: PR/advertorial, paywall preview, duplikált átvételek.
- Téma: igazodjon a rovat témájához: "{rovat}".
- Nyelv: magyar vagy angol.
- Strict JSON – semmi magyarázat.

Paraméterek:
- ROVAT = "{rovat}"
- FROM  = {date_from}
- TO    = {date_to}
- QUERY = {q}
"""

_URL_RE = re.compile(r"^https?://", re.I)

@app.post("/chat")
def chat(p: ChatPayload):
    if not _GEMINI_MODEL:
        raise HTTPException(503, "Gemini API key missing on server.")

    # 1) dátumablak
    if p.date_from and p.date_to:
        d_from, d_to = p.date_from.strip(), p.date_to.strip()
    elif p.worksheet:
        try:
            mon = monday_of(p.worksheet.strip())
            d_from, d_to = week_range_from_monday(mon)  # hétfő–vasárnap
        except Exception:
            d_from, d_to = last_7_days()
    else:
        d_from, d_to = last_7_days()

    # 2) elemszám
    n = max(1, min(int(p.n or 10), 20))

    # 3) prompt
    prompt = _build_links_prompt(
        rovat=p.rovat.strip(),
        q=(p.query or "").strip(),
        date_from=d_from,
        date_to=d_to,
        n=n
    )

    # 4) modell hívás
    try:
        resp = _GEMINI_MODEL.generate_content(prompt)
        txt = (resp.text or "").strip()
    except Exception as e:
        raise HTTPException(500, f"Gemini error: {e}")

    # 5) JSON értelmezés
    import json
    try:
        items = json.loads(txt)
        if not isinstance(items, list):
            raise ValueError("Not a list")
    except Exception:
        raise HTTPException(502, "Model returned non-JSON output.")

    # 6) szűrés/normalizálás
    out = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or "").strip()
        title = (it.get("title") or "").strip()
        source = (it.get("source") or "").strip()
        published = (it.get("published") or "").strip()
        if not url or not _URL_RE.match(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        if not source:
            try:
                source = urlparse(url).netloc.replace("www.", "")
            except Exception:
                source = ""
        out.append({"title": title, "url": url, "source": source, "published": published})
        if len(out) >= n:
            break

    if not out:
        raise HTTPException(404, "No links produced by model.")

    # Apps Script kompatibilitás: 'sources' kell
    return {"ok": True, "date_from": d_from, "date_to": d_to, "sources": out, "items": out}

