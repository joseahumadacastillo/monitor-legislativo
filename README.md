# Monitor Legislativo MX

Sistema automatizado que revisa todos los días las publicaciones oficiales del Poder Legislativo mexicano, identifica las que son relevantes con un modelo de lenguaje y envía un resumen ejecutivo por correo.

**Problema:** dar seguimiento manual a la Gaceta Parlamentaria —iniciativas, dictámenes, acuerdos y convocatorias— consume horas y es fácil que se pase algo importante.
**Solución:** un pipeline que corre solo cada mañana, extrae y estructura la información, la clasifica con IA y entrega un reporte priorizado.

---

## ⚙️ Cómo funciona

```
GitHub Actions (cron, 9:30 am CDMX)
        │
        ▼
1. Extracción ──► scraping de la Gaceta y sus anexos
        │
        ▼
2. Filtrado ───► palabras clave definidas por tema
        │
        ▼
3. Clasificación con IA (GPT-4o-mini) ──► para cada documento:
        • tipo de documento   • tema   • resumen breve
        • quién lo presenta    • relevancia
        │
        ▼
4. Registro ───► historial en Google Sheets
        │
        ▼
5. Entrega ────► correo HTML + PDF con el resumen priorizado
```

## 🧰 Herramientas

- **Python**: `requests`, scraping y procesamiento de texto
- **OpenAI API** (`gpt-4o-mini`, temperatura 0) para extracción estructurada y clasificación
- **Google Sheets API** como registro histórico
- **ReportLab** para el PDF y **SMTP** para el envío
- **GitHub Actions** para la ejecución programada

## 🔐 Configuración

Las credenciales nunca están en el código. Se leen de variables de entorno, configuradas como *GitHub Secrets*:

| Variable | Uso |
|---|---|
| `OPENAI_API_KEY` | Clasificación con el modelo de lenguaje |
| `GMAIL_ACCOUNT`, `GMAIL_PASS` | Envío del reporte (contraseña de aplicación) |
| `SHEET_ID`, `GOOGLE_CREDENTIALS` | Registro en Google Sheets |
| `DESTINATARIOS` *(opcional)* | Correos que reciben el reporte, separados por coma |
| `KEYWORDS` *(opcional)* | Palabras clave a monitorear, separadas por coma |

## ⏰ Ejecución programada

El workflow `.github/workflows/monitor.yml` corre el monitor con GitHub Actions. En esta versión pública la ejecución diaria viene comentada; para activarla, configura los *secrets* y descomenta el bloque `schedule`.

## ▶️ Ejecución local

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...   # y el resto de las variables
python monitor.py
```

## 📌 Resultado

Un correo diario con los documentos relevantes, su clasificación y un resumen de cada uno, en lugar de revisar manualmente decenas de publicaciones.

---
Autor: **José Ahumada Castillo** · [LinkedIn](https://www.linkedin.com/in/jos%C3%A9-ahumada-castillo-495b7519a/)
