from fastapi import FastAPI, Response, HTTPException
from pydantic import BaseModel
from urllib.parse import quote, urlparse
from readability import Document as ReadabilityDoc
from lxml import html
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from docx.enum.text import WD_BREAK
import os, io, re, datetime, requests, pandas as pd
import trafilatura
import google.generativeai as genai
import json

# ===== 1. MODELL DEFINÍCIÓK (A hiba elkerülése végett a tetején) =====

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

# ===== 2. KONFIGURÁCIÓ =====

TEMPLATE_PATH = "ceges_sablon.docx"
REQUIRED_COLS = {"Rovat", "Link"}
APP_SECRET = "007"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

app = FastAPI()

# ===== 3. REKLÁM ÉS JUNK SZŰRŐK (Eredeti + Új kérések) =====

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
    # Economx specifikus
    r"A gazdaság és az üzleti élet legfrissebb hírei az Economx.hu hírlevelében",
    r"Küldtünk Önnek egy emailt!",
    r"feliratkozása megerősítéséhez"
]
JUNK_RE = re.compile("|".join(AD_PATTERNS), flags=re.IGNORECASE)
SENT_END_RE = re.compile(r'[.!?…]"?$')

# ===== 4. SEGÉDFÜGGVÉNYEK (Visszaállítva az eredeti stabil verzióra) =====

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

def is_sentence_like(s: str) -> bool:
    s = s.strip()
    return bool(SENT_END_RE.search(s)) or len(s) > 200

def clean_and_merge(paras: list[str]) -> list[str]:
    """Az eredeti, bevált tisztítási logika."""
    lines = []
    for p in paras:
        t = norm_space(p)
        if not t or JUNK_RE.search(t):
            continue
        # Felsorolások miatt a VG.hu-nál engedékenyebb hossz
        if len(t) < 35 and not t.endswith(":"):
            continue
        lines.append(t)

    merged, buf = [], ""
    for t in lines:
        buf = f"{buf} {t}".strip() if buf else t
        if is_sentence_like(buf):
            merged.append(buf)
            buf = ""
    if buf and len(buf) > 60:
        merged.append(buf)
    return [m for m in merged if not JUNK_RE.search(m)]

def fallback_html_parser(html_content: str):
    """Extra védelem a VG.hu listákhoz, ha az alap kinyerés elbukna."""
    try:
        tree = html.fromstring(html_content)
        main = tree.xpath("//article | //div[contains(@class, 'content')] | //div[contains(@class, 'article')]")
        target = main[0] if main else tree
        extracted = []
        for el in target.xpath(".//p | .//li"):
            text = norm_space(el.text_content())
            if not text or JUNK_RE.search(text): continue
            if el.tag == "li": text = f"• {text}"
            extracted.append(text)
        return extracted
    except: return []

def read_paras(url: str):
    """Visszaállítva az eredeti kettős (Readability + Trafilatura) logika."""
    # 1) Readability
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        rd = ReadabilityDoc(r.text)
        title = (rd.short_title() or "").strip()
        root = html.fromstring(rd.summary())
        paras = [norm_space(el.text_content()) for el in root.xpath(".//p")]
        
        # Speciális eset: ha VG.hu, akkor a listákat is behúzzuk
        if "vg.hu" in url:
            paras = fallback_html_parser(r.text)

        cleaned = clean_and_merge(paras)
        if cleaned:
            return title, cleaned
    except: pass

    # 2) Trafilatura fallback
    try:
        dl = trafilatura.fetch_url(url)
        text = trafilatura.extract(dl, include_comments=False, include_tables=True, favor_recall=True)
        paras = []
        if text:
            blocks = [norm_space(b) for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n"))]
            paras = [b for b in blocks if b]
        
        title = ""
        try:
            meta = trafilatura.extract_metadata(dl)
            if meta and getattr(meta, "title", None):
                title = meta.title.strip()
        except: pass
        
        return title, clean_and_merge(paras)
    except:
        return "", []

def pick_lead(paras: list[str]) -> str:
    if not paras: return ""
    text = paras[0]
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    lead = parts[0] if parts else text
    if len(parts) >= 2 and len(lead) < 220:
        lead = f"{lead} {parts[1]}"
    return lead.strip()

# ===== 5. AI KERESŐ (Gemini 1.5 Flash) =====

def perform_gemini_search(rovat: str, start_date: str, end_date: str):
    if not GEMINI_API_KEY:
        return [{"title": "HIÁNYZIK AZ API KULCS!", "url": ""}]
    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"Magyar gazdasági hírek JSON listaként (title, url) a(z) {rovat} témában {start_date} és {end_date} között."
    try:
        response = model.generate_content(prompt)
        match = re.search(r'\[.*\]', response.text, re.DOTALL)
        return json.loads(match.group()) if match else []
    except: return []

@app.post("/chat")
def chat_endpoint(p: ChatPayload):
    if APP_SECRET and (p.secret != APP_SECRET): raise HTTPException(status_code=401)
    try:
        ref = datetime.datetime.strptime(p.worksheet, "%Y-%m-%d")
        s, e = (ref - datetime.timedelta(days=7)).strftime("%Y-%m-%d"), (ref - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    except: s, e = "utóbbi 7 nap", "ma"
    return {"sources": perform_gemini_search(p.rovat, s, e)}

# ===== 6. DOCX ÉS ENDPOINT (Eredeti stílus) =====

def add_bm(paragraph, name: str):
    run = paragraph.add_run()
    r = run._r
    bs, be = OxmlElement("w:bookmarkStart"), OxmlElement("w:bookmarkEnd")
    bs.set(qn("w:id"), "1"); bs.set(qn("w:name"), name); be.set(qn("w:id"), "1")
    r.append(bs); r.append(be)

def add_link(paragraph, text: str, anchor: str):
    h = OxmlElement("w:hyperlink"); h.set(qn("w:anchor"), anchor)
    r = OxmlElement("w:r"); rPr = OxmlElement("w:rPr")
    u = OxmlElement("w:u"); u.set(qn("w:val"), "single")
    c = OxmlElement("w:color"); c.set(qn("w:val"), "0000FF")
    rPr.append(u); rPr.append(c); t = OxmlElement("w:t"); t.text = text
    r.append(rPr); r.append(t); h.append(r); paragraph._p.append(h)

@app.post("/generate")
def generate(p: Payload):
    if APP_SECRET and (p.secret != APP_SECRET): raise HTTPException(status_code=401)
    try:
        csv_path = f"https://docs.google.com/spreadsheets/d/{p.sheet_id}/gviz/tq?tqx=out:csv&sheet={quote(p.worksheet)}"
        df = pd.read_csv(csv_path).dropna(subset=["Rovat", "Link"])
        df = df[df["Rovat"].astype(str).str.strip() == p.rovat]
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

    doc = Document(TEMPLATE_PATH) if os.path.exists(TEMPLATE_PATH) else Document()
    doc.add_paragraph(f"Weekly News | {p.rovat}").bold = True
    doc.add_paragraph(p.worksheet)
    add_bm(doc.add_paragraph(), "INTRO")

    articles = []
    for i, row in df.iterrows():
        t, ps = read_paras(str(row["Link"]))
        articles.append({"title": t or "Cím nélkül", "url": row["Link"], "paras": ps})

    for i, a in enumerate(articles):
        p_i = doc.add_paragraph()
        p_i.add_run(f"{i+1}. {a['title']}").bold = True
        lead = pick_lead(a["paras"])
        if lead: doc.add_paragraph(lead)
        add_link(doc.add_paragraph(), "read article >>>", f"cikk_{i}")

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    for i, a in enumerate(articles):
        tp = doc.add_paragraph()
        add_bm(tp, f"cikk_{i}")
        tp.add_run(a["title"]).bold = True
        doc.add_paragraph(f"Source: {urlparse(str(a['url'])).netloc}")
        for para in a["paras"]: doc.add_paragraph(para)
        add_link(doc.add_paragraph(), "back to intro >>>", "INTRO")
        if i < len(articles)-1: doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return Response(content=buf.read(), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"X-Filename": f"BN_{p.rovat}.docx"})
