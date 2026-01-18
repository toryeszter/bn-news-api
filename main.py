# main.py – BN News DOCX generátor (FastAPI)
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

# ===== Konfiguráció =====
TEMPLATE_PATH = "ceges_sablon.docx"
REQUIRED_COLS = {"Rovat", "Link"}
APP_SECRET = "007"
app = FastAPI()

# ==========================================================
# EGYEDI TISZTÍTÁSI ÉS JAVÍTÓ SZAKASZ (Könnyen kezelhető/törölhető)
# ==========================================================
def apply_custom_cleaning(url: str, title: str, paras: list[str]) -> list[str]:
    cleaned = []
    
    # Economx hírlevél szemét eltávolítása
    junk_strings = [
        "A gazdaság és az üzleti élet legfrissebb hírei az Economx.hu hírlevelében.",
        "Küldtünk Önnek egy emailt!",
        "feliratkozása megerősítéséhez."
    ]

    for p in paras:
        if any(junk in p for junk in junk_strings):
            continue
        cleaned.append(p)
    return cleaned

def fallback_html_parser(html_content: str):
    """
    Ha a trafilatura elbukik a felsorolásoknál (pl. VG.hu), 
    ez a fapados parser kinyeri a p és li tageket.
    """
    tree = html.fromstring(html_content)
    extracted_tags = []
    
    # Keressük a fő tartalmi részt (VG.hu és általános cikkek esetén)
    # A legtöbb hírportál 'article' vagy 'post-content' div-et használ
    main_content = tree.xpath("//article | //div[contains(@class, 'content')] | //div[contains(@class, 'article')]")
    target = main_content[0] if main_content else tree

    # p = bekezdés, li = felsorolás elem
    for el in target.xpath(".//p | .//li"):
        text = norm_space(el.text_content())
        if not text:
            continue
            
        # Ha felsorolás elem, tegyünk elé egy gondolatjelet
        if el.tag == "li":
            text = f"• {text}"
            
        extracted_tags.append(text)
    
    return extracted_tags
# ==========================================================

# ===== Segédek =====
def csv_url(sheet_id: str, sheet_name: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={quote(sheet_name)}"

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

SENT_END_RE = re.compile(r'[.!?…]"?$')
AD_PATTERNS = [
    r"^\s*hirdet[ée]s\b", r"^\s*szponzor[áa]lt\b", r"^\s*tov[áa]bbi (h[íi]reink|cikkek)\b",
    r"^\s*hírlev[ée]l", r"^\s*olvasta m[áa]r\??", r"^\s*fot[óo]gal[ée]ria\b"
]
JUNK_RE = re.compile("|".join(AD_PATTERNS), flags=re.IGNORECASE)

def is_sentence_like(s: str) -> bool:
    s = s.strip()
    return bool(SENT_END_RE.search(s)) or len(s) > 150 or s.startswith("•")

def clean_and_merge(paras: list[str]) -> list[str]:
    lines = []
    for p in paras:
        t = norm_space(p)
        if not t or JUNK_RE.search(t):
            continue
        # Megtartjuk, ha: mondat, felsorolás (•), vagy elég hosszú
        if len(t) > 35 or t.startswith("•") or t.endswith(":"):
            lines.append(t)
    
    merged, buf = [], ""
    for t in lines:
        # Ha felsorolás, ne fűzzük össze az előzővel, legyen külön sor
        if t.startswith("•"):
            if buf: merged.append(buf)
            merged.append(t)
            buf = ""
            continue

        buf = f"{buf} {t}".strip() if buf else t
        if is_sentence_like(buf):
            merged.append(buf)
            buf = ""
    if buf: merged.append(buf)
    return merged

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

def hu_date(d: datetime.date) -> str:
    return d.strftime("%Y.%m.%d.")

# ===== Cikk kinyerés =====
def read_paras(url: str):
    title, raw_html = "", ""
    paras = []
    
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        raw_html = r.text
        
        # Cím kinyerése Readability-vel
        rd = ReadabilityDoc(raw_html)
        title = rd.short_title()
        
        # 1. Próbálkozás: Trafilatura (ha sikerül a felsorolás, ez a legjobb)
        text = trafilatura.extract(raw_html, include_tables=True, favor_recall=True)
        if text:
            # Sortörés mentén daraboljuk, hogy a felsorolások megmaradjanak
            paras = [p for p in text.split("\n") if p.strip()]
        
        # 2. Próbálkozás: Ha a trafilatura túl kevés sort hozott, vagy VG.hu-n vagyunk
        if len(paras) < 5 or "vg.hu" in url:
            paras = fallback_html_parser(raw_html)
            
    except Exception as e:
        print(f"Hiba a letöltésnél ({url}): {e}")

    # Egyedi tisztítás (Economx stb.)
    paras = apply_custom_cleaning(url, title, paras)
    cleaned = clean_and_merge(paras)
    
    return title, cleaned

def pick_lead(paras: list[str]) -> str:
    # A lead ne legyen felsorolás pont
    filtered = [p for p in paras if not p.startswith("•")]
    if not filtered: return paras[0] if paras else ""
    text = filtered[0]
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    lead = parts[0]
    if len(parts) >= 2 and len(lead) < 200:
        lead = f"{lead} {parts[1]}"
    return lead

# ===== Endpoint =====
@app.post("/generate")
def generate(p: Payload):
    if APP_SECRET and (p.secret != APP_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        df = pd.read_csv(csv_url(p.sheet_id, p.worksheet))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Sheet hiba: {e}")
    
    df = df.dropna(subset=["Rovat", "Link"])
    df = df[df["Rovat"].astype(str).str.strip() == p.rovat]
    rows = df.reset_index(drop=True)

    doc = Document(TEMPLATE_PATH) if os.path.exists(TEMPLATE_PATH) else Document()
    
    # Cím és Dátum
    title_p = doc.add_paragraph()
    run = title_p.add_run(f"Weekly News | {p.rovat}")
    run.bold = True
    
    try:
        y, m, d = [int(x) for x in p.worksheet.split("-")]
        monday = datetime.date(y, m, d)
    except: monday = datetime.date.today()
    doc.add_paragraph(hu_date(monday))

    add_bm(doc.add_paragraph(), "INTRO")

    articles_data = []
    for i, row in rows.iterrows():
        url = str(row["Link"]).strip()
        t, p_list = read_paras(url)
        if not t: t = urlparse(url).netloc
        articles_data.append({"url": url, "title": t, "paras": p_list})

    # Intro szekció
    for i, data in enumerate(articles_data):
        intro_line = doc.add_paragraph()
        r = intro_line.add_run(f"{i+1}. {data['title']}")
        r.bold = True
        lead = pick_lead(data['paras'])
        if lead: doc.add_paragraph(lead)
        add_link(doc.add_paragraph(), "read article >>>", f"cikk_{i}")

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # Cikkek szekció
    for i, data in enumerate(articles_data):
        ptitle = doc.add_paragraph()
        add_bm(ptitle, f"cikk_{i}")
        rr = ptitle.add_run(data['title'])
        rr.bold = True
        
        doc.add_paragraph(f"Source: {urlparse(data['url']).netloc}")
        for para in data['paras']:
            doc.add_paragraph(para)
        
        back = doc.add_paragraph()
        add_link(back, "back to intro >>>", "INTRO")
        if i != len(articles_data) - 1:
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return Response(content=buf.read(), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
