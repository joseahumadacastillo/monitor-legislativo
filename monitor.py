import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import date, datetime, timedelta
import pdfplumber
import io
import re
import json
import os
import tempfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import gspread
from google.oauth2.service_account import Credentials
import base64
from email.mime.base import MIMEBase
from email import encoders
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, Image
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

# ── CONFIG ──────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
GMAIL_ACCOUNT  = os.environ["GMAIL_ACCOUNT"]
GMAIL_PASS     = os.environ["GMAIL_PASS"]
SHEET_ID       = os.environ["SHEET_ID"]
GOOGLE_CREDS   = json.loads(os.environ["GOOGLE_CREDENTIALS"])
# Destinatarios separados por coma (por defecto, la misma cuenta que envía)
CORREOS        = [c.strip() for c in os.environ.get("DESTINATARIOS", GMAIL_ACCOUNT).split(",") if c.strip()]

# Palabras clave configurables con la variable KEYWORDS (separadas por coma)
KEYWORDS = [k.strip() for k in os.environ.get(
    "KEYWORDS",
    "SAT,Digitalización,Inteligencia Artificial,Fintech,ATDT,devoluciones,informalidad"
).split(",") if k.strip()]

MESES_ES = {
    1:"ene",2:"feb",3:"mar",4:"abr",5:"may",6:"jun",
    7:"jul",8:"ago",9:"sep",10:"oct",11:"nov",12:"dic"
}

# ── SCRAPER ──────────────────────────────────────────────────────────────────
def url_por_fecha(dt):
    return f"https://gaceta.diputados.gob.mx/Gaceta/66/{dt.year}/{MESES_ES[dt.month]}/{dt.strftime('%Y%m%d')}.html"

def tiene_keyword(texto, keywords):
    texto_lower = texto.lower()
    for kw in keywords:
        if kw.lower() in texto_lower:
            return True
    return False

def detectar_seccion(texto):
    t = texto.lower()
    if "acuerdo" in t:      return "Acuerdos"
    if "convocatoria" in t: return "Convocatorias"
    if "acta" in t:         return "Actas"
    if "invitaci" in t:     return "Invitaciones"
    if "iniciativa" in t:   return "Iniciativas"
    if "dictamen" in t:     return "Dictámenes"
    if "anexo" in t:        return "Anexos"
    if "minuta" in t:       return "Minutas"
    return "General"

def parsear_indice(soup, fecha_str):
    titulo = soup.title.text.strip() if soup.title else ""
    resultados = []
    seccion_actual = "General"
    indice = soup.find("div", id="Indice") or soup
    for tag in indice.find_all(["a","li"]):
        texto = " ".join(tag.get_text(separator=" ", strip=True).split())
        if not texto or len(texto) < 15:
            continue
        sec = detectar_seccion(texto)
        if tag.name == "a":
            href = tag.get("href","")
            if href.startswith("#") and sec:
                seccion_actual = sec
            continue
        if tag.name == "li":
            link = ""
            for a in tag.find_all("a", href=True):
                href = a["href"]
                if not href.startswith("#"):
                    link = href if href.startswith("http") else "https://gaceta.diputados.gob.mx" + href
                    break
            resultados.append({
                "seccion": seccion_actual,
                "texto": texto[:400],
                "link": link,
                "fecha": fecha_str,
            })
    df = pd.DataFrame(resultados if resultados else [])
    if not df.empty:
        df = df.drop_duplicates(subset=["texto"])
    return df, titulo

def extraer_texto_pdf(url_pdf):
    try:
        resp = requests.get(url_pdf, headers={"User-Agent":"Mozilla/5.0"}, timeout=30, verify=False)
        if resp.status_code != 200:
            return ""
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            texto = ""
            for page in pdf.pages[:15]:
                t = page.extract_text()
                if t:
                    texto += t + "\n"
        return texto[:8000]
    except Exception:
        return ""

def extraer_texto_para_titulo(soup, titulo_item):
    div = soup.find("div", id="Contenido")
    if not div:
        return ""
    texto_completo = div.get_text(separator="\n", strip=True)
    lineas = texto_completo.split("\n")
    palabras = titulo_item.split()
    
    # Palabras que indican inicio de nueva sección — parar ahí
    SECCIONES = ["acuerdos", "convocatorias", "actas", "invitaciones", 
                 "iniciativas", "dictámenes", "anexos", "minutas", "orden del día"]
    
    idx_inicio = -1
    for n in [6, 4]:
        busqueda = " ".join(palabras[:n]).lower()
        for i, linea in enumerate(lineas):
            if busqueda in linea.lower():
                idx_inicio = i
                break
        if idx_inicio >= 0:
            break
    
    if idx_inicio < 0:
        return ""
    
    # Tomar líneas hasta la siguiente sección o máximo 150
    bloque = []
    for linea in lineas[idx_inicio:idx_inicio+150]:
        # Si encontramos inicio de nueva sección después de las primeras 3 líneas, parar
        if len(bloque) > 3 and any(s in linea.lower() for s in SECCIONES):
            break
        bloque.append(linea)
    
    return "\n".join(bloque)[:6000]

def resumir_con_gpt(titulo_item, texto, keywords_enc):
    if not texto or len(texto) < 50:
        return None
    try:
        prompt = f"""Eres un asistente legislativo que resume documentos mexicanos.

INSTRUCCIONES ESTRICTAS:
1. Resume ÚNICAMENTE lo que dice el TEXTO DEL DOCUMENTO. NO inventes. NO uses conocimiento externo.
2. Si algo no está en el texto escribe: "No se menciona en el documento".
3. keywords_encontradas: SOLO las de esta lista exacta que aparezcan en el texto: {', '.join(keywords_enc)}
4. nivel_relevancia = "alta" SOLO si trata de: ISR, IVA, SAT, declaración anual, devoluciones, obligaciones fiscales personas físicas, fintech fiscal. Todo lo demás = "baja".
5. presentadores: Busca en el texto quién presenta, firma, remite o es autor. Puede ser un legislador, la SHCP, el SAT, el Ejecutivo, una comisión, una secretaría, etc. Si el título menciona una institución como remitente, úsala. Si no encuentras nada, pon el nombre de la comisión más relevante del texto.

Responde SOLO JSON válido sin backticks:
{{
  "resumen": "2-3 oraciones del texto",
  "tema_principal": "máximo 8 palabras del texto",
  "relevancia": "explicación del texto",
  "nivel_relevancia": "alta|media|baja",
  "keywords_encontradas": ["solo keywords de la lista que están en el texto"],
  "presentadores": [{{"nombre": "del texto", "partido": "si aparece", "camara": "Diputados|Senado|SHCP|SAT|Ejecutivo|Otro", "rol": "presentador|remitente"}}],
  "comisiones": ["del texto"]
}}

Título: {titulo_item}
Keywords a buscar (SOLO estas): {', '.join(keywords_enc)}

TEXTO (resume SOLO esto):
{texto[:5000]}"""

        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Content-Type":"application/json","Authorization":f"Bearer {OPENAI_API_KEY}"},
            json={"model":"gpt-4o-mini","max_tokens":800,"temperature":0,
                  "messages":[{"role":"user","content":prompt}]},
            timeout=30, verify=False
        )
        txt = resp.json()["choices"][0]["message"]["content"].strip()
        txt = re.sub(r'^```json\s*|\s*```$', '', txt)
        result = json.loads(txt)
        result["titulo_pdf"] = titulo_item
        return result
    except Exception as e:
        print(f"Error GPT: {e}")
        return None

def scrape_gaceta():
    headers = {"User-Agent": "Mozilla/5.0"}
    soup = None
    df = pd.DataFrame()
    titulo = ""
    base = datetime.today()

    for delta in range(0, 1):  # Solo busca hoy
        dt_try = base - timedelta(days=delta)
        if dt_try.weekday() >= 5:
            continue
        url = url_por_fecha(dt_try)
        try:
            resp = requests.get(url, headers=headers, timeout=10, verify=False)
            if resp.status_code == 200 and len(resp.text) > 2000:
                soup = BeautifulSoup(resp.text, "html.parser")
                df, titulo = parsear_indice(soup, dt_try.strftime("%Y-%m-%d"))
                if not df.empty:
                    print(f"Gaceta encontrada: {url}")
                    break
        except Exception:
            continue

    if df.empty or soup is None:
        print("No se encontró Gaceta hoy")
        return [], [], "", None

    # Keywords solo en títulos del índice
    no_anexos = df[df["seccion"] != "Anexos"].copy()
    mask = no_anexos["texto"].apply(lambda t: tiene_keyword(t, KEYWORDS))
    df_keywords = no_anexos[mask].copy()
    print(f"Items con keywords: {len(df_keywords)}")

    resumenes = []

    # Anexos PDF con keyword en el párrafo
    div_anexos = soup.find("div", id="Anexos")
    if div_anexos:
        for p in div_anexos.find_all("p"):
            texto_p = " ".join(p.get_text(separator=" ", strip=True).split())
            a = p.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            if not (href.endswith(".pdf") or "PDF" in href):
                continue
            if tiene_keyword(texto_p, KEYWORDS):
                url_pdf = href if href.startswith("http") else "https://gaceta.diputados.gob.mx" + href
                texto_pdf = extraer_texto_pdf(url_pdf)
                kws_enc = [kw for kw in KEYWORDS if kw.lower() in texto_p.lower()]
                resumen = resumir_con_gpt(texto_p, texto_pdf, kws_enc)
                if resumen:
                    resumen["url_pdf"] = url_pdf
                    resumenes.append(resumen)

    # Items del índice con keyword en título
    for _, row in df_keywords.iterrows():
        titulo_item = row["texto"]
        kws_enc = [kw for kw in KEYWORDS if kw.lower() in titulo_item.lower()]
        texto_item = extraer_texto_para_titulo(soup, titulo_item)
        texto_sin_titulo = texto_item.replace(titulo_item, "").strip()
        if not texto_sin_titulo or len(texto_sin_titulo) < 200:
            resumenes.append({
                "titulo_pdf": titulo_item,
                "tema_principal": titulo_item,
                "resumen": "Texto insuficiente para resumen.",
                "nivel_relevancia": "baja",
                "relevancia": "No se puede determinar sin el texto completo.",
                "keywords_encontradas": kws_enc,
                "presentadores": [],
                "comisiones": [],
                "url_pdf": row["link"] if row["link"] else ""
            })
            continue
        resumen = resumir_con_gpt(titulo_item, texto_item, kws_enc)
        if resumen:
            resumen["url_pdf"] = row["link"] if row["link"] else ""
            resumenes.append(resumen)

    return df_keywords.to_dict("records"), resumenes, titulo, soup

# ── PDF ───────────────────────────────────────────────────────────────────────
def generar_pdf(df_keywords, resumenes, titulo_gaceta):
    buf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    doc = SimpleDocTemplate(buf.name, pagesize=letter,
                            leftMargin=0.75*inch, rightMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)

    # Paleta de colores
    VERDE   = colors.HexColor("#3A7C22")
    AZUL    = colors.HexColor("#1F3A5F")
    GRIS    = colors.HexColor("#555555")
    ROJO    = colors.HexColor("#c0392b")
    NARANJA = colors.HexColor("#e67e22")
    AZUL_L  = colors.HexColor("#2980b9")
    FONDO   = colors.HexColor("#f5f7fa")

    styles = getSampleStyleSheet()
    estilo_normal = ParagraphStyle("normal", fontName="Helvetica", fontSize=10, textColor=colors.HexColor("#1a1a1a"), leading=14)
    estilo_titulo = ParagraphStyle("titulo", fontName="Helvetica-Bold", fontSize=13, textColor=AZUL, leading=16, spaceAfter=4)
    estilo_label  = ParagraphStyle("label",  fontName="Helvetica-Bold", fontSize=8,  textColor=GRIS, leading=10, spaceBefore=6)
    estilo_valor  = ParagraphStyle("valor",  fontName="Helvetica",      fontSize=10, textColor=colors.HexColor("#1a1a1a"), leading=13)
    estilo_meta   = ParagraphStyle("meta",   fontName="Helvetica",      fontSize=9,  textColor=GRIS, leading=12)
    estilo_kw     = ParagraphStyle("kw",     fontName="Helvetica-Oblique", fontSize=9, textColor=AZUL, leading=12)

    story = []
    W = letter[0] - 1.5*inch

    # ── PORTADA ──
    # Logo
    import os
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
    if os.path.exists(logo_path):
        img = Image(logo_path, width=2.2*inch, height=0.7*inch)
        story.append(img)
        story.append(Spacer(1, 0.2*inch))

    # Línea verde
    story.append(HRFlowable(width="100%", thickness=3, color=VERDE, spaceAfter=12))

    # Título
    story.append(Paragraph("Reporte Monitor Legislativo MX", ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=22, textColor=AZUL, leading=26)))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"{titulo_gaceta}", estilo_meta))
    story.append(Paragraph(f"Fecha de análisis: <b>{date.today()}</b>", estilo_meta))
    story.append(Spacer(1, 0.15*inch))

    # Resumen ejecutivo en tabla
    n_alta  = sum(1 for r in resumenes if r.get("nivel_relevancia","").lower() == "alta")
    n_media = sum(1 for r in resumenes if r.get("nivel_relevancia","").lower() == "media")
    n_baja  = sum(1 for r in resumenes if r.get("nivel_relevancia","").lower() == "baja")

    resumen_data = [
        [Paragraph("<b>Con keywords</b>", estilo_label), Paragraph("<b>Alta</b>", estilo_label), Paragraph("<b>Media</b>", estilo_label), Paragraph("<b>Baja</b>", estilo_label)],
        [Paragraph(f"<b>{len(df_keywords)}</b>", ParagraphStyle("big", fontName="Helvetica-Bold", fontSize=20, textColor=AZUL, leading=24, alignment=TA_CENTER)),
         Paragraph(f"<b>{n_alta}</b>",  ParagraphStyle("big2", fontName="Helvetica-Bold", fontSize=20, textColor=ROJO,   leading=24, alignment=TA_CENTER)),
         Paragraph(f"<b>{n_media}</b>", ParagraphStyle("big3", fontName="Helvetica-Bold", fontSize=20, textColor=NARANJA, leading=24, alignment=TA_CENTER)),
         Paragraph(f"<b>{n_baja}</b>",  ParagraphStyle("big4", fontName="Helvetica-Bold", fontSize=20, textColor=AZUL_L, leading=24, alignment=TA_CENTER))],
    ]
    t = Table(resumen_data, colWidths=[W/4]*4)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), FONDO),
        ("BACKGROUND", (0,1), (-1,1), colors.white),
        ("BOX",        (0,0), (-1,-1), 1, colors.HexColor("#dddddd")),
        ("INNERGRID",  (0,0), (-1,-1), 0.5, colors.HexColor("#dddddd")),
        ("ALIGN",      (0,0), (-1,-1), "CENTER"),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("ROUNDEDCORNERS", [4]),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.2*inch))

    # Keywords
    kws_str = ", ".join(KEYWORDS)
    story.append(Paragraph(f"Keywords monitoreadas: {kws_str}", estilo_kw))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#dddddd"), spaceBefore=12, spaceAfter=12))

    # ── TABLA DE DOCUMENTOS ──
    story.append(Paragraph("Documentos con keywords", ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=14, textColor=AZUL, leading=18)))
    story.append(Spacer(1, 6))

    if df_keywords:
        headers = [Paragraph("<b>Sección</b>", estilo_label), Paragraph("<b>Documento</b>", estilo_label), Paragraph("<b>Link</b>", estilo_label)]
        rows = [headers]
        for item in df_keywords:
            link = item.get("link","")
            txt  = item.get("texto","")[:120]
            sec  = item.get("seccion","")
            link_p = Paragraph(f'<a href="{link}" color="#1a3a5c">Ver</a>' if link else "—", estilo_normal)
            rows.append([Paragraph(sec, estilo_normal), Paragraph(txt, estilo_normal), link_p])
        t2 = Table(rows, colWidths=[1.2*inch, W-2.2*inch, 1*inch])
        t2.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), AZUL),
            ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
            ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, FONDO]),
            ("BOX",           (0,0), (-1,-1), 0.5, colors.HexColor("#dddddd")),
            ("INNERGRID",     (0,0), (-1,-1), 0.5, colors.HexColor("#dddddd")),
            ("ALIGN",         (0,0), (-1,-1), "LEFT"),
            ("VALIGN",        (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",    (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ]))
        story.append(t2)
    else:
        story.append(Paragraph("No se encontraron documentos con keywords.", estilo_normal))

    story.append(Spacer(1, 0.3*inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#dddddd"), spaceAfter=12))

    # ── FICHAS DETALLADAS ──
    story.append(Paragraph("Análisis IA por documento", ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=14, textColor=AZUL, leading=18)))
    story.append(Spacer(1, 8))

    for res in resumenes:
        nivel = res.get("nivel_relevancia","baja").lower()
        color_nivel = {"alta": ROJO, "media": NARANJA, "baja": AZUL_L}.get(nivel, AZUL_L)
        titulo_item = res.get("titulo_pdf","")
        tema    = res.get("tema_principal","")
        resumen = res.get("resumen","")
        rel     = res.get("relevancia","")
        if not rel or "no se menciona" in rel.lower():
            rel = f"Baja relevancia — el documento trata sobre: {tema}"
        legs = res.get("presentadores",[])
        presenta = ""
        if legs:
            n = legs[0].get("nombre",""); c = legs[0].get("camara","")
            if n: presenta = f"{n} ({c})"
        kws_enc = ", ".join(res.get("keywords_encontradas",[]))
        url_pdf = res.get("url_pdf","")

        # Badge de nivel
        badge_data = [[Paragraph(f"<b>{nivel.upper()}</b>", ParagraphStyle("badge", fontName="Helvetica-Bold", fontSize=9, textColor=colors.white, leading=11, alignment=TA_CENTER))]]
        badge = Table(badge_data, colWidths=[0.7*inch])
        badge.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), color_nivel),
            ("ROUNDEDCORNERS", [4]),
            ("TOPPADDING",    (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ]))

        ficha = []
        ficha.append(badge)
        ficha.append(Spacer(1, 4))
        ficha.append(Paragraph(titulo_item, estilo_titulo))
        ficha.append(Paragraph("TEMA", estilo_label))
        ficha.append(Paragraph(tema, estilo_valor))
        ficha.append(Paragraph("RESUMEN", estilo_label))
        ficha.append(Paragraph(resumen, estilo_valor))
        ficha.append(Paragraph("RELEVANCIA", estilo_label))
        ficha.append(Paragraph(rel, estilo_valor))
        if presenta:
            ficha.append(Paragraph("PRESENTA", estilo_label))
            ficha.append(Paragraph(presenta, estilo_valor))
        if kws_enc:
            ficha.append(Paragraph("KEYWORDS ENCONTRADAS", estilo_label))
            ficha.append(Paragraph(kws_enc, estilo_kw))
        if url_pdf:
            ficha.append(Spacer(1, 4))
            ficha.append(Paragraph(f'<a href="{url_pdf}" color="#1a3a5c">Ver documento completo</a>', estilo_normal))
        else:
            ficha.append(Spacer(1, 4))
            ficha.append(Paragraph(f'<a href="https://gaceta.diputados.gob.mx/gp_hoy.html" color="#1a3a5c">Ver Gaceta Parlamentaria</a>', estilo_normal))

        ficha_table = Table([[ficha]], colWidths=[W])
        ficha_table.setStyle(TableStyle([
            ("BOX",           (0,0), (-1,-1), 1, colors.HexColor("#dddddd")),
            ("LEFTPADDING",   (0,0), (-1,-1), 12),
            ("RIGHTPADDING",  (0,0), (-1,-1), 12),
            ("TOPPADDING",    (0,0), (-1,-1), 10),
            ("BOTTOMPADDING", (0,0), (-1,-1), 10),
            ("BACKGROUND",    (0,0), (-1,-1), colors.white),
        ]))
        story.append(ficha_table)
        story.append(Spacer(1, 10))

    # Footer
    story.append(HRFlowable(width="100%", thickness=1, color=VERDE, spaceBefore=12))
    story.append(Paragraph(f"Monitor Legislativo MX &nbsp;·&nbsp; Generado automáticamente &nbsp;·&nbsp; {date.today()}", 
                           ParagraphStyle("footer", fontName="Helvetica", fontSize=8, textColor=GRIS, alignment=TA_CENTER, leading=10)))

    doc.build(story)
    with open(buf.name, "rb") as f:
        pdf_bytes = f.read()
    return pdf_bytes


# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
def guardar_en_sheets(resumenes, titulo_gaceta):
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(GOOGLE_CREDS, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.sheet1
        url_gaceta = "https://gaceta.diputados.gob.mx/gp_hoy.html"
        filas = []
        for res in resumenes:
            legs = res.get("presentadores", [])
            nombre  = legs[0].get("nombre","")  if legs else ""
            partido = legs[0].get("partido","") if legs else ""
            camara  = legs[0].get("camara","")  if legs else ""
            kws  = ", ".join(res.get("keywords_encontradas", []))
            coms = ", ".join(res.get("comisiones", []))
            rel  = res.get("relevancia","")
            if not rel or "no se menciona" in rel.lower():
                rel = f"Baja relevancia — el documento trata sobre: {res.get('tema_principal','')}"
            filas.append([
                str(date.today()),
                res.get("titulo_pdf",""),
                res.get("tema_principal",""),
                res.get("resumen",""),
                res.get("nivel_relevancia","").upper(),
                rel,
                kws,
                nombre, partido, camara,
                coms,
                url_gaceta,
                res.get("url_pdf","")
            ])
        if filas:
            ws.append_rows(filas)
            print(f"Guardadas {len(filas)} filas en Sheets")
    except Exception as e:
        print(f"Error Sheets: {e}")

# ── EMAIL ─────────────────────────────────────────────────────────────────────
def enviar_email(df_keywords, resumenes, titulo_gaceta):
    n_alta  = sum(1 for r in resumenes if r.get("nivel_relevancia","").lower() == "alta")
    n_media = sum(1 for r in resumenes if r.get("nivel_relevancia","").lower() == "media")
    n_baja  = sum(1 for r in resumenes if r.get("nivel_relevancia","").lower() == "baja")
    asunto  = f"\U0001f3db Monitor Legislativo MX \u2014 {date.today()} \u2014 {n_alta} alta | {n_media} media | {n_baja} baja"
    kws_str = ", ".join(KEYWORDS)

    rows = []
    for item in df_keywords:
        link = item.get("link","")
        txt  = item.get("texto","")
        sec  = item.get("seccion","")
        row  = f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eee;"><b>[{sec}]</b> {txt}</td>'
        if link:
            row += f'<td style="padding:6px 8px;border-bottom:1px solid #eee;"><a href="{link}" style="color:#1a3a5c;font-weight:bold;">Ver &rarr;</a></td>'
        else:
            row += f'<td style="padding:6px 8px;border-bottom:1px solid #eee;"><a href="https://gaceta.diputados.gob.mx/gp_hoy.html" style="color:#1a3a5c;font-weight:bold;">Ver Gaceta &rarr;</a></td>'
        row += '</tr>'
        rows.append(row)
    kw_table = ""
    if rows:
        kw_table = f'''<h3 style="color:#1a3a5c;font-family:Garamond,Georgia,serif;">&#9889; Items con keywords</h3>
        <table style="width:100%;border-collapse:collapse;font-family:Garamond,Georgia,serif;font-size:14px;">{"".join(rows)}</table>'''

    res_html = ""
    for res in resumenes:
        nivel = res.get("nivel_relevancia","baja").lower()
        color = {"alta":"#c0392b","media":"#e67e22","baja":"#2980b9"}.get(nivel,"#2980b9")
        titulo_item = res.get("titulo_pdf","")
        tema    = res.get("tema_principal","")
        resumen = res.get("resumen","")
        rel     = res.get("relevancia","")
        if not rel or "no se menciona" in rel.lower():
            rel = f"Baja relevancia \u2014 el documento trata sobre: {tema}"
        legs = res.get("presentadores",[])
        presenta = ""
        if legs:
            n = legs[0].get("nombre",""); c = legs[0].get("camara","")
            if n: presenta = f"{n} ({c})"
        kws_enc = ", ".join(res.get("keywords_encontradas",[]))
        url_pdf = res.get("url_pdf","")
        res_html += f'''
        <div style="border-left:5px solid {color};padding:12px 16px;margin-bottom:16px;background:#fff;border-radius:0 6px 6px 0;font-family:Garamond,Georgia,serif;">
          <span style="background:{color};color:white;font-size:11px;padding:2px 10px;border-radius:10px;font-weight:bold;">{nivel.upper()}</span>
          <p style="font-weight:bold;font-size:15px;margin:8px 0 4px;">{titulo_item}</p>
          <p style="color:#555;font-size:12px;text-transform:uppercase;margin:0;">&#128204; Tema</p>
          <p style="margin:2px 0 8px;font-size:14px;">{tema}</p>
          <p style="color:#555;font-size:12px;text-transform:uppercase;margin:0;">&#128221; Resumen</p>
          <p style="margin:2px 0 8px;font-size:14px;">{resumen}</p>
          <p style="color:#555;font-size:12px;text-transform:uppercase;margin:0;">&#127919; Relevancia</p>
          <p style="margin:2px 0 8px;font-size:14px;">{rel}</p>'''
        if presenta:
            res_html += f'<p style="color:#555;font-size:12px;text-transform:uppercase;margin:0;">&#128100; Presenta</p><p style="margin:2px 0 8px;font-size:14px;">{presenta}</p>'
        if kws_enc:
            res_html += f'<p style="color:#555;font-size:12px;text-transform:uppercase;margin:0;">&#128273; Keywords</p><p style="margin:2px 0 8px;font-size:14px;">{kws_enc}</p>'
        if url_pdf:
            res_html += f'<p style="margin-top:10px;"><a href="{url_pdf}" style="color:#1a3a5c;font-weight:bold;">&#128229; Ver documento completo &rarr;</a></p>'
        else:
            res_html += f'<p style="margin-top:10px;"><a href="https://gaceta.diputados.gob.mx/gp_hoy.html" style="color:#1a3a5c;font-weight:bold;">&#128229; Ver Gaceta Parlamentaria &rarr;</a></p>'
        res_html += '</div>'

    html = f"""
    <div style="font-family:Garamond,Georgia,serif;max-width:680px;margin:0 auto;background:#f5f5f5;padding:24px;">
      <div style="background:#1a3a5c;color:white;padding:20px 24px;border-radius:8px 8px 0 0;">
        <h1 style="margin:0;font-size:22px;font-family:Garamond,Georgia,serif;">&#127963; Monitor Legislativo MX</h1>
        <p style="margin:4px 0 0;font-size:13px;opacity:0.8;">{titulo_gaceta}</p>
        <p style="margin:2px 0 0;font-size:13px;opacity:0.8;">{date.today()}</p>
      </div>
      <div style="background:white;padding:20px 24px;border-radius:0 0 8px 8px;">
        <p style="background:#eef3f8;padding:8px 12px;border-radius:4px;font-size:13px;">
          &#128269; <b>Keywords monitoreadas:</b> {kws_str}
        </p>
        <p style="background:#e8f5e9;padding:8px 12px;border-radius:4px;font-size:13px;border-left:4px solid #27ae60;">
          &#128202; <b>Spreadsheet actualizado</b> con los resultados de hoy.
        </p>
        <p style="font-size:14px;">
          <b>Con keywords:</b> {len(df_keywords)} &nbsp;|&nbsp;
          <b style="color:#c0392b;">Alta:</b> {n_alta} &nbsp;|&nbsp;
          <b style="color:#e67e22;">Media:</b> {n_media} &nbsp;|&nbsp;
          <b style="color:#2980b9;">Baja:</b> {n_baja}
        </p>
        {kw_table}
        {("<h3 style='color:#1a3a5c;font-family:Garamond,Georgia,serif;'>&#129302; An&aacute;lisis IA por documento</h3>") if resumenes else ""}
        {res_html}
        <p style="color:#aaa;font-size:11px;border-top:1px solid #eee;padding-top:12px;margin-top:24px;">
          Monitor Legislativo MX &nbsp;&middot;&nbsp; Generado autom&aacute;ticamente &nbsp;&middot;&nbsp; {date.today()}
        </p>
      </div>
    </div>
    """

    msg = MIMEMultipart("mixed")
    msg["Subject"] = asunto
    msg["From"]    = GMAIL_ACCOUNT
    msg["To"]      = ", ".join(CORREOS)
    msg.attach(MIMEText(html, "html", "utf-8"))

    # PDF attachment
    try:
        pdf_bytes = generar_pdf(df_keywords, resumenes, titulo_gaceta)
        pdf_part = MIMEBase("application", "pdf")
        pdf_part.set_payload(pdf_bytes)
        encoders.encode_base64(pdf_part)
        pdf_part.add_header("Content-Disposition", f"attachment; filename=monitor_legislativo_{date.today()}.pdf")
        msg.attach(pdf_part)
    except Exception as e:
        print(f"Error PDF: {e}")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ACCOUNT, GMAIL_PASS)
        server.sendmail(GMAIL_ACCOUNT, CORREOS, msg.as_string())
    print("Email enviado")


# ── EMAILS SIN RESULTADOS ─────────────────────────────────────────────────────
def enviar_email_sin_gaceta():
    asunto = f"🏛️ Monitor Legislativo MX — {date.today()} — Sin Gaceta"
    kws_str = ", ".join(KEYWORDS)
    html = f"""
    <div style="font-family:Georgia,serif;max-width:680px;margin:0 auto;background:#f5f5f5;padding:24px;">
      <div style="background:#1a3a5c;color:white;padding:20px 24px;border-radius:8px 8px 0 0;">
        <h1 style="margin:0;font-size:22px;font-family:Georgia,serif;">🏛️ Monitor Legislativo MX</h1>
        <p style="margin:4px 0 0;font-size:13px;opacity:0.8;">{date.today()}</p>
      </div>
      <div style="background:white;padding:20px 24px;border-radius:0 0 8px 8px;">
        <p style="background:#eef3f8;padding:8px 12px;border-radius:4px;font-size:13px;">
          🔍 <b>Keywords monitoreadas:</b> {kws_str}
        </p>
        <div style="border-left:5px solid #e67e22;padding:14px 16px;margin:16px 0;background:#fdf6ec;border-radius:0 6px 6px 0;">
          <p style="margin:0;font-size:15px;color:#1a3a5c;"><b>Hoy no se publicó Gaceta Parlamentaria.</b></p>
        </div>
        <p style="font-size:13px;color:#555;">No se encontró publicación de la Gaceta para el día de hoy.</p>
        <p style="color:#aaa;font-size:11px;border-top:1px solid #eee;padding-top:12px;margin-top:24px;">
          Monitor Legislativo MX · Generado automáticamente · {date.today()}
        </p>
      </div>
    </div>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"]    = GMAIL_ACCOUNT
    msg["To"]      = ", ".join(CORREOS)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ACCOUNT, GMAIL_PASS)
        server.sendmail(GMAIL_ACCOUNT, CORREOS, msg.as_string())
    print("Email sin gaceta enviado")

def enviar_email_sin_coincidencias(titulo_gaceta):
    asunto = f"🏛️ Monitor Legislativo MX — {date.today()} — Sin coincidencias"
    kws_str = ", ".join(KEYWORDS)
    html = f"""
    <div style="font-family:Georgia,serif;max-width:680px;margin:0 auto;background:#f5f5f5;padding:24px;">
      <div style="background:#1a3a5c;color:white;padding:20px 24px;border-radius:8px 8px 0 0;">
        <h1 style="margin:0;font-size:22px;font-family:Georgia,serif;">🏛️ Monitor Legislativo MX</h1>
        <p style="margin:4px 0 0;font-size:13px;opacity:0.8;">{titulo_gaceta}</p>
        <p style="margin:2px 0 0;font-size:13px;opacity:0.8;">{date.today()}</p>
      </div>
      <div style="background:white;padding:20px 24px;border-radius:0 0 8px 8px;">
        <p style="background:#eef3f8;padding:8px 12px;border-radius:4px;font-size:13px;">
          🔍 <b>Keywords monitoreadas:</b> {kws_str}
        </p>
        <div style="border-left:5px solid #2980b9;padding:14px 16px;margin:16px 0;background:#f0f4f8;border-radius:0 6px 6px 0;">
          <p style="margin:0;font-size:15px;color:#1a3a5c;"><b>El día de hoy no hubo asuntos relevantes en la Gaceta para las keywords monitoreadas.</b></p>
        </div>
        <p style="font-size:13px;color:#555;">
          La Gaceta fue revisada pero ningún documento en el índice contiene las keywords de búsqueda.
        </p>
        <p style="font-size:13px;">
          <a href="https://gaceta.diputados.gob.mx/gp_hoy.html" style="color:#1a3a5c;font-weight:bold;">🏛️ Ver Gaceta Parlamentaria →</a>
        </p>
        <p style="color:#aaa;font-size:11px;border-top:1px solid #eee;padding-top:12px;margin-top:24px;">
          Monitor Legislativo MX · Generado automáticamente · {date.today()}
        </p>
      </div>
    </div>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"]    = GMAIL_ACCOUNT
    msg["To"]      = ", ".join(CORREOS)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ACCOUNT, GMAIL_PASS)
        server.sendmail(GMAIL_ACCOUNT, CORREOS, msg.as_string())
    print("Email sin coincidencias enviado")

def _enviar(asunto, texto):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"]    = GMAIL_ACCOUNT
    msg["To"]      = ", ".join(CORREOS)
    msg.attach(MIMEText(texto, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ACCOUNT, GMAIL_PASS)
        server.sendmail(GMAIL_ACCOUNT, CORREOS, msg.as_string())

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Iniciando Monitor Legislativo — {date.today()}")
    df_keywords, resumenes, titulo, soup = scrape_gaceta()

    if soup is None:
        enviar_email_sin_gaceta()
    elif not resumenes and not df_keywords:
        enviar_email_sin_coincidencias(titulo)
    else:
        guardar_en_sheets(resumenes, titulo)
        enviar_email(df_keywords, resumenes, titulo)
    print("✅ Completado")
