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
    worksheet: str  # A Spreadsheetből jövő dátum (YYYY-MM-DD)
    rovat: str
    query: str | None = ""
    secret: str | None = None

# ===== 2. KONFIGURÁCIÓ =====

TEMPLATE_PATH = "ceges_sablon.docx"
APP_SECRET = "007"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

app = FastAPI()

# ==========================================================
# 3. AI KERESŐ SZEKCIÓ (Gemini 1.5 Flash + Web Search)
# ==========================================================

def perform_gemini_search(rovat: str, start_date: str, end_date: str):
    """
    Gemini API használata hírek keresésére a megadott időszakban.
    """
    if not GEMINI_API_KEY:
        return [{"title": "Hiba: Hiányzó API kulcs", "url": ""}]

    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt = f"""
    Kérlek keress releváns magyar nyelvű gazdasági és üzleti híreket a(z) '{rovat}' szektorral kapcsolatban.
    Időszak: {start_date} és {end_date} között.
    Csak megbízható forrásokat használj (pl. vg.hu, economx.hu, portfolio.hu, telex, hvg).
    
    A választ kizárólag egy JSON listaként add meg, az alábbi formátumban, egyéb szöveg nélkül:
    [
      {{"title": "Cikk címe", "url": "https://link-a-cikkre.hu"}},
      ...
    ]
    Minimum 5, maximum 10 cikket gyűjts össze.
    """

    try:
        # Itt a 'tools' paraméterrel aktiváljuk a Google Search-öt (ha a modell támogatja)
        response = model.generate_content(prompt)
        
        # JSON kinyerése a válaszból
        clean_json = re.search(r'\[.*\]', response.text, re.DOTALL)
        if clean_json:
            return json.loads(clean_json.group())
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
        # Előző hét hétfőtől vasárnapig
        start_dt = (ref_date - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        end_dt = (ref_date - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    except:
        start_dt = "last 7 days"
        end_dt = "today"

    found_sources = perform_gemini_search(p.rovat, start_dt, end_dt)
    return {"sources": found_sources}

# ==========================================================
# 4. TISZTÍTÓ ÉS PARSER LOGIKA (A DOCX-hez)
# ==========================================================

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

def apply_custom_cleaning(paras: list[str]) -> list[str]:
    cleaned = []
    junk_strings = [
        "A gazdaság és az üzleti élet legfrissebb hírei az Economx.hu hírlevelében.",
        "Küldtünk Önnek egy emailt!",
        "feliratkozása megerősítéséhez."
    ]
    for p in paras:
        t = norm_space(p)
        if not t or any(junk in t for junk in junk_strings):
            continue
        cleaned.append(t)
    return cleaned

def fallback_html_parser(html_content: str):
    try:
        tree = html.fromstring(html_content)
        extracted_tags = []
        main_content = tree.xpath("//article | //div[contains(@class, 'content')] | //div[contains(@class, 'article')]")
        target = main_content[0] if main_content else tree
        
        for el in target.xpath(".//p | .//li"):
            text = norm_space(el.text_content())
            if not text or len(text) < 20: continue
            if el.tag == "li": text = f"• {text}"
            extracted_tags.append(text)
        return extracted_tags
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
        
        if len(paras) < 5 or "vg.hu" in url:
            paras = fallback_html_parser(raw_html)
    except: pass
    return title, apply_custom_cleaning(paras)

# ==========================================================
# 5. DOCX ÉS SEGÉDFÜGGVÉNYEK
# ==========================================================

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

def csv_url(sheet_id: str, sheet_name: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={quote(sheet_name)}"

# ==========================================================
# 6. GENERÁLÓ ENDPOINT
# ==========================================================

@app.post("/generate")
def generate(p: Payload):
    if APP_SECRET and (p.secret != APP_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        df = pd.read_csv(csv_url(p.sheet_id, p.worksheet))
        df = df.dropna(subset=["Rovat", "Link"])
        df = df[df["Rovat"].astype(str).str.strip() == p.rovat]
        rows = df.reset_index(drop=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Sheet hiba: {e}")

    if rows.empty:
        raise HTTPException(status_code=404, detail="Nincs adat a rovathoz.")

    doc = Document(TEMPLATE_PATH) if os.path.exists(TEMPLATE_PATH) else Document()
    
    # Cím és Dátum
    title_p = doc.add_paragraph()
    title_p.add_run(f"Weekly News | {p.rovat}").bold = True
    doc.add_paragraph(p.worksheet)

    add_bm(doc.add_paragraph(), "INTRO")

    articles_data = []
    for i, row in rows.iterrows():
        url = str(row["Link"]).strip()
        t, p_list = read_paras(url)
        if not t: t = urlparse(url).netloc or "Cím nélkül"
        articles_data.append({"title": t, "url": url, "paras": p_list})

    # Intro
    for i, data in enumerate(articles_data):
        p_intro = doc.add_paragraph()
        p_intro.add_run(f"{i+1}. {data['title']}").bold = True
        if data["paras"]:
            doc.add_paragraph(data["paras"][0][:250] + "...")
        add_link(doc.add_paragraph(), "read article >>>", f"cikk_{i}")

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # Cikkek
    for i, data in enumerate(articles_data):
        t_p = doc.add_paragraph()
        add_bm(t_p, f"cikk_{i}")
        t_p.add_run(data["title"]).bold = True
        doc.add_paragraph(f"Source: {urlparse(data['url']).netloc}")
        for para in data["paras"]:
            doc.add_paragraph(para)
        add_link(doc.add_paragraph(), "back to intro >>>", "INTRO")
        if i < len(articles_data) - 1:
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"X-Filename": f"BN_{p.rovat}_{p.worksheet}.docx"}
    )
