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

# ===== 3. REKLÁM ÉS JUNK SZŰRŐK (Minden kért elem benne van) =====

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
    # Economx specifikus törlendő sorok:
    r"A gazdaság és az üzleti élet legfrissebb hírei az Economx.hu hírlevelében",
    r"Küldtünk Önnek egy emailt!",
    r"feliratkozása megerősítéséhez"
]
JUNK_RE = re.compile("|".join(AD_PATTERNS), flags=re.IGNORECASE)

# ===== 4. AI KERESŐ (Gemini 1.5 Flash + Search) =====

def perform_gemini_search(rovat: str, start_date: str, end_date: str):
    if not GEMINI_API_KEY:
        return [{"title": "Hiba: Hiányzó GEMINI_API_KEY a szerveren!", "url": ""}]

    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt = f"""
    Feladat: Keress 6-10 releváns magyar nyelvű gazdasági hírt a '{rovat}' témában.
    Időszak: {start_date} és {end_date} között publikált cikkek.
    Források: vg.hu, economx.hu, portfolio.hu, telex.hu, hvg.hu.
    
    A választ KIZÁRÓLAG egy JSON listaként add meg:
    [
      {{"title": "Cikk pontos címe", "url": "https://link-a-cikkre.hu"}},
      ...
    ]
    """

    try:
        response = model.generate_content(prompt)
        match = re.search(r'\[.*\]', response.text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []
    except Exception as e:
        print(f"Gemini hiba: {e}")
        return []

@app.post("/chat")
def chat_endpoint(p: ChatPayload):
    if APP_SECRET and (p.secret != APP_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        ref_date = datetime.datetime.strptime(p.worksheet, "%Y-%m-%d")
        start_dt = (ref_date - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        end_dt = (ref_date - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    except:
        start_dt, end_dt = "last 7 days", "today"

    found_sources = perform_gemini_search(p.rovat, start_dt, end_dt)
    return {"sources": found_sources}

# ===== 5. TISZTÍTÁS ÉS KINYERÉS (VG.hu felsorolás-fixszel) =====

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

def clean_and_merge(paras: list[str]) -> list[str]:
    lines = []
    for p in paras:
        t = norm_space(p)
        if not t or JUNK_RE.search(t):
            continue
        # Rövid sorok kezelése: megtartjuk, ha felsorolás (•) vagy alcímszerű (:)
        if len(t) > 35 or t.startswith("•") or t.endswith(":"):
            lines.append(t)
    
    merged, buf = [], ""
    for t in lines:
        if t.startswith("•"):
            if buf: merged.append(buf)
            merged.append(t)
            buf = ""
            continue
        buf = f"{buf} {t}".strip() if buf else t
        if len(buf) > 160 or (buf and buf[-1] in ".!?…"):
            merged.append(buf)
            buf = ""
    if buf: merged.append(buf)
    return merged

def fallback_html_parser(html_content: str):
    """Kényszerített kinyerés a VG.hu-s felsorolásokhoz."""
    try:
        tree = html.fromstring(html_content)
        main_content = tree.xpath("//article | //div[contains(@class, 'content')] | //div[contains(@class, 'article')]")
        target = main_content[0] if main_content else tree
        extracted = []
        for el in target.xpath(".//p | .//li"):
            text = norm_space(el.text_content())
            if not text or JUNK_RE.search(text): continue
            if el.tag == "li": text = f"• {text}"
            extracted.append(text)
        return extracted
    except: return []

def read_paras(url: str):
    title, paras = "", []
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        raw_html = r.text
        rd = ReadabilityDoc(raw_html)
        title = rd.short_title()
        
        text = trafilatura.extract(raw_html, include_tables=True, favor_recall=True)
        if text:
            paras = [p for p in text.split("\n") if p.strip()]
        
        # Ha VG.hu vagy gyanúsan kevés szöveg, jön a biztosabb parser
        if len(paras) < 5 or "vg.hu" in url:
            paras = fallback_html_parser(raw_html)
    except: pass
    return title, clean_and_merge(paras)

# ===== 6. DOCX GENERÁLÁS ÉS SEGÉDEK =====

def add_bm(paragraph, name: str):
    run = paragraph.add_run()
    r = run._r
    bs, be = OxmlElement("w:bookmarkStart"), OxmlElement("w:bookmarkEnd")
    bs.set(qn("w:id"), "1"); bs.set(qn("w:name"), name)
    be.set(qn("w:id"), "1")
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
    if APP_SECRET and (p.secret != APP_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        sheet_url = f"https://docs.google.com/spreadsheets/d/{p.sheet_id}/gviz/tq?tqx=out:csv&sheet={quote(p.worksheet)}"
        df = pd.read_csv(sheet_url).dropna(subset=["Rovat", "Link"])
        df = df[df["Rovat"].astype(str).str.strip() == p.rovat]
        rows = df.reset_index(drop=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Sheet hiba: {e}")

    if rows.empty:
        raise HTTPException(status_code=404, detail="Nincs adat ehhez a rovathoz.")

    doc = Document(TEMPLATE_PATH) if os.path.exists(TEMPLATE_PATH) else Document()
    doc.add_paragraph(f"Weekly News | {p.rovat}").bold = True
    doc.add_paragraph(f"Sheet: {p.worksheet}")
    add_bm(doc.add_paragraph(), "INTRO")

    articles = []
    for i, row in rows.iterrows():
        t, p_list = read_paras(str(row["Link"]))
        articles.append({"title": t or "Cím nélkül", "url": row["Link"], "paras": p_list})

    # Intro (Kivonat)
    for i, a in enumerate(articles):
        p_i = doc.add_paragraph()
        p_i.add_run(f"{i+1}. {a['title']}").bold = True
        if a["paras"]:
            doc.add_paragraph(a["paras"][0][:250] + "...")
        add_link(doc.add_paragraph(), "read article >>>", f"cikk_{i}")

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # Cikkek törzse
    for i, a in enumerate(articles):
        tp = doc.add_paragraph()
        add_bm(tp, f"cikk_{i}")
        tp.add_run(a["title"]).bold = True
        doc.add_paragraph(f"Source: {urlparse(str(a['url'])).netloc}")
        for txt in a["paras"]:
            doc.add_paragraph(txt)
        back = doc.add_paragraph()
        add_link(back, "back to intro >>>", "INTRO")
        if i < len(articles) - 1:
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"X-Filename": f"BN_{p.rovat}_{p.worksheet}.docx"}
    )
