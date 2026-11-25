"""
Dashboard visual de movimientos de márgenes (mínimo / máximo) por mes,
con señales para revisar posibles malas prácticas.

Los datos NO van en el repositorio: se leen desde st.secrets.
Ver .streamlit/secrets.toml.example y README.md.
"""
import base64
import gzip
import hmac
import io
import re
from html import escape

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(page_title="Márgenes 2026", page_icon=":material/bar_chart:", layout="wide")

MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
ANCHO = "stretch"


# ================================================================ acceso
def check_password() -> bool:
    """Contraseña opcional: si existe [app] password en secrets, se pide."""
    pwd = st.secrets.get("app", {}).get("password")
    if not pwd or st.session_state.get("auth_ok"):
        return True
    p = st.text_input("Contraseña", type="password")
    if p and hmac.compare_digest(p, pwd):
        st.session_state["auth_ok"] = True
        st.rerun()
    elif p:
        st.error("Contraseña incorrecta")
    return False


# ================================================================ lectura
def _descarga_directa(url: str) -> str:
    """Convierte un link de compartir de OneDrive/SharePoint en descarga directa."""
    if "sharepoint.com" in url:  # OneDrive empresarial / Microsoft 365
        if "download=1" in url:
            return url
        return url + ("&" if "?" in url else "?") + "download=1"
    if "1drv.ms" in url or "onedrive.live.com" in url:  # OneDrive personal
        token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"https://api.onedrive.com/v1.0/shares/u!{token}/root/content"
    return url


def _bytes_from_secrets() -> bytes:
    """[datos] archivo_b64 (gzip+base64) | url (+token) | ruta"""
    d = st.secrets.get("datos", {})
    if d.get("archivo_b64"):
        raw = base64.b64decode(d["archivo_b64"])
        return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    if d.get("url"):
        headers = {"Authorization": f"Bearer {d['token']}"} if d.get("token") else {}
        r = requests.get(_descarga_directa(d["url"]), headers=headers, timeout=60)
        if r.status_code in (401, 403):
            st.error(f"OneDrive negó el acceso al archivo ({r.status_code}). El link debe estar "
                     "compartido como 'Cualquier persona con el vínculo puede ver'. Si tu "
                     "organización no lo permite, usa archivo_b64 (generar_secret.py).")
            st.stop()
        r.raise_for_status()
        if b"login.microsoftonline.com" in r.content[:20000] or b"login.live.com" in r.content[:20000]:
            st.error("El link de OneDrive pide iniciar sesión. Compártelo como "
                     "'Cualquier persona con el vínculo puede ver'.")
            st.stop()
        return r.content
    if d.get("ruta"):
        with open(d["ruta"], "rb") as f:
            return f.read()
    st.error("No hay datos configurados en secrets. Revisa la sección [datos] de secrets.toml.")
    st.stop()


def _parse_html_xls(text: str) -> pd.DataFrame:
    """El .xls del sistema es en realidad una tabla HTML."""
    limpiar = lambda c: re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()  # noqa: E731
    hdr = [limpiar(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", text, flags=re.S | re.I)]
    rows = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", r, flags=re.S | re.I)
        if cells and (not hdr or len(cells) == len(hdr)):
            rows.append([limpiar(c) for c in cells])
    if not hdr:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=hdr)


def _corto(s: str) -> str:
    u = str(s).upper()
    if "CERVANTES" in u:
        return "Beatriz Cervantes"
    if "MONTESINOS" in u:
        return "Daniel Montesinos"
    return re.sub(r"\s*\(.*\)$", "", str(s)).title()


REQUERIDAS = [
    "SCMARMINACT", "SCMARMINSOL", "SCMARMAXACT", "SCMARMAXSOL", "FCHSOL", "FCHAUT",
    "DETERMINACION_MIN", "DETERMINACION_MAX", "DESCRIPCION DEL PRODUCTO", "SUCURSAL",
    "UNIDAD OPERATIVA", "SCFOLIO", "CVE. DE PRODUCTO", "CLAVE DE INVENTARIO",
    "AUTORIZO", "SOLICITO",
]


@st.cache_data(ttl=3600, show_spinner="Cargando datos…")
def cargar() -> pd.DataFrame:
    b = _bytes_from_secrets()
    head = b[:2000].lower()
    df = pd.DataFrame()
    try:
        if b"<html" in head or b"<table" in head or b"<th" in head:
            df = _parse_html_xls(b.decode("utf-8", errors="replace"))
        else:
            # Excel real (p. ej. si se abrió y guardó en Excel): buscar la fila de encabezados
            raw = pd.read_excel(io.BytesIO(b), header=None, dtype=str)
            fila = next((i for i, r in raw.iterrows()
                         if "SCFOLIO" in [str(x).strip() for x in r.values]), None)
            if fila is not None:
                df = raw.iloc[fila + 1:].reset_index(drop=True)
                df.columns = [str(x) for x in raw.iloc[fila].values]
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudo leer el archivo descargado: {type(e).__name__}")
    df.columns = [str(c).strip() for c in df.columns]
    faltan = [c for c in REQUERIDAS if c not in df.columns]
    if faltan:
        st.error("El archivo recibido no tiene las columnas esperadas. "
                 "Si usas un link de OneDrive, probablemente no se está descargando el archivo "
                 "sino una página web: revisa que esté compartido como "
                 "'Cualquier persona con el vínculo'.")
        with st.expander("Diagnóstico"):
            st.write(f"Tamaño recibido: {len(b):,} bytes")
            st.write("Columnas encontradas:", list(df.columns)[:30])
            st.write("Faltan:", faltan)
            st.code(b[:600].decode("utf-8", errors="replace"))
        st.stop()
    for c in ["SCMARMINACT", "SCMARMINSOL", "SCMARMAXACT", "SCMARMAXSOL"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ["FCHSOL", "FCHAUT"]:
        df[c] = pd.to_datetime(df[c], format="%d/%m/%Y %H:%M:%S", errors="coerce")
    for c in ["DETERMINACION_MIN", "DETERMINACION_MAX", "DESCRIPCION DEL PRODUCTO"]:
        df[c] = df[c].astype(str).str.strip()
    for c in ["SUCURSAL", "UNIDAD OPERATIVA", "SCFOLIO", "CVE. DE PRODUCTO", "CLAVE DE INVENTARIO"]:
        df[c] = df[c].astype(str).str.strip()

    df["Autorizó"] = df["AUTORIZO"].map(_corto)
    df["Solicitó"] = df["SOLICITO"].map(lambda s: re.sub(r"\s*\(.*\)$", "", str(s)).title())
    df["Mes"] = df["FCHSOL"].dt.to_period("M").astype(str)
    df["Solicitud"] = df["SUCURSAL"] + "-" + df["UNIDAD OPERATIVA"] + "-" + df["SCFOLIO"]
    df["Cambio mín."] = df["SCMARMINSOL"] - df["SCMARMINACT"]
    df["Cambio máx."] = df["SCMARMAXSOL"] - df["SCMARMAXACT"]
    df["Cambio máx. %"] = df["Cambio máx."] / df["SCMARMAXACT"].replace(0, np.nan)
    df["Minutos para autorizar"] = (df["FCHAUT"] - df["FCHSOL"]).dt.total_seconds() / 60
    df["Hora autorización"] = df["FCHAUT"].dt.hour
    df["Combinación"] = (
        "Mín. " + df["DETERMINACION_MIN"].str.lower() + " / Máx. " + df["DETERMINACION_MAX"].str.lower()
    )
    # clave producto-lugar para detectar cambios repetidos
    df["Clave producto-UO"] = (
        df["SUCURSAL"] + "-" + df["UNIDAD OPERATIVA"] + "-" + df["CLAVE DE INVENTARIO"] + "-" + df["CVE. DE PRODUCTO"]
    )
    df = df.sort_values("FCHSOL").reset_index(drop=True)
    df["Días desde cambio previo"] = df.groupby("Clave producto-UO")["FCHSOL"].diff().dt.total_seconds() / 86400
    df["Auto-aprobación"] = (
        df["SOLICITO"].str.extract(r"\((.*?)\)")[0].str.upper()
        == df["AUTORIZO"].str.extract(r"\((.*?)\)")[0].str.upper()
    )
    return df


# ================================================================ alertas
ALERTAS = {
    # nombre: descripción mostrada en la app
    "Cambio extremo en máximo": "El margen máximo sube al menos {u_dmax} pts o más del {u_rmax}% sobre el actual.",
    "Máximo solicitado muy alto": "El margen máximo solicitado es de {u_maxalto} o más.",
    "Baja fuerte del mínimo": "El margen mínimo baja {u_dmin} pts o más.",
    "Mínimo en cero o negativo": "El margen mínimo solicitado es ≤ 0 (posible venta sin margen o bajo costo).",
    "Rango invertido": "El mínimo solicitado es igual o mayor que el máximo solicitado.",
    "Ampliación doble": "En el mismo movimiento se baja el mínimo y se sube el máximo.",
    "Autorización exprés": "Se autorizó en menos de {u_expres} minutos desde la solicitud.",
    "Autorización tardía": "Se autorizó después de {u_tarde} horas.",
    "Fuera de horario": "Autorizado antes de las {u_hini} h, a partir de las {u_hfin} h o en fin de semana.",
    "Cambio repetido": "Mismo producto, U.O. e inventario con otro cambio menos de {u_dias} días antes.",
    "Autorización en lote": "{u_lote} o más solicitudes distintas autorizadas en el mismo minuto por la misma persona.",
    "Auto-aprobación": "Quien solicitó es la misma persona que autorizó.",
}


def marcar(df: pd.DataFrame, u: dict) -> pd.DataFrame:
    d = df.copy()
    d["Cambio extremo en máximo"] = (d["Cambio máx."] >= u["u_dmax"]) | (d["Cambio máx. %"] > u["u_rmax"] / 100)
    d["Máximo solicitado muy alto"] = d["SCMARMAXSOL"] >= u["u_maxalto"]
    d["Baja fuerte del mínimo"] = d["Cambio mín."] <= -u["u_dmin"]
    d["Mínimo en cero o negativo"] = d["SCMARMINSOL"] <= 0
    d["Rango invertido"] = d["SCMARMINSOL"] >= d["SCMARMAXSOL"]
    d["Ampliación doble"] = (d["DETERMINACION_MIN"] == "DECREMENTO") & (d["DETERMINACION_MAX"] == "INCREMENTO")
    d["Autorización exprés"] = d["Minutos para autorizar"] < u["u_expres"]
    d["Autorización tardía"] = d["Minutos para autorizar"] > u["u_tarde"] * 60
    h, wd = d["FCHAUT"].dt.hour, d["FCHAUT"].dt.weekday
    d["Fuera de horario"] = (h < u["u_hini"]) | (h >= u["u_hfin"]) | (wd >= 5)
    d["Cambio repetido"] = d["Días desde cambio previo"] < u["u_dias"]
    minuto = d["FCHAUT"].dt.floor("min")
    n_lote = d.groupby(["Autorizó", minuto])["Solicitud"].transform("nunique")
    d["Solicitudes en el mismo minuto"] = n_lote
    d["Autorización en lote"] = n_lote >= u["u_lote"]
    flags = list(ALERTAS)
    d[flags] = d[flags].fillna(False).astype(bool)
    d["Núm. alertas"] = d[flags].sum(axis=1)
    d["Alertas"] = d[flags].apply(lambda r: ", ".join(f for f in flags if r[f]), axis=1)
    return d


def tabla_alertas(d: pd.DataFrame) -> pd.DataFrame:
    """Conteo y tasa por cada 100 movimientos, por autorizador."""
    filas = []
    for f in ALERTAS:
        fila = {"Alerta": f}
        for a, x in d.groupby("Autorizó"):
            fila[f"{a} · casos"] = int(x[f].sum())
            fila[f"{a} · por 100 mov."] = round(100 * x[f].mean(), 1)
            fila[f"{a} · solicitudes"] = int(x.loc[x[f], "Solicitud"].nunique())
        filas.append(fila)
    return pd.DataFrame(filas)


# ================================================================ resúmenes
def resumen(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("Autorizó")
    out = pd.DataFrame({
        "Movimientos": g.size(),
        "Solicitudes": g["Solicitud"].nunique(),
        "Mín. decremento": g["DETERMINACION_MIN"].apply(lambda s: (s == "DECREMENTO").sum()),
        "Mín. incremento": g["DETERMINACION_MIN"].apply(lambda s: (s == "INCREMENTO").sum()),
        "Máx. decremento": g["DETERMINACION_MAX"].apply(lambda s: (s == "DECREMENTO").sum()),
        "Máx. incremento": g["DETERMINACION_MAX"].apply(lambda s: (s == "INCREMENTO").sum()),
        "Mediana min. p/autorizar": g["Minutos para autorizar"].median().round(1),
        "Primer mes": g["Mes"].min(),
        "Último mes": g["Mes"].max(),
    })
    return out.reset_index()


def por_mes(df: pd.DataFrame) -> pd.DataFrame:
    t = pd.crosstab(df["Mes"], df["Autorizó"])
    t["Total"] = t.sum(axis=1)
    for k, col in [("Mín.", "DETERMINACION_MIN"), ("Máx.", "DETERMINACION_MAX")]:
        c = pd.crosstab(df["Mes"], df[col])
        for v in ["DECREMENTO", "INCREMENTO"]:
            t[f"{k} {v.lower()}"] = c.get(v, 0)
    return t.reset_index()


def pct_tipo(df: pd.DataFrame) -> pd.DataFrame:
    filas = []
    for campo, et in [("DETERMINACION_MIN", "Mín."), ("DETERMINACION_MAX", "Máx.")]:
        t = pd.crosstab(df["Autorizó"], df[campo], normalize="index") * 100
        for v in ["DECREMENTO", "INCREMENTO", "SIN CAMBIOS"]:
            for a in t.index:
                filas.append({"Tipo": f"{et} {v.lower()}", "Autorizó": a, "%": round(t.loc[a].get(v, 0), 1)})
    return pd.DataFrame(filas)


def magnitudes(df: pd.DataFrame) -> pd.DataFrame:
    filas = []
    for campo, cambio, et in [("DETERMINACION_MIN", "Cambio mín.", "Mín."), ("DETERMINACION_MAX", "Cambio máx.", "Máx.")]:
        for v in ["DECREMENTO", "INCREMENTO"]:
            for a, x in df[df[campo] == v].groupby("Autorizó"):
                s = x[cambio]
                filas.append({"Movimiento": f"{et} {v.lower()}", "Autorizó": a, "Casos": len(s),
                              "Promedio (pts)": round(s.mean(), 2), "Mediana (pts)": round(s.median(), 2),
                              "Más bajo": round(s.min(), 2), "Más alto": round(s.max(), 2)})
    return pd.DataFrame(filas)


# ================================================================ excel
DETALLE_COLS = {
    "Mes": "Mes", "Autorizó": "Autorizó", "Solicitó": "Solicitó", "FCHSOL": "Fecha solicitud",
    "FCHAUT": "Fecha autorización", "Minutos para autorizar": "Minutos para autorizar",
    "NOMBRE DE SUCURSAL": "Región", "NOMBRE DE UNIDAD OP.": "Unidad operativa", "SCFOLIO": "Folio",
    "DESCRIPCIÓN DEL INVENTARIO": "Inventario", "CVE. DE PRODUCTO": "Cve. producto",
    "DESCRIPCION DEL PRODUCTO": "Producto", "SCMARMINACT": "Mín. actual", "SCMARMINSOL": "Mín. solicitado",
    "Cambio mín.": "Cambio mín.", "DETERMINACION_MIN": "Determinación mín.", "SCMARMAXACT": "Máx. actual",
    "SCMARMAXSOL": "Máx. solicitado", "Cambio máx.": "Cambio máx.", "DETERMINACION_MAX": "Determinación máx.",
    "Núm. alertas": "Núm. alertas", "Alertas": "Alertas",
}


def a_excel(d: pd.DataFrame) -> bytes:
    det = d[[c for c in DETALLE_COLS if c in d.columns]].rename(columns=DETALLE_COLS)
    det["Minutos para autorizar"] = det["Minutos para autorizar"].round(1)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        resumen(d).to_excel(xw, sheet_name="Resumen", index=False)
        por_mes(d).to_excel(xw, sheet_name="Por mes", index=False)
        magnitudes(d).to_excel(xw, sheet_name="Comparativo", index=False)
        pct_tipo(d).pivot(index="Tipo", columns="Autorizó", values="%").reset_index().to_excel(
            xw, sheet_name="Comparativo", index=False, startrow=len(magnitudes(d)) + 3)
        tabla_alertas(d).to_excel(xw, sheet_name="Alertas resumen", index=False)
        det[det["Núm. alertas"] > 0].sort_values("Núm. alertas", ascending=False).to_excel(
            xw, sheet_name="Alertas detalle", index=False)
        det.to_excel(xw, sheet_name="Detalle", index=False)
        for a in sorted(d["Autorizó"].unique()):
            det[det["Autorizó"] == a].to_excel(xw, sheet_name=a.split()[0][:31], index=False)
        det[(det["Determinación mín."] == "INCREMENTO") | (det["Determinación máx."] == "INCREMENTO")].to_excel(
            xw, sheet_name="Incrementos", index=False)
        det[(det["Determinación mín."] == "DECREMENTO") | (det["Determinación máx."] == "DECREMENTO")].to_excel(
            xw, sheet_name="Decrementos", index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "A2"
            if ws.title != "Comparativo":
                ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    50, max(10, max(len(str(c.value or "")) for c in col[:300]) + 2))
    return buf.getvalue()


# ================================================================ estilo visual (tipo Ant Design)
AZUL, VERDE, AMARILLO, ROJO = "#1677ff", "#52c41a", "#faad14", "#ff4d4f"
MORADO, CIAN, ROSA, NARANJA = "#722ed1", "#13c2c2", "#eb2f96", "#fa8c16"
GRIS = "#d9d9d9"
TEXTO, TEXTO_2 = "rgba(0,0,0,0.88)", "rgba(0,0,0,0.45)"
FUENTE = dict(family="Inter, -apple-system, Segoe UI, Roboto, sans-serif", size=13, color=TEXTO)
PALETA = [AZUL, VERDE, AMARILLO, ROJO, MORADO, CIAN, ROSA, NARANJA]
COLORES_AUT = {"Daniel Montesinos": AZUL, "Beatriz Cervantes": ROSA}
COLORES_SUBE_BAJA = {"Sube": ROJO, "Baja": AZUL, "Sin cambio": GRIS}
COLORES_TIPO = {"Bajó el mínimo": AZUL, "Subió el máximo": ROJO, "Bajó mín. y subió máx.": MORADO,
                "Subió el mínimo": NARANJA, "Bajó el máximo": CIAN, "Sin cambio en márgenes": GRIS}
ICONOS = {  # nombres de Material Symbols
    "Cambio extremo en máximo": "trending_up", "Máximo solicitado muy alto": "vertical_align_top",
    "Baja fuerte del mínimo": "trending_down", "Mínimo en cero o negativo": "exposure_zero",
    "Rango invertido": "swap_vert", "Ampliación doble": "unfold_more", "Autorización exprés": "bolt",
    "Autorización tardía": "hourglass_bottom", "Fuera de horario": "dark_mode", "Cambio repetido": "repeat",
    "Autorización en lote": "inventory_2", "Auto-aprobación": "how_to_reg",
}
EXPLICA = {  # versión corta y en lenguaje simple
    "Cambio extremo en máximo": "Subieron mucho el margen máximo",
    "Máximo solicitado muy alto": "El margen máximo quedó muy alto",
    "Baja fuerte del mínimo": "Bajaron mucho el margen mínimo",
    "Mínimo en cero o negativo": "El mínimo quedó en cero o negativo",
    "Rango invertido": "El mínimo quedó arriba del máximo",
    "Ampliación doble": "Bajaron el mínimo y subieron el máximo a la vez",
    "Autorización exprés": "Se autorizó en segundos",
    "Autorización tardía": "Tardaron días en autorizar",
    "Fuera de horario": "Se autorizó de noche o en fin de semana",
    "Cambio repetido": "El mismo producto se cambió otra vez en pocos días",
    "Autorización en lote": "Muchas solicitudes autorizadas de golpe",
    "Auto-aprobación": "La misma persona pidió y autorizó",
}
NOMBRE_MES = {f"{m:02d}": n for m, n in zip(range(1, 13), ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
              "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"])}
U = {"u_dmax": 40.0, "u_rmax": 100, "u_maxalto": 100.0, "u_dmin": 10.0, "u_expres": 2.0,
     "u_tarde": 72, "u_hini": 8, "u_hfin": 19, "u_dias": 30, "u_lote": 5}  # umbrales fijos de las señales

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,400,0..1,0&display=block');
.msi{font-family:'Material Symbols Outlined';font-weight:normal;font-style:normal;line-height:1;letter-spacing:normal;
  text-transform:none;display:inline-block;white-space:nowrap;direction:ltr;font-feature-settings:'liga';
  -webkit-font-smoothing:antialiased;vertical-align:middle}
.block-container{padding-top:4.75rem;max-width:1440px}
header[data-testid="stHeader"]{background:#ffffff;box-shadow:0 1px 4px rgba(0,21,41,.08)}
/* menú lateral */
[data-testid="stSidebarNavLink"]{border-radius:8px;margin:2px 8px}
[data-testid="stSidebarNavLink"][aria-current="page"]{background:rgba(22,119,255,.16)!important;
  box-shadow:inset 3px 0 0 #1677ff}
[data-testid="stSidebarNavLink"][aria-current="page"] span{color:#ffffff!important;font-weight:600}
[data-testid="stNavSectionHeader"]{font-size:11px!important;letter-spacing:.08em;text-transform:uppercase;
  color:rgba(255,255,255,.45)!important;font-weight:600}
/* encabezados */
.page-title{font-size:26px!important;font-weight:600;color:rgba(0,0,0,.88);margin:0;line-height:1.3}
.page-sub{color:rgba(0,0,0,.45);font-size:14px;margin:4px 0 0}
.sec-label{font-size:12px;font-weight:600;letter-spacing:.06em;color:rgba(0,0,0,.45);text-transform:uppercase;
  margin:26px 0 10px}
/* tarjetas */
[class*="st-key-card"]{background:#ffffff;border-radius:8px;border:none!important;padding:0 20px 12px;
  box-shadow:0 1px 2px rgba(0,0,0,.03),0 1px 6px -1px rgba(0,0,0,.02),0 2px 4px rgba(0,0,0,.02)}
.card-head{font-size:16px;font-weight:600;color:rgba(0,0,0,.88);display:flex;align-items:center;gap:8px;
  margin:0 -20px 4px;padding:16px 20px 14px;border-bottom:1px solid #f0f0f0}
.card-head .msi{color:#1677ff;font-size:20px}
.stat{background:#ffffff;border-radius:8px;padding:22px 24px;min-height:160px;
  box-shadow:0 1px 2px rgba(0,0,0,.03),0 1px 6px -1px rgba(0,0,0,.02),0 2px 4px rgba(0,0,0,.02);margin-bottom:16px}
.stat-title{color:rgba(0,0,0,.45);font-size:14px}
.stat-row{display:flex;align-items:center;gap:8px;margin:8px 0 14px;flex-wrap:wrap}
.stat-row .msi{font-size:28px}
.stat-val{font-size:28px;font-weight:500;color:rgba(0,0,0,.88);line-height:1.1}
.stat-trend{font-size:13px;display:flex;align-items:center}
.stat-trend .msi{font-size:16px}
.stat-bar{height:8px;background:#f0f0f0;border-radius:100px;overflow:hidden}
.stat-bar>div{height:100%;border-radius:100px}
.stat-note{color:rgba(0,0,0,.45);font-size:12px;margin-top:10px}
.ring-card{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:120px}
.ring-card .stat-row{margin:6px 0 0}
.ring-card .stat-val{font-size:22px;font-weight:600}
.ring{width:80px;height:80px;border-radius:50%;display:grid;place-items:center;flex-shrink:0}
.ring>span{width:66px;height:66px;border-radius:50%;background:#ffffff;display:grid;place-items:center;
  font-size:17px;color:rgba(0,0,0,.88)}
.lista{list-style:none;padding:0;margin:4px 0}
.lista li{display:flex;gap:12px;align-items:flex-start;padding:9px 0;border-bottom:1px solid #f5f5f5;
  font-size:14px;color:rgba(0,0,0,.88)}
.lista li:last-child{border-bottom:none}
.lista .dot{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;flex-shrink:0;
  background:rgba(22,119,255,.08);color:#1677ff}
.lista .dot .msi{font-size:18px}
.lista small{color:rgba(0,0,0,.45);display:block;font-size:12px}
.mes-nota{text-align:center;font-size:13px;color:rgba(0,0,0,.45);margin:-6px 0 4px}
.mes-nota b{color:rgba(0,0,0,.88);font-weight:600}
.aviso{background:#fffbe6;border:1px solid #ffe58f;border-radius:8px;padding:10px 16px;font-size:14px;
  display:flex;gap:10px;align-items:center;color:rgba(0,0,0,.88)}
.aviso .msi{color:#faad14}
.nota{font-size:13.5px;color:rgba(0,0,0,.65);margin:2px 0 6px;line-height:1.5;min-height:42px}
.nota b{color:rgba(0,0,0,.88);font-weight:600}
.ley{overflow-y:auto;padding-right:4px}
.ley-pastel{min-height:250px}
.ley-fila{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #f5f5f5;font-size:13.5px}
.ley-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;display:inline-block}
.ley-txt{flex:1;color:rgba(0,0,0,.88);min-width:0}
.ley-num{font-weight:600;color:rgba(0,0,0,.88);min-width:44px;text-align:right;font-variant-numeric:tabular-nums}
.ley-pct{color:rgba(0,0,0,.45);min-width:38px;text-align:right;font-variant-numeric:tabular-nums}
.ley-tot{border-bottom:none;border-top:1px solid #e8e8e8;margin-top:2px}
.ley-tot .ley-txt{color:rgba(0,0,0,.45)}
.rk-fila{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #f5f5f5;font-size:13.5px}
.rk-n{width:22px;color:rgba(0,0,0,.35);font-size:12px;text-align:right;flex-shrink:0}
.rk-txt{width:50%;line-height:1.35;color:rgba(0,0,0,.88)}
.rk-bar{flex:1;height:8px;background:#f5f5f5;border-radius:100px;overflow:hidden}
.rk-bar>span{display:block;height:100%;border-radius:100px}
.chip{display:inline-flex;align-items:center;gap:6px;margin-left:14px;white-space:nowrap}
.mes-ley{font-size:12.5px;padding:2px 0 6px}
.mes-ley>div{display:flex;align-items:center;gap:8px;padding:3px 0;color:rgba(0,0,0,.65)}
.mes-ley b{margin-left:auto;color:rgba(0,0,0,.88)}
.stat{height:100%}
.tabla-casos{overflow:auto;border:1px solid #f0f0f0;border-radius:8px;margin:6px 0 10px}
.tabla-casos table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px;min-width:1250px}
.tabla-casos th{position:sticky;top:0;background:#fafafa;color:rgba(0,0,0,.65);font-weight:600;text-align:left;
  padding:8px 10px;border-bottom:1px solid #f0f0f0;z-index:1;white-space:nowrap;height:35px}
.tabla-casos thead tr:nth-child(2) th{top:35px}
.tabla-casos th.grp{text-align:center}
.tabla-casos td{padding:8px 10px;border-bottom:1px solid #f0f0f0;vertical-align:top;color:rgba(0,0,0,.88)}
.tabla-casos .num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.tabla-casos td.cambio{font-weight:700}
.tabla-casos td.cambio .msi{vertical-align:-2px;margin-right:2px}
.tabla-casos .sep{border-left:1px solid #f0f0f0}
.tabla-casos .porque{min-width:250px;max-width:330px}
.tabla-casos .prod{min-width:160px;max-width:220px}
.tabla-casos .fechas{white-space:nowrap}
.tabla-casos .rz{display:flex;gap:6px;align-items:flex-start;line-height:1.35;padding:1px 0}
.tabla-casos .rz .msi{margin-top:2px}
.tabla-casos .sub{color:rgba(0,0,0,.45);font-size:12px;margin-top:2px}
.chip-sem{display:inline-block;padding:1px 8px;border:1px solid;border-radius:4px;font-weight:600;font-size:12px}
.ley-cambios{font-size:12.5px;color:rgba(0,0,0,.65);margin:2px 0 6px;line-height:2}
.chip-c{display:inline-block;padding:0 8px;border-radius:4px;font-weight:600;font-size:12px;margin-right:4px;
  line-height:20px}
.hist{margin:4px 0 6px 19px;padding:8px 10px;background:#f5f9ff;border:1px solid #d6e4ff;border-radius:6px;
  font-size:12.5px}
.hist-paso{display:flex;gap:8px;align-items:flex-start;padding:3px 0;line-height:1.35}
.hist-n{width:18px;height:18px;border-radius:50%;background:#1677ff;color:#fff;font-size:11px;font-weight:600;
  display:grid;place-items:center;flex-shrink:0;margin-top:2px}
.hist .sub{margin:0}
.hist-pie{margin-top:5px;padding-top:5px;border-top:1px dashed #d6e4ff;color:rgba(0,0,0,.65)}
</style>
"""


def mes_largo(m: str) -> str:
    """'2026-03' -> 'Marzo 2026'"""
    try:
        a, n = str(m).split("-")
        return f"{NOMBRE_MES[n]} {a}"
    except (ValueError, KeyError):
        return str(m)


def mes_corto(m: str) -> str:
    try:
        a, n = str(m).split("-")
        return f"{MESES[int(n) - 1]} {a[2:]}"
    except (ValueError, IndexError):
        return str(m)


def ic(nombre: str, color: str | None = None, tam: int | None = None) -> str:
    estilo_ = (f"color:{color};" if color else "") + (f"font-size:{tam}px;" if tam else "")
    return f"<span class='msi' style='{estilo_}'>{nombre}</span>"


def html(texto: str, dest=None) -> None:
    (dest or st).markdown(re.sub(r"\n\s*", " ", texto), unsafe_allow_html=True)


def color_semaforo(pct: float) -> str:
    return VERDE if pct < 10 else AMARILLO if pct < 25 else ROJO


def encabezado(titulo: str, sub: str, excel_de: str) -> None:
    c1, c2 = st.columns([5, 1], vertical_alignment="center")
    html(f"<div class='page-title'>{titulo}</div><div class='page-sub'>{sub}</div>", c1)
    c2.download_button("Exportar Excel", data=excel_para(excel_de), icon=":material/download:",
                       file_name=f"margenes_{excel_de.split()[0].lower()}.xlsx", width="stretch",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def seccion(texto: str) -> None:
    html(f"<div class='sec-label'>{texto}</div>")


_n_card = iter(range(10_000))


def card(titulo: str, icono: str, dest=None):
    """Tarjeta blanca con encabezado (como las de Ant Design)."""
    c = (dest or st).container(key=f"card_{next(_n_card)}")
    html(f"<div class='card-head'>{ic(icono)} {titulo}</div>", c)
    return c


def stat(dest, titulo: str, valor: str, icono: str, color: str, barra: float,
         nota: str = "", tendencia: float | None = None) -> None:
    t = ""
    if tendencia is not None and np.isfinite(tendencia):
        sube = tendencia >= 0
        t = (f"<span class='stat-trend' style='color:{VERDE if sube else ROJO}'>"
             f"{ic('arrow_upward' if sube else 'arrow_downward')}{abs(tendencia):.0f}%</span>")
    html(f"""<div class='stat'><div class='stat-title'>{titulo}</div>
        <div class='stat-row'>{ic(icono, color)}<span class='stat-val'>{valor}</span>{t}</div>
        <div class='stat-bar'><div style='width:{max(0, min(100, barra)):.0f}%;background:{color}'></div></div>
        <div class='stat-note'>{nota}</div></div>""", dest)


def anillo(dest, titulo: str, valor: str, pct: float, color: str, icono: str, nota: str = "") -> None:
    html(f"""<div class='stat ring-card'><div><div class='stat-title'>{titulo}</div>
        <div class='stat-row'>{ic(icono, color, 22)}<span class='stat-val'>{valor}</span></div>
        <div class='stat-note' style='margin-top:6px'>{nota}</div></div>
        <div class='ring' style='background:conic-gradient({color} {pct:.1f}%, #f0f0f0 0)'>
        <span>{pct:.0f}%</span></div></div>""", dest)


def estilo(fig, alto: int = 380, leyenda: bool = True):
    fig.update_layout(template="plotly_white", font=FUENTE, height=alto, showlegend=leyenda,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", title=None,
                      margin=dict(l=4, r=4, t=36 if leyenda else 10, b=4), legend_title_text="",
                      legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0))
    fig.update_xaxes(showgrid=False, linecolor="#f0f0f0", tickfont_color=TEXTO_2)
    fig.update_yaxes(gridcolor="#f0f0f0", griddash="dash", zeroline=False, tickfont_color=TEXTO_2)
    return fig


def mostrar(fig, dest=None) -> None:
    (dest or st).plotly_chart(fig, width=ANCHO, config={"displayModeBar": False})


def sube_baja(serie: pd.Series) -> pd.Series:
    return serie.map({"INCREMENTO": "Sube", "DECREMENTO": "Baja"}).fillna("Sin cambio")


def tipo_de_cambio(df_: pd.DataFrame) -> pd.Series:
    """Agrupa las combinaciones mínimo/máximo en 4 tipos fáciles de leer."""
    mn, mx = df_["DETERMINACION_MIN"], df_["DETERMINACION_MAX"]
    return pd.Series(np.select(
        [(mn == "DECREMENTO") & (mx == "INCREMENTO"), mn == "DECREMENTO", mx == "INCREMENTO",
         mn == "INCREMENTO", mx == "DECREMENTO"],
        ["Bajó mín. y subió máx.", "Bajó el mínimo", "Subió el máximo", "Subió el mínimo", "Bajó el máximo"],
        "Sin cambio en márgenes"), index=df_.index)


def inventario_corto(s_: str) -> str:
    u_ = str(s_).upper()
    if "OFERTA" in u_:
        return "Mercancía en oferta"
    if "TIENDAS" in u_:
        return "Mercancía para tiendas"
    if "INDIGENAS" in u_:
        return "INPI (pueblos indígenas)"
    if "MAIZ" in u_:
        return "Componente Maíz para Todos"
    if "DIF" in u_:
        return "DIF estatal"
    if "GOBIERNO" in u_:
        return "Gobierno estatal"
    return str(s_).strip().capitalize()


PALETA_LARGA = PALETA + ["#2f54eb", "#a0d911", "#fadb14", "#9254de", "#36cfc9", "#ff7a45", "#597ef7", "#73d13d",
                         "#f759ab", "#ffc53d", "#40a9ff", "#95de64"]


def conteo(serie: pd.Series, colores: dict | None) -> tuple[pd.Series, list]:
    """Cuenta todas las categorías (sin agrupar en 'Otros') y les asigna color."""
    vc = serie.value_counts()
    if colores:
        vc = vc.reindex([k for k in colores if k in vc.index] + [k for k in vc.index if k not in colores])
        cols = [colores.get(k, GRIS) for k in vc.index]
    else:
        cols = [PALETA_LARGA[i % len(PALETA_LARGA)] for i in range(len(vc))]
    return vc, cols


def conclusion(vc: pd.Series, unidad: str) -> str:
    top, tot = vc.sort_values(ascending=False), vc.sum()
    return (f"Lo más común: <b>{top.index[0]}</b> — {top.iloc[0]:,} de {tot:,} {unidad} "
            f"({100 * top.iloc[0] / tot:.0f}%)")


def pastel(serie: pd.Series, colores: dict | None = None, centro: str = "", alto: int = 240,
           unidad: str = "cambios"):
    """Gráfica de pastel (dona) sin leyenda; la leyenda va aparte, con números."""
    vc, cols = conteo(serie, colores)
    pct = 100 * vc / vc.sum()
    fig = go.Figure(go.Pie(
        labels=vc.index, values=vc.values, hole=0.6, sort=False, textposition="inside",
        text=[f"{p:.0f}%" if p >= 5 else "" for p in pct], textinfo="text",
        marker=dict(colors=cols, line=dict(color="#ffffff", width=2)), textfont=dict(size=13, color="#fff"),
        insidetextorientation="horizontal",
        hovertemplate="<b>%{label}</b><br>%{value:,} " + unidad + " (%{percent})<extra></extra>"))
    fig.update_layout(height=alto, font=FUENTE, showlegend=False, paper_bgcolor="rgba(0,0,0,0)",
                      margin=dict(l=0, r=0, t=4, b=4))
    if centro:
        fig.add_annotation(text=centro, showarrow=False, font=dict(size=15, color=TEXTO))
    return fig


def leyenda(vc: pd.Series, cols: list, unidad: str, alto: int) -> str:
    tot = vc.sum()
    filas = "".join(
        f"<div class='ley-fila'><span class='ley-dot' style='background:{c_}'></span>"
        f"<span class='ley-txt'>{k}</span><span class='ley-num'>{v:,}</span>"
        f"<span class='ley-pct'>{100 * v / tot:.0f}%</span></div>"
        for k, v, c_ in zip(vc.index, vc.values, cols))
    return (f"<div class='ley ley-pastel'>{filas}"
            f"<div class='ley-fila ley-tot'><span class='ley-dot'></span><span class='ley-txt'>Total</span>"
            f"<span class='ley-num'>{tot:,}</span><span class='ley-pct'>100%</span></div></div>")


def card_pastel(dest, titulo: str, icono: str, serie: pd.Series, colores: dict | None = None,
                unidad: str = "cambios", nota: str | None = None, alto: int = 240) -> None:
    """Tarjeta: frase con la conclusión, pastel a la izquierda y leyenda con números a la derecha."""
    cont = card(titulo, icono, dest)
    vc, cols = conteo(serie, colores)
    if vc.empty:
        html("<div class='nota'>Sin datos.</div>", cont)
        return
    html(f"<div class='nota'>{nota or conclusion(vc, unidad)}</div>", cont)
    mostrar(pastel(serie, colores, f"<b>{vc.sum():,}</b><br>{unidad}", alto, unidad), cont)
    html(leyenda(vc, cols, unidad, alto), cont)


def card_lista(dest, titulo: str, icono: str, serie: pd.Series, color: str = AZUL,
               unidad: str = "cambios", alto: int = 300) -> None:
    """Tarjeta con TODAS las categorías ordenadas de mayor a menor, con barra, número y porcentaje."""
    cont = card(titulo, icono, dest)
    vc = serie.value_counts()
    if vc.empty:
        html("<div class='nota'>Sin datos.</div>", cont)
        return
    tot, mx = vc.sum(), vc.max()
    filas = "".join(
        f"<div class='rk-fila'><span class='rk-n'>{i}</span><span class='rk-txt' title='{k}'>{k}</span>"
        f"<span class='rk-bar'><span style='width:{100 * v / mx:.0f}%;background:{color}'></span></span>"
        f"<span class='ley-num'>{v:,}</span><span class='ley-pct'>{100 * v / tot:.0f}%</span></div>"
        for i, (k, v) in enumerate(vc.items(), 1))
    completo = len(vc) <= 20
    aviso = "" if completo else f" · desliza la lista para ver los {len(vc)}"
    estilo_ = "" if completo else f"max-height:{alto}px"
    html(f"<div class='nota'>{conclusion(vc, unidad)} · {len(vc)} en total{aviso}</div>"
         f"<div class='ley' style='{estilo_}'>{filas}</div>", cont)


def preparar(df_: pd.DataFrame) -> pd.DataFrame:
    d_ = marcar(df_, U).copy()
    d_["Mes (texto)"] = d_["Mes"].map(mes_corto)
    d_["Mínimo"] = sube_baja(d_["DETERMINACION_MIN"]).map(
        {"Sube": "Subió el mínimo", "Baja": "Bajó el mínimo", "Sin cambio": "Mínimo sin cambio"})
    d_["Máximo"] = sube_baja(d_["DETERMINACION_MAX"]).map(
        {"Sube": "Subió el máximo", "Baja": "Bajó el máximo", "Sin cambio": "Máximo sin cambio"})
    d_["Tipo de cambio"] = tipo_de_cambio(d_)
    d_["Inventario (corto)"] = d_["DESCRIPCIÓN DEL INVENTARIO"].map(inventario_corto)
    d_["Región"] = d_["NOMBRE DE SUCURSAL"].str.title()
    d_["Producto"] = d_["DESCRIPCION DEL PRODUCTO"].str.title()
    d_["Revisar"] = np.select([d_["Núm. alertas"] >= 3, d_["Núm. alertas"] >= 1],
                              [ROJO_TXT, AMARILLO_TXT], VERDE_TXT)
    # cambio anterior del mismo producto en la misma U.O. e inventario (para "cambio repetido")
    g = d_.sort_values("FCHSOL").groupby("Clave producto-UO")
    for col in ["SCMARMINACT", "SCMARMINSOL", "SCMARMAXACT", "SCMARMAXSOL", "FCHSOL", "FCHAUT", "Autorizó",
                "SCFOLIO"]:
        d_[f"Prev {col}"] = g[col].shift()
    d_["Horas entre rango 2 y 3"] = (d_["FCHAUT"] - d_["Prev FCHAUT"]).dt.total_seconds() / 3600
    return d_


COLORES_MIN = {"Bajó el mínimo": AZUL, "Subió el mínimo": ROJO, "Mínimo sin cambio": GRIS}
COLORES_MAX = {"Subió el máximo": ROJO, "Bajó el máximo": AZUL, "Máximo sin cambio": GRIS}
VERDE_TXT = "Verde: sin señales"
AMARILLO_TXT = "Amarillo: 1 o 2 señales"
ROJO_TXT = "Rojo: 3 o más señales"
COLORES_REVISAR = {VERDE_TXT: VERDE, AMARILLO_TXT: AMARILLO, ROJO_TXT: ROJO}


def card_semaforo(dest, serie: pd.Series) -> None:
    """Semáforo de revisión, explicado paso a paso."""
    cont = card("Semáforo de revisión: ¿qué cambios conviene revisar?", "traffic", dest)
    vc = serie.value_counts()
    tot = int(vc.sum())
    n = {k: int(vc.get(k, 0)) for k in COLORES_REVISAR}
    html(f"""<div class='nota' style='min-height:0'>A cada cambio se le revisan <b>12 señales</b> de algo
        inusual (por ejemplo: <i>se autorizó en segundos</i>, <i>subieron mucho el margen máximo</i> o
        <i>el mismo producto se cambió otra vez en pocos días</i>). Según <b>cuántas señales tiene</b>,
        el cambio recibe un color:</div>""", cont)
    mostrar(pastel(serie, COLORES_REVISAR, f"<b>{tot:,}</b><br>cambios", 220), cont)
    filas = [
        (VERDE, "Verde", "Sin señales", "No tiene nada inusual. No requiere revisión.", n[VERDE_TXT]),
        (AMARILLO, "Amarillo", "1 o 2 señales",
         "Tiene algo inusual. Puede tener explicación, pero conviene preguntar.", n[AMARILLO_TXT]),
        (ROJO, "Rojo", "3 o más señales",
         "Junta varias cosas inusuales. Son los primeros que hay que revisar "
         "(están en la página <b>Casos a revisar</b>).", n[ROJO_TXT]),
    ]
    html("<div class='ley'>" + "".join(
        f"<div class='ley-fila' style='align-items:flex-start'>"
        f"<span class='ley-dot' style='background:{c_};margin-top:5px'></span>"
        f"<span class='ley-txt'><b>{col}: {qué}</b><br><span style='color:{TEXTO_2};font-size:12.5px'>"
        f"{desc}</span></span><span class='ley-num'>{k:,}</span>"
        f"<span class='ley-pct'>{100 * k / max(tot, 1):.0f}%</span></div>"
        for c_, col, qué, desc, k in filas) + "</div>"
        f"<div class='nota' style='min-height:0;margin-top:8px'><b>Ejemplo:</b> si en un cambio subieron mucho el "
        "máximo, se autorizó en segundos y el producto ya se había cambiado hace pocos días, tiene 3 señales "
        "y queda en <b>rojo</b>. El semáforo no dice que algo esté mal hecho; solo indica por dónde empezar a "
        "revisar.</div>", cont)
COLORES_INV = {"Mercancía para tiendas": AZUL, "INPI (pueblos indígenas)": NARANJA,
               "Mercancía en oferta": VERDE, "Gobierno estatal": MORADO, "Componente Maíz para Todos": AMARILLO,
               "DIF estatal": ROSA}


# ================================================================ secciones reutilizables
def bloque_resumen(x: pd.DataFrame, total_general: int) -> None:
    n, n_sol = len(x), x["Solicitud"].nunique()
    con = int((x["Núm. alertas"] > 0).sum())
    prio = int((x["Núm. alertas"] >= 3).sum())
    pm = x.groupby("Mes").size().sort_index()
    tend = 100 * (pm.iloc[-1] - pm.iloc[-2]) / pm.iloc[-2] if len(pm) > 1 and pm.iloc[-2] else None

    seccion("Resumen")
    c = st.columns(4, gap="medium")
    nota_tend = (f"{mes_largo(pm.index[-1]).split()[0]}: {pm.iloc[-1]:,} cambios vs. {pm.iloc[-2]:,} en "
                 f"{mes_largo(pm.index[-2]).split()[0].lower()}" if len(pm) > 1 else "")
    stat(c[0], "Cambios autorizados", f"{n:,}", "swap_vert", AZUL,
         100 * n / total_general, nota_tend, tend)
    stat(c[1], "Solicitudes (folios)", f"{n_sol:,}", "description", VERDE, 100 * n_sol / max(n, 1),
         f"En promedio {n / max(n_sol, 1):.1f} productos por solicitud")
    stat(c[2], "Cambios con alguna señal", f"{100 * con / n:.0f}%", "flag", color_semaforo(100 * con / n),
         100 * con / n, f"{con:,} de {n:,} cambios conviene revisarlos")
    stat(c[3], "Prioridad alta", f"{prio:,}", "error", ROJO, 100 * prio / n,
         f"{100 * prio / n:.1f}% de los cambios tiene 3 o más señales")

    seccion("¿Qué se hizo con los márgenes?")
    c = st.columns(3, gap="medium")
    for col, (tipo, icono, expl) in zip(c, [
            ("Bajó el mínimo", "south", "Se permite vender más barato"),
            ("Subió el máximo", "north", "Se permite vender más caro"),
            ("Bajó mín. y subió máx.", "unfold_more", "Se amplió el rango hacia los dos lados")]):
        k = int((x["Tipo de cambio"] == tipo).sum())
        anillo(col, tipo, f"{k:,} cambios", 100 * k / n, COLORES_TIPO[tipo], icono,
               f"{expl} · {k:,} de {n:,} cambios")


def bloque_pasteles(x: pd.DataFrame, con_quien: bool) -> None:
    c = st.columns(2, gap="medium")
    card_pastel(c[0], "Tipo de cambio", "swap_vert", x["Tipo de cambio"], COLORES_TIPO)
    card_semaforo(c[1], x["Revisar"])
    c = st.columns(2, gap="medium")
    card_pastel(c[0], "Margen mínimo", "south", x["Mínimo"], COLORES_MIN)
    card_pastel(c[1], "Margen máximo", "north", x["Máximo"], COLORES_MAX)
    c = st.columns(2, gap="medium")
    card_pastel(c[0], "Inventario", "inventory_2", x["Inventario (corto)"], COLORES_INV)
    if con_quien:
        card_pastel(c[1], "¿Quién autorizó?", "group", x["Autorizó"], COLORES_AUT)
    else:
        card_lista(c[1], "¿Quién pidió los cambios?", "person_raised_hand", x["Solicitó"].str.title(), MORADO)
    c = st.columns(2, gap="medium")
    card_lista(c[0], "Regiones", "map", x["Región"], CIAN)
    card_lista(c[1], "Productos", "shopping_cart", x["Producto"], NARANJA)


def mosaico_meses(x: pd.DataFrame, por_fila: int = 4) -> None:
    meses_x = sorted(x["Mes"].unique())
    ley = "".join(f"<span class='chip'><span class='ley-dot' style='background:{c_}'></span>{t}</span>"
                  for t, c_ in COLORES_TIPO.items())
    html(f"<div class='nota' style='margin:0 0 10px'>Cada pastel es un mes; el color muestra qué tipo de cambio se "
         f"hizo. {ley}</div>")
    for i in range(0, len(meses_x), por_fila):
        cols = st.columns(por_fila, gap="medium")
        for col, mes in zip(cols, meses_x[i:i + por_fila]):
            xm = x[x["Mes"] == mes]
            cont = card(mes_largo(mes), "calendar_month", col)
            mostrar(pastel(xm["Tipo de cambio"], COLORES_TIPO, f"<b>{len(xm):,}</b><br>cambios", 200), cont)
            vc = xm["Tipo de cambio"].value_counts()
            html("<div class='mes-ley'>" + "".join(
                f"<div><span class='ley-dot' style='background:{COLORES_TIPO[t]}'></span>{t}"
                f"<b>{vc.get(t, 0):,}</b></div>" for t in COLORES_TIPO if vc.get(t, 0)) + "</div>", cont)


def barras_mes(x: pd.DataFrame, por_persona: bool) -> None:
    orden = [mes_corto(m) for m in sorted(x["Mes"].unique())]
    pm_tot = x.groupby("Mes").size()
    nota = (f"El mes con más cambios fue <b>{mes_largo(pm_tot.idxmax())}</b> ({pm_tot.max():,}) y el de menos "
            f"<b>{mes_largo(pm_tot.idxmin())}</b> ({pm_tot.min():,}). Promedio: {pm_tot.mean():.0f} por mes.")
    if por_persona:
        pm = x.groupby(["Mes", "Mes (texto)", "Autorizó"]).size().reset_index(name="Cambios").sort_values("Mes")
        fig = px.bar(pm, x="Mes (texto)", y="Cambios", color="Autorizó", text="Cambios",
                     color_discrete_map=COLORES_AUT, category_orders={"Mes (texto)": orden})
    else:
        pm = x.groupby(["Mes", "Mes (texto)"]).size().reset_index(name="Cambios").sort_values("Mes")
        fig = px.bar(pm, x="Mes (texto)", y="Cambios", text="Cambios", category_orders={"Mes (texto)": orden})
        fig.update_traces(marker_color=AZUL)
    fig.update_traces(textposition="inside", textfont_size=13, marker_cornerradius=4,
                      hovertemplate="%{x}: %{y:,} cambios<extra></extra>")
    fig.update_xaxes(title="")
    fig.update_yaxes(title="Cambios")
    cont = card("Cambios autorizados por mes", "bar_chart")
    html(f"<div class='nota'>{nota}</div>", cont)
    mostrar(estilo(fig, 320, leyenda=por_persona), cont)


def mapa_regiones(x: pd.DataFrame) -> None:
    tm = (x.groupby(["Región", "NOMBRE DE UNIDAD OP."])
          .agg(Cambios=("Solicitud", "size"), Señales=("Núm. alertas", lambda s: round(100 * (s > 0).mean(), 0)))
          .reset_index())
    fig = px.treemap(tm, path=[px.Constant("Todas las regiones"), "Región", "NOMBRE DE UNIDAD OP."],
                     values="Cambios", color="Señales", range_color=[0, 60],
                     color_continuous_scale=[[0, "#d9f7be"], [0.5, "#fff1b8"], [1, "#ffa39e"]])
    fig.update_traces(texttemplate="<b>%{label}</b><br>%{value} cambios", textfont_size=13,
                      marker_line_color="#ffffff", marker_line_width=2,
                      hovertemplate="<b>%{label}</b><br>%{value} cambios<br>%{color:.0f}% con señal<extra></extra>")
    fig.update_layout(coloraxis_colorbar=dict(title="% con<br>señal", ticksuffix="%", thickness=12))
    cont = card("Mapa de regiones y unidades", "map")
    html("<div class='nota'>Cada cuadro es una región y, adentro, sus unidades operativas. "
         "<b>Más grande</b> = más cambios. <b>Más rojo</b> = más cambios con señales para revisar.</div>", cont)
    mostrar(estilo(fig, 460, leyenda=False), cont)


# ================================================================ páginas
def pagina_general() -> None:
    x = D
    encabezado("Resumen general",
               f"Daniel Montesinos y Beatriz Cervantes juntos · {mes_largo(x['Mes'].min())} a "
               f"{mes_largo(x['Mes'].max())}", "General")
    bloque_resumen(x, len(D))
    seccion("Detalle de los cambios")
    bloque_pasteles(x, con_quien=True)
    seccion("Por mes")
    barras_mes(x, por_persona=True)
    seccion("Por región")
    mapa_regiones(x)


def pagina_persona(nombre: str) -> None:
    x = D[D["Autorizó"] == nombre]
    if x.empty:
        st.info(f"No hay cambios autorizados por {nombre}.")
        return
    encabezado(nombre, f"Cambios que autorizó · {mes_largo(x['Mes'].min())} a {mes_largo(x['Mes'].max())}",
               nombre)
    bloque_resumen(x, len(D))
    seccion("Detalle de los cambios")
    bloque_pasteles(x, con_quien=False)
    seccion("Mes a mes")
    barras_mes(x, por_persona=False)
    mosaico_meses(x)
    seccion("Por región")
    mapa_regiones(x)


def pagina_mes() -> None:
    x = D
    encabezado("Mes a mes", "Qué tipo de cambios se hicieron en cada mes (las dos personas juntas)", "General")
    seccion("Cada mes")
    mosaico_meses(x)

    seccion("Un mes a detalle")
    meses_d = sorted(x["Mes"].unique())
    mes_sel = st.selectbox("Elige el mes", meses_d[::-1], format_func=mes_largo, key="mes_sel")
    xm = x[x["Mes"] == mes_sel]
    idx = meses_d.index(mes_sel)
    prev = x[x["Mes"] == meses_d[idx - 1]] if idx > 0 else None

    tipo = xm["Tipo de cambio"].value_counts()
    inv = xm["Inventario (corto)"].value_counts()
    reg = xm["Región"].value_counts()
    prod = xm["Producto"].value_counts()
    pct_sen = 100 * (xm["Núm. alertas"] > 0).mean()
    señales = pd.Series({f: int(xm[f].sum()) for f in ALERTAS}).sort_values(ascending=False)
    quien = xm["Autorizó"].value_counts()
    var = ""
    if prev is not None:
        dif = len(xm) - len(prev)
        var = (f"{ic('arrow_upward' if dif >= 0 else 'arrow_downward', VERDE if dif >= 0 else ROJO, 15)}"
               f" {abs(dif):,} {'más' if dif >= 0 else 'menos'} que en "
               f"{mes_largo(meses_d[idx - 1]).split()[0].lower()} ({len(prev):,})")
    items = [
        ("swap_vert", f"Se autorizaron <b>{len(xm):,} cambios</b> en <b>{xm['Solicitud'].nunique():,} "
                      "solicitudes</b>", var),
        ("group", "Autorizó: <b>" + ", ".join(f"{a} ({k:,})" for a, k in quien.items()) + "</b>", ""),
        ("category", f"Lo que más se hizo: <b>{tipo.index[0]}</b>",
         f"{tipo.iloc[0]:,} de {len(xm):,} cambios ({100 * tipo.iloc[0] / len(xm):.0f}%)"),
        ("inventory_2", f"Inventario principal: <b>{inv.index[0]}</b>",
         f"{inv.iloc[0]:,} de {len(xm):,} cambios ({100 * inv.iloc[0] / len(xm):.0f}%)"),
        ("map", f"Región con más cambios: <b>{reg.index[0]}</b>",
         f"{reg.iloc[0]:,} cambios · {len(reg)} regiones con cambios este mes"),
        ("shopping_cart", f"Producto más movido: <b>{prod.index[0]}</b>",
         f"{prod.iloc[0]:,} cambios · {len(prod)} productos distintos este mes"),
        ("flag", f"<b>{pct_sen:.0f}%</b> de los cambios tiene alguna señal",
         (f"La más común: {EXPLICA[señales.index[0]].lower()} ({señales.iloc[0]:,} cambios)"
          if señales.iloc[0] > 0 else "")),
    ]
    c1, c2 = st.columns(2, gap="medium")
    cont = card(f"{mes_largo(mes_sel)} en resumen", "summarize", c1)
    html("<ul class='lista'>" + "".join(
        f"<li><span class='dot'>{ic(i)}</span><div>{t}<small>{s}</small></div></li>" for i, t, s in items)
         + "</ul>", cont)
    card_pastel(c2, "Tipo de cambio", "swap_vert", xm["Tipo de cambio"], COLORES_TIPO, alto=300)
    seccion(f"Detalle de {mes_largo(mes_sel).lower()}")
    c = st.columns(2, gap="medium")
    card_semaforo(c[0], xm["Revisar"])
    card_pastel(c[1], "¿Quién autorizó?", "group", xm["Autorizó"], COLORES_AUT)
    c = st.columns(2, gap="medium")
    card_pastel(c[0], "Margen mínimo", "south", xm["Mínimo"], COLORES_MIN)
    card_pastel(c[1], "Margen máximo", "north", xm["Máximo"], COLORES_MAX)
    c = st.columns(2, gap="medium")
    card_pastel(c[0], "Inventario", "inventory_2", xm["Inventario (corto)"], COLORES_INV)
    card_lista(c[1], "Regiones", "map", xm["Región"], CIAN)
    card_lista(None, "Productos", "shopping_cart", xm["Producto"], NARANJA, alto=360)

    seccion("Todos los meses")
    tabla_mes = (x.groupby("Mes")
                 .agg(**{"Cambios": ("Solicitud", "size"), "Solicitudes": ("Solicitud", "nunique"),
                         "Tipo más común": ("Tipo de cambio", lambda s: s.value_counts().index[0]),
                         "Inventario principal": ("Inventario (corto)", lambda s: s.value_counts().index[0]),
                         "% con señal": ("Núm. alertas", lambda s: round(100 * (s > 0).mean()))})
                 .reset_index())
    tabla_mes["Mes"] = tabla_mes["Mes"].map(mes_largo)
    card("Resumen por mes", "table_chart").dataframe(
        tabla_mes, width=ANCHO, hide_index=True, height=38 * (len(tabla_mes) + 1) + 4,
        column_config={"% con señal": st.column_config.ProgressColumn(
            "% con señal", min_value=0, max_value=100, format="%d%%")})


def pagina_revisar() -> None:
    x = D
    encabezado("Qué revisar", "Señales encontradas en los cambios (las dos personas juntas)", "General")
    html(f"<div class='aviso'>{ic('info')}<div>Estas señales <b>no prueban una mala práctica</b>: indican "
         "cambios que vale la pena preguntar. Muchos tienen una explicación válida (temporada, ofertas, "
         "costos de la región). Un mismo cambio puede tener varias señales.</div></div>")
    seccion("Señales")
    sen = x.melt(value_vars=list(ALERTAS), var_name="Alerta", value_name="Sí")
    sen = sen[sen["Sí"]]
    c = st.columns(2, gap="medium")
    if sen.empty:
        st.success("No hay señales para revisar.")
    else:
        card_pastel(c[0], "¿Qué señales aparecen más?", "flag", sen["Alerta"].map(EXPLICA), unidad="señales",
                    alto=420)
    cont = card("¿Qué significa cada señal?", "help", c[1])
    html("<ul class='lista'>" + "".join(
        f"<li><span class='dot'>{ic(ICONOS[f])}</span><div>{EXPLICA[f]}<small>{t.format(**U)}</small></div></li>"
        for f, t in ALERTAS.items()) + "</ul>", cont)

    seccion("Tiempos y horarios")
    c = st.columns(2, gap="medium")
    sol = x.groupby("Solicitud")["Minutos para autorizar"].first().reset_index(name="Minutos")
    orden_t = ["Menos de 2 minutos", "De 2 minutos a 1 hora", "De 1 hora a 1 día", "De 1 a 3 días",
               "Más de 3 días"]
    sol["Tiempo"] = pd.cut(sol["Minutos"], [-1, 2, 60, 1440, 4320, 1e9], labels=orden_t).astype(str)
    card_pastel(c[0], "¿Cuánto se tarda en autorizar una solicitud?", "schedule", sol["Tiempo"],
                dict(zip(orden_t, [ROJO, "#95de64", VERDE, AMARILLO, NARANJA])), unidad="solicitudes")
    horario = pd.Series(np.where(x["Fuera de horario"], "Noche o fin de semana", "Horario normal"))
    card_pastel(c[1], "¿Cuándo se autoriza?", "dark_mode", horario,
                {"Horario normal": CIAN, "Noche o fin de semana": "#1d39c4"},
                nota=f"Horario normal = lunes a viernes de {U['u_hini']} a {U['u_hfin']} h. "
                     f"<b>{int(x['Fuera de horario'].sum()):,}</b> cambios se autorizaron fuera de ese horario.")

    seccion("Margen máximo: antes y después")
    fig = px.scatter(x, x="SCMARMAXACT", y="SCMARMAXSOL", color="Revisar", color_discrete_map=COLORES_REVISAR,
                     opacity=0.8, category_orders={"Revisar": list(COLORES_REVISAR)},
                     hover_data={"Producto": True, "Región": True, "Mes": True, "Alertas": True, "Revisar": False})
    lim = float(max(x["SCMARMAXACT"].max(), x["SCMARMAXSOL"].max()))
    fig.add_trace(go.Scatter(x=[0, lim], y=[0, lim], mode="lines", line=dict(dash="dot", color="#bfbfbf"),
                             name="Sin cambio"))
    fig.update_traces(marker_size=9, selector=dict(mode="markers"))
    fig.update_layout(xaxis_title="Margen máximo antes", yaxis_title="Margen máximo después")
    cont = card("Margen máximo antes y después del cambio", "scatter_plot")
    subio = int((x["SCMARMAXSOL"] > x["SCMARMAXACT"]).sum())
    html(f"<div class='nota'>Cada punto es un cambio. Los puntos <b>arriba de la línea gris</b> son subidas del "
         f"máximo ({subio:,}); entre más arriba, más grande la subida.</div>", cont)
    mostrar(estilo(fig, 440), cont)


def tabla_casos(r: pd.DataFrame) -> pd.DataFrame:
    """Cambios del semáforo con sus datos y la razón en palabras simples."""
    t = pd.DataFrame({
        "Semáforo": r["Revisar"].map({ROJO_TXT: "Rojo", AMARILLO_TXT: "Amarillo", VERDE_TXT: "Verde"}),
        "Señales": r["Núm. alertas"].astype(int),
        "¿Por qué?": r[list(ALERTAS)].apply(lambda f: " · ".join(EXPLICA[k] for k in ALERTAS if f[k]), axis=1),
        "Autorizó": r["Autorizó"],
        "Pidió": r["Solicitó"].str.title(),
        "Mes": r["Mes (texto)"],
        "Pedido el": r["FCHSOL"],
        "Autorizado el": r["FCHAUT"],
        "Tardó": r["Minutos para autorizar"].map(
            lambda m: "" if pd.isna(m) else f"{m:.0f} min" if m < 120 else
            f"{m / 60:.0f} h" if m < 2880 else f"{m / 1440:.0f} días"),
        "Región": r["Región"],
        "Unidad": r["NOMBRE DE UNIDAD OP."].str.title(),
        "Folio": r["SCFOLIO"],
        "Inventario": r["Inventario (corto)"],
        "Producto": r["Producto"],
        "Tipo de cambio": r["Tipo de cambio"],
        "Mín. antes": r["SCMARMINACT"], "Mín. después": r["SCMARMINSOL"], "Cambio mín.": r["Cambio mín."],
        "Máx. antes": r["SCMARMAXACT"], "Máx. después": r["SCMARMAXSOL"], "Cambio máx.": r["Cambio máx."],
    })
    # historial de los 3 rangos cuando el producto se volvió a cambiar en pocos días
    rep = r["Cambio repetido"] & r["Prev SCMARMINSOL"].notna()
    rango = lambda mn, mx: f"mín {mn:.1f} / máx {mx:.1f}"  # noqa: E731
    t["Rango 1 (original)"] = np.where(rep, [rango(a, b) for a, b in zip(r["Prev SCMARMINACT"],
                                                                          r["Prev SCMARMAXACT"])], "")
    t["Rango 2 (cambio anterior)"] = np.where(rep, [rango(a, b) for a, b in zip(r["Prev SCMARMINSOL"],
                                                                                 r["Prev SCMARMAXSOL"])], "")
    t["Rango 3 (este cambio)"] = np.where(rep, [rango(a, b) for a, b in zip(r["SCMARMINSOL"],
                                                                             r["SCMARMAXSOL"])], "")
    t["Cambio anterior autorizado el"] = r["Prev FCHAUT"].where(rep)
    t["Folio del cambio anterior"] = r["Prev SCFOLIO"].where(rep, "")
    t["Tiempo del rango 2 al 3"] = r["Horas entre rango 2 y 3"].where(rep).map(tiempo_txt)
    mn_ok = r["SCMARMINSOL"] == r["Prev SCMARMINACT"]
    mx_ok = r["SCMARMAXSOL"] == r["Prev SCMARMAXACT"]
    mn_mov = r["Prev SCMARMINSOL"] != r["Prev SCMARMINACT"]  # el cambio anterior sí movió el mínimo
    mx_mov = r["Prev SCMARMAXSOL"] != r["Prev SCMARMAXACT"]
    t["¿Regresó al rango original?"] = np.where(rep, np.select(
        [mn_ok & mx_ok, mx_ok & mx_mov, mn_ok & mn_mov],
        ["Sí, los dos márgenes", "Solo el máximo", "Solo el mínimo"], "No"), "")
    t["_rep"] = rep
    t["_r1"] = list(zip(r["Prev SCMARMINACT"], r["Prev SCMARMAXACT"]))
    t["_r2"] = list(zip(r["Prev SCMARMINSOL"], r["Prev SCMARMAXSOL"]))
    t["_r3"] = list(zip(r["SCMARMINSOL"], r["SCMARMAXSOL"]))
    return t.sort_values(["Señales", "Cambio máx."], ascending=False)


def tiempo_txt(h: float) -> str:
    if pd.isna(h):
        return ""
    if h < 1:
        return f"{h * 60:.0f} minutos"
    if h < 48:
        return f"{h:.0f} horas"
    return f"{h / 24:.0f} días"


def historial_html(r) -> str:
    """Los 3 rangos (original → cambio anterior → este cambio) y cuánto tardó en pasar al 3º."""
    def val(nuevo: float, viejo: float, etiqueta: str) -> str:
        c_ = "#389e0d" if nuevo > viejo else "#cf1322" if nuevo < viejo else "rgba(0,0,0,.88)"
        f_ = (ic("arrow_upward", c_, 12) if nuevo > viejo else ic("arrow_downward", c_, 12) if nuevo < viejo
              else "")
        return f"{etiqueta} <b style='color:{c_}'>{f_}{nuevo:.1f}</b>"

    (a1, b1), (a2, b2), (a3, b3) = r["_r1"], r["_r2"], r["_r3"]
    fecha_ant = r["Cambio anterior autorizado el"]
    fecha_ant = "" if pd.isna(fecha_ant) else fecha_ant.strftime("%d/%m/%Y")
    fecha_act = "" if pd.isna(r["Autorizado el"]) else r["Autorizado el"].strftime("%d/%m/%Y")
    regreso = r["¿Regresó al rango original?"]
    return (
        "<div class='hist'>"
        f"<div class='hist-paso'><span class='hist-n'>1</span><div><span class='sub'>Rango original</span><br>"
        f"mín <b>{a1:.1f}</b> · máx <b>{b1:.1f}</b></div></div>"
        f"<div class='hist-paso'><span class='hist-n'>2</span><div><span class='sub'>Cambio anterior · {fecha_ant}"
        f"</span><br>{val(a2, a1, 'mín')} · {val(b2, b1, 'máx')}</div></div>"
        f"<div class='hist-paso'><span class='hist-n'>3</span><div><span class='sub'>Este cambio · {fecha_act}"
        f"</span><br>{val(a3, a2, 'mín')} · {val(b3, b2, 'máx')}</div></div>"
        f"<div class='hist-pie'>{ic('schedule', None, 13)} Del rango 2 al 3 pasaron "
        f"<b>{r['Tiempo del rango 2 al 3']}</b>"
        + {"Sí, los dos márgenes": " · <b style='color:#cf1322'>regresó al rango original</b>",
           "Solo el máximo": " · <b style='color:#cf1322'>el máximo regresó a su valor original</b>",
           "Solo el mínimo": " · <b style='color:#cf1322'>el mínimo regresó a su valor original</b>"}.get(regreso, "")
        + "</div></div>")


# umbrales de intensidad (los mismos que usan las señales)
FUERZA = {"mín": (3, U["u_dmin"]), "máx": (10, U["u_dmax"])}
TONOS = {  # (fondo, texto) por intensidad: leve, media, fuerte
    "sube": [("#f6ffed", "#389e0d"), ("#b7eb8f", "#135200"), ("#389e0d", "#ffffff")],
    "baja": [("#fff1f0", "#cf1322"), ("#ffa39e", "#820014"), ("#cf1322", "#ffffff")],
}


def celda_cambio(v: float, cual: str) -> str:
    if pd.isna(v):
        return "<td></td>"
    if v == 0:
        return "<td class='num' style='color:rgba(0,0,0,.35)'>0</td>"
    medio, fuerte = FUERZA[cual]
    nivel = 2 if abs(v) >= fuerte else 1 if abs(v) >= medio else 0
    fondo, txt = TONOS["sube" if v > 0 else "baja"][nivel]
    flecha = "arrow_upward" if v > 0 else "arrow_downward"
    return (f"<td class='num cambio' style='background:{fondo};color:{txt}'>"
            f"{ic(flecha, txt, 14)}{v:+.1f}</td>")


def celda_despues(antes: float, despues: float) -> str:
    if pd.isna(despues):
        return "<td></td>"
    color = "#389e0d" if despues > antes else "#cf1322" if despues < antes else "rgba(0,0,0,.88)"
    return f"<td class='num' style='color:{color};font-weight:600'>{despues:.1f}</td>"


def mostrar_casos(dest, t: pd.DataFrame, alto: int = 700) -> None:
    """Tabla HTML: '¿Por qué?' en varias líneas y cambios de margen en verde (sube) / rojo (baja)."""
    chip = {"Rojo": ("#fff1f0", "#cf1322", "#ffa39e"), "Amarillo": ("#fffbe6", "#d48806", "#ffe58f"),
            "Verde": ("#f6ffed", "#389e0d", "#b7eb8f")}

    def fecha(f) -> str:
        return "" if pd.isna(f) else f.strftime("%d/%m/%Y %H:%M")

    filas = []
    for _, r in t.iterrows():
        fo, tx, bo = chip.get(r["Semáforo"], ("#fff", "#000", "#ddd"))
        razones = "".join(
            f"<div class='rz'>{ic('flag', tx, 13)}<span>{escape(z)}</span></div>"
            + (historial_html(r) if z == EXPLICA["Cambio repetido"] and r["_rep"] else "")
            for z in str(r["¿Por qué?"]).split(" · ") if z)
        n_sen = int(r["Señales"])
        filas.append(
            "<tr>"
            f"<td><span class='chip-sem' style='background:{fo};color:{tx};border-color:{bo}'>{r['Semáforo']}</span>"
            f"<div class='sub'>{n_sen} {'señal' if n_sen == 1 else 'señales'}</div></td>"
            f"<td class='porque'>{razones}</td>"
            f"<td class='prod'><b>{escape(str(r['Producto']))}</b>"
            f"<div class='sub'>{escape(str(r['Inventario']))}</div></td>"
            f"<td class='num sep'>{r['Mín. antes']:.1f}</td>{celda_despues(r['Mín. antes'], r['Mín. después'])}"
            f"{celda_cambio(r['Cambio mín.'], 'mín')}"
            f"<td class='num sep'>{r['Máx. antes']:.1f}</td>{celda_despues(r['Máx. antes'], r['Máx. después'])}"
            f"{celda_cambio(r['Cambio máx.'], 'máx')}"
            f"<td class='sep'>{escape(str(r['Autorizó']))}"
            f"<div class='sub'>Pidió: {escape(str(r['Pidió']))}</div></td>"
            f"<td class='fechas'>Pedido: {fecha(r['Pedido el'])}<div class='sub'>Autorizado: {fecha(r['Autorizado el'])}"
            f"</div><div class='sub'>Tardó: {r['Tardó']}</div></td>"
            f"<td>{escape(str(r['Región']))}<div class='sub'>{escape(str(r['Unidad']))}</div></td>"
            f"<td>{escape(str(r['Folio']))}<div class='sub'>{r['Mes']}</div></td>"
            "</tr>")
    cab = ("<tr><th rowspan='2'>Semáforo</th><th rowspan='2'>¿Por qué?</th><th rowspan='2'>Producto</th>"
           "<th colspan='3' class='grp sep'>Margen mínimo</th><th colspan='3' class='grp sep'>Margen máximo</th>"
           "<th rowspan='2' class='sep'>Autorizó</th><th rowspan='2'>Fechas</th>"
           "<th rowspan='2'>Región / unidad</th><th rowspan='2'>Folio / mes</th></tr>"
           "<tr><th class='num sep'>Antes</th><th class='num'>Después</th><th class='num'>Cambio</th>"
           "<th class='num sep'>Antes</th><th class='num'>Después</th><th class='num'>Cambio</th></tr>")
    html(f"<div class='tabla-casos' style='max-height:{alto}px'><table><thead>{cab}</thead>"
         f"<tbody>{''.join(filas)}</tbody></table></div>", dest)


def leyenda_cambios() -> str:
    m1, f1 = FUERZA["mín"]
    m2, f2 = FUERZA["máx"]
    s, b = TONOS["sube"], TONOS["baja"]

    def caja(fo: str, tx: str, t_: str) -> str:
        return f"<span class='chip-c' style='background:{fo};color:{tx}'>{t_}</span>"

    return (
        "<div class='ley-cambios'><b>Colores de la columna Cambio:</b> "
        "<b style='color:#389e0d'>verde = el margen subió</b> · <b style='color:#cf1322'>rojo = el margen bajó</b>"
        " · entre más fuerte el color, más grande el cambio.<br>"
        f"{caja(*s[0], 'Subió poco')}{caja(*s[1], 'Subió medio')}{caja(*s[2], 'Subió mucho')}&nbsp;&nbsp;"
        f"{caja(*b[0], 'Bajó poco')}{caja(*b[1], 'Bajó medio')}{caja(*b[2], 'Bajó mucho')}<br>"
        f"<span style='color:rgba(0,0,0,.45)'>Mínimo: medio desde {m1:g} pts, mucho desde {f1:g} pts · "
        f"Máximo: medio desde {m2:g} pts, mucho desde {f2:g} pts</span></div>")


def pagina_casos() -> None:
    x = D
    r = x[x["Núm. alertas"] >= 1]
    rojo, amarillo = r[r["Revisar"] == ROJO_TXT], r[r["Revisar"] == AMARILLO_TXT]
    encabezado("Casos a revisar",
               "Todos los cambios en amarillo y rojo del semáforo de revisión, con sus datos y la razón", "General")
    html(f"<div class='aviso'>{ic('info')}<div>Aquí están los cambios que el <b>semáforo de revisión</b> marcó en "
         "<b>rojo</b> (3 o más señales, revisar primero) y en <b>amarillo</b> (1 o 2 señales, conviene preguntar). "
         "La columna <b>¿Por qué?</b> dice qué señales tiene cada cambio.</div></div>")
    seccion("Resumen")
    c = st.columns(4, gap="medium")
    stat(c[0], "Rojo: 3 o más señales", f"{len(rojo):,}", "error", ROJO, 100 * len(rojo) / len(x),
         f"{100 * len(rojo) / len(x):.0f}% de los {len(x):,} cambios · revisar primero")
    stat(c[1], "Amarillo: 1 o 2 señales", f"{len(amarillo):,}", "warning", AMARILLO, 100 * len(amarillo) / len(x),
         f"{100 * len(amarillo) / len(x):.0f}% de los {len(x):,} cambios · conviene preguntar")
    stat(c[2], "Solicitudes involucradas", f"{r['Solicitud'].nunique():,}", "description", AZUL,
         100 * r["Solicitud"].nunique() / x["Solicitud"].nunique(),
         f"de {x['Solicitud'].nunique():,} solicitudes en total")
    stat(c[3], "Productos distintos", f"{r['CVE. DE PRODUCTO'].nunique():,}", "shopping_cart", MORADO,
         100 * r["CVE. DE PRODUCTO"].nunique() / x["CVE. DE PRODUCTO"].nunique(),
         f"de {x['CVE. DE PRODUCTO'].nunique():,} productos con cambios")
    if r.empty:
        st.success("No hay cambios en amarillo ni en rojo.")
        return

    seccion("¿Cómo son estos casos? (amarillo y rojo juntos)")
    c = st.columns(2, gap="medium")
    card_pastel(c[0], "¿En qué color están?", "traffic", r["Revisar"],
                {ROJO_TXT: ROJO, AMARILLO_TXT: AMARILLO}, unidad="casos")
    card_pastel(c[1], "¿Quién los autorizó?", "group", r["Autorizó"], COLORES_AUT, unidad="casos")
    c = st.columns(2, gap="medium")
    sen = r.melt(value_vars=list(ALERTAS), var_name="Alerta", value_name="Sí")
    card_lista(c[0], "¿Qué señales tienen?", "flag", sen.loc[sen["Sí"], "Alerta"].map(EXPLICA), ROJO,
               unidad="señales")
    card_pastel(c[1], "¿Qué tipo de cambio son?", "swap_vert", r["Tipo de cambio"], COLORES_TIPO, unidad="casos")
    c = st.columns(2, gap="medium")
    card_lista(c[0], "Regiones", "map", r["Región"], CIAN, unidad="casos")
    card_lista(c[1], "Productos", "shopping_cart", r["Producto"], NARANJA, unidad="casos")

    seccion("Lista de casos")
    cont = card(f"Rojo: 3 o más señales · {len(rojo):,} cambios (revisar primero)", "error")
    html("<div class='nota' style='min-height:0'>Ordenados del que tiene más señales al que tiene menos.</div>"
         + leyenda_cambios(), cont)
    mostrar_casos(cont, tabla_casos(rojo))
    cont = card(f"Amarillo: 1 o 2 señales · {len(amarillo):,} cambios (conviene preguntar)", "warning")
    html("<div class='nota' style='min-height:0'>Ordenados del que tiene más señales al que tiene menos. "
         "Desliza hacia abajo dentro de la tabla para ver todos.</div>" + leyenda_cambios(), cont)
    mostrar_casos(cont, tabla_casos(amarillo))
    st.download_button("Descargar la lista en Excel (amarillo y rojo)", icon=":material/download:",
                       data=casos_excel(), file_name="casos_a_revisar.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@st.cache_data(ttl=3600, show_spinner="Preparando Excel…")
def casos_excel() -> bytes:
    r = D[D["Núm. alertas"] >= 1]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for nombre, sub in [("Rojo", r[r["Revisar"] == ROJO_TXT]), ("Amarillo", r[r["Revisar"] == AMARILLO_TXT])]:
            t = tabla_casos(sub).drop(columns=["_rep", "_r1", "_r2", "_r3"])
            t.to_excel(xw, sheet_name=nombre, index=False)
            ws = xw.book[nombre]
            ws.freeze_panes = "A2"
            from openpyxl.styles import Alignment, Font, PatternFill
            col_pq = list(t.columns).index("¿Por qué?") + 1
            for (cual, nombre_col) in [("mín", "Cambio mín."), ("máx", "Cambio máx.")]:
                ci = list(t.columns).index(nombre_col) + 1
                medio, fuerte = FUERZA[cual]
                for fila in range(2, len(t) + 2):
                    celda = ws.cell(fila, ci)
                    v = celda.value
                    if v is None or v == 0 or pd.isna(v):
                        continue
                    nivel = 2 if abs(v) >= fuerte else 1 if abs(v) >= medio else 0
                    fondo, txt = TONOS["sube" if v > 0 else "baja"][nivel]
                    celda.fill = PatternFill("solid", fgColor=fondo.lstrip("#"))
                    celda.font = Font(bold=True, color=txt.lstrip("#"))
            for fila in range(2, len(t) + 2):
                c_ = ws.cell(fila, col_pq)
                c_.value = str(c_.value or "").replace(" · ", "\n")
                c_.alignment = Alignment(wrap_text=True, vertical="top")
            ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    60, max(10, max(len(str(c.value or "")) for c in col[:200]) + 2))
    return buf.getvalue()


@st.cache_data(ttl=3600, show_spinner="Preparando Excel…")
def excel_para(quien: str) -> bytes:
    return a_excel(D if quien == "General" else D[D["Autorizó"] == quien])


# ================================================================ app
html(CSS)
try:
    st.logo("logo.svg", size="large")
except Exception:  # noqa: BLE001
    pass

if not check_password():
    st.stop()

D = preparar(cargar())
if D.empty:
    st.warning("No hay movimientos en el archivo.")
    st.stop()

personas = [a for a in ["Daniel Montesinos", "Beatriz Cervantes"] if a in set(D["Autorizó"])]
personas += sorted(set(D["Autorizó"]) - set(personas))
paginas = {
    "General": [
        st.Page(pagina_general, title="Resumen general", icon=":material/dashboard:", url_path="general",
                default=True),
        st.Page(pagina_mes, title="Mes a mes", icon=":material/calendar_month:", url_path="mes-a-mes"),
    ],
    "Por persona": [
        st.Page((lambda n=a: pagina_persona(n)), title=a, icon=":material/person:",
                url_path=re.sub(r"[^a-z]+", "-", a.lower()).strip("-"))
        for a in personas
    ],
    "Revisión": [
        st.Page(pagina_revisar, title="Qué revisar", icon=":material/flag:", url_path="que-revisar"),
        st.Page(pagina_casos, title="Casos a revisar", icon=":material/fact_check:", url_path="casos"),
    ],
}
st.navigation(paginas).run()
