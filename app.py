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
                "Otros cambios": GRIS}
COLORES_REVISAR = {"Sin señales": VERDE, "Revisar": AMARILLO, "Revisar con prioridad": ROJO}
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
.stat{background:#ffffff;border-radius:8px;padding:22px 24px;min-height:150px;
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


def anillo(dest, titulo: str, valor: str, pct: float, color: str, icono: str) -> None:
    html(f"""<div class='stat ring-card'><div><div class='stat-title'>{titulo}</div>
        <div class='stat-row'>{ic(icono, TEXTO, 22)}<span class='stat-val'>{valor}</span></div></div>
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
    baja_min, sube_max, ambos, otros = COLORES_TIPO
    return pd.Series(np.select(
        [(mn == "DECREMENTO") & (mx == "INCREMENTO"), mn == "DECREMENTO", mx == "INCREMENTO"],
        [ambos, baja_min, sube_max], otros), index=df_.index)


def inventario_corto(s_: str) -> str:
    u_ = str(s_).upper()
    if "OFERTA" in u_:
        return "Mercancía en oferta"
    if "TIENDAS" in u_:
        return "Mercancía para tiendas"
    if "INDIGENAS" in u_:
        return "INPI (pueblos indígenas)"
    return "Otros"


def top_n(serie: pd.Series, n: int = 6) -> pd.Series:
    """Deja los n valores más frecuentes y junta el resto en 'Otros'."""
    return serie.where(serie.isin(serie.value_counts().index[:n]), "Otros")


def pastel(serie: pd.Series, colores: dict | None = None, centro: str = "", alto: int = 320,
           leyenda: bool = True, unidad: str = "cambios"):
    """Gráfica de pastel (dona)."""
    vc = serie.value_counts()
    if colores:
        vc = vc.reindex([k for k in colores if k in vc.index] + [k for k in vc.index if k not in colores])
        cols = [colores.get(k, GRIS) for k in vc.index]
    else:
        if "Otros" in vc.index:
            vc = pd.concat([vc.drop("Otros"), vc[["Otros"]]])
        cols = [(GRIS if k == "Otros" else PALETA[i % len(PALETA)]) for i, k in enumerate(vc.index)]
    fig = go.Figure(go.Pie(
        labels=vc.index, values=vc.values, hole=0.62, sort=False, textinfo="percent", textposition="inside",
        marker=dict(colors=cols, line=dict(color="#ffffff", width=2)), textfont=dict(size=13, color="#fff"),
        insidetextorientation="horizontal",
        hovertemplate="<b>%{label}</b><br>%{value:,} " + unidad + " (%{percent})<extra></extra>"))
    fig.update_layout(height=alto, font=FUENTE, showlegend=leyenda, paper_bgcolor="rgba(0,0,0,0)",
                      margin=dict(l=4, r=4, t=8, b=4), uniformtext_minsize=11, uniformtext_mode="hide",
                      legend=dict(orientation="h", yanchor="top", y=-0.02, x=0.5, xanchor="center"))
    if centro:
        fig.add_annotation(text=centro, showarrow=False, font=dict(size=15, color=TEXTO))
    return fig


def card_pastel(dest, titulo: str, icono: str, serie: pd.Series, colores: dict | None = None,
                centro: str = "", alto: int = 320, unidad: str = "cambios") -> None:
    mostrar(pastel(serie, colores, centro, alto, unidad=unidad), card(titulo, icono, dest))


def preparar(df_: pd.DataFrame) -> pd.DataFrame:
    d_ = marcar(df_, U).copy()
    d_["Mes (texto)"] = d_["Mes"].map(mes_corto)
    d_["Mínimo"] = sube_baja(d_["DETERMINACION_MIN"])
    d_["Máximo"] = sube_baja(d_["DETERMINACION_MAX"])
    d_["Tipo de cambio"] = tipo_de_cambio(d_)
    d_["Inventario (corto)"] = d_["DESCRIPCIÓN DEL INVENTARIO"].map(inventario_corto)
    d_["Revisar"] = np.select([d_["Núm. alertas"] >= 3, d_["Núm. alertas"] >= 1],
                              ["Revisar con prioridad", "Revisar"], "Sin señales")
    return d_


# ================================================================ secciones reutilizables
def bloque_resumen(x: pd.DataFrame, total_general: int) -> None:
    n, n_sol = len(x), x["Solicitud"].nunique()
    con = int((x["Núm. alertas"] > 0).sum())
    prio = int((x["Núm. alertas"] >= 3).sum())
    pm = x.groupby("Mes").size().sort_index()
    tend = 100 * (pm.iloc[-1] - pm.iloc[-2]) / pm.iloc[-2] if len(pm) > 1 and pm.iloc[-2] else None

    seccion("Resumen")
    c = st.columns(4)
    stat(c[0], "Cambios autorizados", f"{n:,}", "swap_vert", AZUL, 100 * n / total_general,
         f"{100 * n / total_general:.0f}% del total · flecha: último mes", tend)
    stat(c[1], "Solicitudes", f"{n_sol:,}", "description", VERDE, 100 * n_sol / max(n, 1),
         f"{n / max(n_sol, 1):.1f} productos por solicitud")
    stat(c[2], "Con alguna señal", f"{100 * con / n:.0f}%", "flag", color_semaforo(100 * con / n),
         100 * con / n, f"{con:,} cambios para revisar")
    stat(c[3], "Prioridad alta", f"{prio:,}", "error", ROJO, 100 * prio / n, "cambios con 3 o más señales")

    seccion("Tipo de movimiento")
    c = st.columns(3)
    for col, (tipo, icono) in zip(c, [("Bajó el mínimo", "south"), ("Subió el máximo", "north"),
                                      ("Bajó mín. y subió máx.", "unfold_more")]):
        k = int((x["Tipo de cambio"] == tipo).sum())
        anillo(col, tipo, f"{k:,} cambios", 100 * k / n, COLORES_TIPO[tipo], icono)


def mosaico_meses(x: pd.DataFrame, por_fila: int = 4) -> None:
    meses_x = sorted(x["Mes"].unique())
    leyenda = "&nbsp;&nbsp;&nbsp;".join(
        f"<span style='display:inline-flex;align-items:center;gap:6px'><span style='width:10px;height:10px;"
        f"border-radius:50%;background:{c_}'></span>{t}</span>" for t, c_ in COLORES_TIPO.items())
    html(f"<div style='font-size:13px;color:{TEXTO_2};margin-bottom:8px'>{leyenda}</div>")
    for i in range(0, len(meses_x), por_fila):
        cols = st.columns(por_fila)
        for col, mes in zip(cols, meses_x[i:i + por_fila]):
            xm = x[x["Mes"] == mes]
            cont = card(mes_largo(mes), "calendar_month", col)
            mostrar(pastel(xm["Tipo de cambio"], COLORES_TIPO, f"<b>{len(xm):,}</b><br>cambios", 230,
                           leyenda=False), cont)
            dom = xm["Tipo de cambio"].value_counts()
            html(f"<p class='mes-nota'>Lo más común<br><b>{dom.index[0]}</b> "
                 f"({100 * dom.iloc[0] / len(xm):.0f}%)</p>", cont)


def barras_mes(x: pd.DataFrame, por_persona: bool) -> None:
    orden = [mes_corto(m) for m in sorted(x["Mes"].unique())]
    if por_persona:
        pm = x.groupby(["Mes", "Mes (texto)", "Autorizó"]).size().reset_index(name="Cambios").sort_values("Mes")
        fig = px.bar(pm, x="Mes (texto)", y="Cambios", color="Autorizó", text="Cambios",
                     color_discrete_map=COLORES_AUT, category_orders={"Mes (texto)": orden})
    else:
        pm = x.groupby(["Mes", "Mes (texto)"]).size().reset_index(name="Cambios").sort_values("Mes")
        fig = px.bar(pm, x="Mes (texto)", y="Cambios", text="Cambios", category_orders={"Mes (texto)": orden})
        fig.update_traces(marker_color=AZUL)
    fig.update_traces(textposition="inside", textfont_size=13, marker_cornerradius=4)
    fig.update_xaxes(title="")
    fig.update_yaxes(title="")
    mostrar(estilo(fig, 340, leyenda=por_persona), card("Cambios autorizados por mes", "bar_chart"))


def mapa_regiones(x: pd.DataFrame) -> None:
    tm = (x.groupby(["NOMBRE DE SUCURSAL", "NOMBRE DE UNIDAD OP."])
          .agg(Cambios=("Solicitud", "size"), Señales=("Núm. alertas", lambda s: round(100 * (s > 0).mean(), 0)))
          .reset_index())
    fig = px.treemap(tm, path=[px.Constant("Todas"), "NOMBRE DE SUCURSAL", "NOMBRE DE UNIDAD OP."],
                     values="Cambios", color="Señales", range_color=[0, 60],
                     color_continuous_scale=[[0, "#d9f7be"], [0.5, "#fff1b8"], [1, "#ffa39e"]])
    fig.update_traces(texttemplate="<b>%{label}</b><br>%{value} cambios", textfont_size=13,
                      marker_line_color="#ffffff", marker_line_width=2)
    fig.update_layout(coloraxis_colorbar=dict(title="% con señal", ticksuffix="%", thickness=12))
    cont = card("¿Dónde se hacen más cambios?", "map")
    html(f"<p style='font-size:13px;color:{TEXTO_2};margin:4px 0'>Cada cuadro es una región y, adentro, sus "
         "unidades operativas. Más grande = más cambios. Más rojo = más señales para revisar.</p>", cont)
    mostrar(estilo(fig, 480, leyenda=False), cont)


# ================================================================ páginas
def pagina_general() -> None:
    x = D
    encabezado("Resumen general",
               f"Daniel Montesinos y Beatriz Cervantes juntos · {mes_largo(x['Mes'].min())} a "
               f"{mes_largo(x['Mes'].max())}", "General")
    bloque_resumen(x, len(D))
    c = st.columns(3)
    card_pastel(c[0], "Tipo de cambio", "swap_vert", x["Tipo de cambio"], COLORES_TIPO, f"<b>{len(x):,}</b><br>cambios")
    card_pastel(c[1], "Semáforo", "traffic", x["Revisar"], COLORES_REVISAR, f"<b>{len(x):,}</b><br>cambios")
    card_pastel(c[2], "¿Quién autorizó?", "group", x["Autorizó"], COLORES_AUT, f"<b>{len(x):,}</b><br>cambios")
    c = st.columns(3)
    card_pastel(c[0], "Margen mínimo", "south", x["Mínimo"], COLORES_SUBE_BAJA)
    card_pastel(c[1], "Margen máximo", "north", x["Máximo"], COLORES_SUBE_BAJA)
    card_pastel(c[2], "Inventario", "inventory_2", x["Inventario (corto)"])
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
    c = st.columns(3)
    card_pastel(c[0], "Tipo de cambio", "swap_vert", x["Tipo de cambio"], COLORES_TIPO, f"<b>{len(x):,}</b><br>cambios")
    card_pastel(c[1], "Semáforo", "traffic", x["Revisar"], COLORES_REVISAR, f"<b>{len(x):,}</b><br>cambios")
    card_pastel(c[2], "Inventario", "inventory_2", x["Inventario (corto)"])
    c = st.columns(3)
    card_pastel(c[0], "Margen mínimo", "south", x["Mínimo"], COLORES_SUBE_BAJA)
    card_pastel(c[1], "Margen máximo", "north", x["Máximo"], COLORES_SUBE_BAJA)
    card_pastel(c[2], "¿Quién le pide los cambios?", "person_raised_hand", top_n(x["Solicitó"], 5))
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
    reg = xm["NOMBRE DE SUCURSAL"].value_counts()
    prod = xm["DESCRIPCION DEL PRODUCTO"].value_counts()
    pct_sen = 100 * (xm["Núm. alertas"] > 0).mean()
    señales = pd.Series({f: int(xm[f].sum()) for f in ALERTAS}).sort_values(ascending=False)
    quien = xm["Autorizó"].value_counts()
    var = ""
    if prev is not None:
        dif = len(xm) - len(prev)
        var = (f" · {ic('arrow_upward' if dif >= 0 else 'arrow_downward', VERDE if dif >= 0 else ROJO, 15)}"
               f"{abs(dif):,} respecto a {mes_largo(meses_d[idx - 1]).split()[0].lower()}")
    items = [
        ("swap_vert", f"<b>{len(xm):,} cambios</b> en <b>{xm['Solicitud'].nunique():,} solicitudes</b>", var),
        ("group", "Autorizó: <b>" + ", ".join(f"{a} ({k:,})" for a, k in quien.items()) + "</b>", ""),
        ("category", f"Lo que más se hizo: <b>{tipo.index[0]}</b>",
         f"{100 * tipo.iloc[0] / len(xm):.0f}% de los cambios"),
        ("inventory_2", f"Inventario principal: <b>{inv.index[0]}</b>", f"{100 * inv.iloc[0] / len(xm):.0f}%"),
        ("map", f"Región con más cambios: <b>{reg.index[0].title()}</b>", f"{reg.iloc[0]:,} cambios"),
        ("shopping_cart", "Productos más movidos: <b>" + ", ".join(p.title() for p in prod.index[:3]) + "</b>", ""),
        ("flag", f"<b>{pct_sen:.0f}%</b> de los cambios tienen alguna señal",
         (f"La más común: {EXPLICA[señales.index[0]].lower()} ({señales.iloc[0]:,})"
          if señales.iloc[0] > 0 else "")),
    ]
    c1, c2 = st.columns([2, 3])
    cont = card(f"{mes_largo(mes_sel)} en resumen", "summarize", c1)
    html("<ul class='lista'>" + "".join(
        f"<li><span class='dot'>{ic(i)}</span><div>{t}<small>{s}</small></div></li>" for i, t, s in items)
         + "</ul>", cont)
    with c2:
        c = st.columns(2)
        card_pastel(c[0], "Tipo de cambio", "swap_vert", xm["Tipo de cambio"], COLORES_TIPO, alto=300)
        card_pastel(c[1], "Semáforo", "traffic", xm["Revisar"], COLORES_REVISAR, alto=300)
    c = st.columns(3)
    card_pastel(c[0], "¿Quién autorizó?", "group", xm["Autorizó"], COLORES_AUT)
    card_pastel(c[1], "Margen mínimo", "south", xm["Mínimo"], COLORES_SUBE_BAJA)
    card_pastel(c[2], "Margen máximo", "north", xm["Máximo"], COLORES_SUBE_BAJA)
    c = st.columns(3)
    card_pastel(c[0], "Inventario", "inventory_2", xm["Inventario (corto)"])
    card_pastel(c[1], "Región", "map", top_n(xm["NOMBRE DE SUCURSAL"].str.title(), 5))
    card_pastel(c[2], "Productos más movidos", "shopping_cart", top_n(xm["DESCRIPCION DEL PRODUCTO"].str.title(), 5))

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
         "costos de la región).</div></div>")
    seccion("Señales")
    sen = x.melt(value_vars=list(ALERTAS), var_name="Alerta", value_name="Sí")
    sen = sen[sen["Sí"]]
    c = st.columns([3, 2])
    if sen.empty:
        st.success("No hay señales para revisar.")
    else:
        card_pastel(c[0], "¿Qué señales aparecen más?", "flag", top_n(sen["Alerta"].map(EXPLICA), 7),
                    centro=f"<b>{len(sen):,}</b><br>señales", alto=460, unidad="señales")
    cont = card("¿Qué significa cada señal?", "help", c[1])
    html("<ul class='lista'>" + "".join(
        f"<li><span class='dot'>{ic(ICONOS[f])}</span><div>{EXPLICA[f]}<small>{t.format(**U)}</small></div></li>"
        for f, t in ALERTAS.items()) + "</ul>", cont)

    seccion("Tiempos y horarios")
    c = st.columns(2)
    sol = x.groupby("Solicitud")["Minutos para autorizar"].first().reset_index(name="Minutos")
    orden_t = ["Menos de 2 min", "Menos de 1 hora", "El mismo día", "1 a 3 días", "Más de 3 días"]
    sol["Tiempo"] = pd.cut(sol["Minutos"], [-1, 2, 60, 1440, 4320, 1e9], labels=orden_t).astype(str)
    card_pastel(c[0], "¿Cuánto se tarda en autorizar?", "schedule", sol["Tiempo"],
                dict(zip(orden_t, [ROJO, "#95de64", VERDE, AMARILLO, NARANJA])),
                f"<b>{len(sol):,}</b><br>solicitudes", alto=380, unidad="solicitudes")
    horario = pd.Series(np.where(x["Fuera de horario"], "Noche o fin de semana", "Horario normal"))
    card_pastel(c[1], f"¿Cuándo se autoriza? (normal: L-V {U['u_hini']} a {U['u_hfin']} h)", "dark_mode",
                horario, {"Horario normal": CIAN, "Noche o fin de semana": "#1d39c4"}, alto=380)

    seccion("Margen máximo: antes y después")
    fig = px.scatter(x, x="SCMARMAXACT", y="SCMARMAXSOL", color="Revisar", color_discrete_map=COLORES_REVISAR,
                     opacity=0.8, category_orders={"Revisar": list(COLORES_REVISAR)},
                     hover_data={"DESCRIPCION DEL PRODUCTO": True, "NOMBRE DE SUCURSAL": True, "Mes": True,
                                 "Alertas": True, "Revisar": False})
    lim = float(max(x["SCMARMAXACT"].max(), x["SCMARMAXSOL"].max()))
    fig.add_trace(go.Scatter(x=[0, lim], y=[0, lim], mode="lines", line=dict(dash="dot", color="#bfbfbf"),
                             name="Sin cambio"))
    fig.update_traces(marker_size=9, selector=dict(mode="markers"))
    fig.update_layout(xaxis_title="Máximo antes", yaxis_title="Máximo después")
    cont = card("Cada punto es un cambio: los que quedan muy arriba de la línea gris son subidas grandes",
                "scatter_plot")
    mostrar(estilo(fig, 460), cont)


def pagina_casos() -> None:
    x = D
    r = x[x["Núm. alertas"] >= 3].sort_values(["Núm. alertas", "Cambio máx."], ascending=False)
    encabezado("Casos a revisar", "Cambios con 3 o más señales: los primeros que conviene revisar", "General")
    seccion("Resumen")
    c = st.columns(3)
    stat(c[0], "Cambios con prioridad", f"{len(r):,}", "error", ROJO, 100 * len(r) / len(x),
         f"{100 * len(r) / len(x):.1f}% de todos los cambios")
    stat(c[1], "Solicitudes", f"{r['Solicitud'].nunique():,}", "description", AMARILLO,
         100 * r["Solicitud"].nunique() / x["Solicitud"].nunique(), "folios con al menos un caso")
    stat(c[2], "Productos distintos", f"{r['CVE. DE PRODUCTO'].nunique():,}", "shopping_cart", MORADO,
         100 * r["CVE. DE PRODUCTO"].nunique() / x["CVE. DE PRODUCTO"].nunique(), "")
    if r.empty:
        return
    st.write("")
    c = st.columns(3)
    card_pastel(c[0], "¿Quién los autorizó?", "group", r["Autorizó"], COLORES_AUT)
    card_pastel(c[1], "¿De qué región son?", "map", top_n(r["NOMBRE DE SUCURSAL"].str.title(), 5))
    card_pastel(c[2], "¿Qué tipo de cambio son?", "swap_vert", r["Tipo de cambio"], COLORES_TIPO)
    seccion("Lista de casos")
    cols = ["Núm. alertas", "Alertas", "Autorizó", "Mes (texto)", "NOMBRE DE SUCURSAL", "NOMBRE DE UNIDAD OP.",
            "SCFOLIO", "DESCRIPCION DEL PRODUCTO", "SCMARMINACT", "SCMARMINSOL", "SCMARMAXACT", "SCMARMAXSOL"]
    card(f"{len(r):,} casos", "fact_check").dataframe(
        r[cols], width=ANCHO, hide_index=True,
        column_config={
            "Núm. alertas": st.column_config.ProgressColumn("Señales", min_value=0, max_value=6, format="%d"),
            "Alertas": "¿Por qué?", "Mes (texto)": "Mes", "NOMBRE DE SUCURSAL": "Región",
            "NOMBRE DE UNIDAD OP.": "Unidad", "SCFOLIO": "Folio", "DESCRIPCION DEL PRODUCTO": "Producto",
            "SCMARMINACT": "Mín. antes", "SCMARMINSOL": "Mín. después",
            "SCMARMAXACT": "Máx. antes", "SCMARMAXSOL": "Máx. después",
        },
    )


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
