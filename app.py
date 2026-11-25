"""
Dashboard de movimientos de márgenes (mínimo / máximo) por autorizador,
con comparativa Daniel vs. Beatriz y alertas para revisar posibles malas prácticas.

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

st.set_page_config(page_title="Movimientos de márgenes 2026", layout="wide")

MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
COLORES_DET = {"DECREMENTO": "#1f77b4", "INCREMENTO": "#d62728", "SIN CAMBIOS": "#bdbdbd"}
COLORES_AUT = {"Daniel Montesinos": "#4c78a8", "Beatriz Cervantes": "#f58518"}
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


# ================================================================ estilo visual
px.defaults.template = "plotly_white"
FUENTE = dict(size=15)
VERDE, AMARILLO, ROJO, GRIS = "#2e7d32", "#f9a825", "#c62828", "#9e9e9e"
COLORES_SUBE_BAJA = {"Sube": "#c62828", "Baja": "#1565c0", "Sin cambio": "#bdbdbd"}
ICONOS = {
    "Cambio extremo en máximo": "📈", "Máximo solicitado muy alto": "⬆️", "Baja fuerte del mínimo": "⬇️",
    "Mínimo en cero o negativo": "0️⃣", "Rango invertido": "🔀", "Ampliación doble": "↔️",
    "Autorización exprés": "⚡", "Autorización tardía": "🐢", "Fuera de horario": "🌙",
    "Cambio repetido": "🔁", "Autorización en lote": "📦", "Auto-aprobación": "🪞",
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


def color_semaforo(pct: float) -> str:
    return VERDE if pct < 10 else AMARILLO if pct < 25 else ROJO


def tarjeta(col, titulo: str, valor: str, nota: str = "", color: str = "#37474f", icono: str = "",
            compacta: bool = False) -> None:
    alto, pad, tam = ("130px", "12px 14px", "1.8rem") if compacta else ("170px", "18px 20px", "2.6rem")
    col.markdown(
        f"""<div style="background:{color};border-radius:16px;padding:{pad};color:#fff;overflow:hidden;
        height:{alto};box-shadow:0 2px 6px rgba(0,0,0,.15)">
        <div style="font-size:1.05rem;opacity:.95">{icono} {titulo}</div>
        <div style="font-size:{tam};font-weight:700;line-height:1.2;margin:6px 0;white-space:nowrap">{valor}</div>
        <div style="font-size:.95rem;opacity:.95">{nota}</div></div>""",
        unsafe_allow_html=True,
    )


def estilo(fig, alto: int = 420, leyenda: bool = True):
    fig.update_layout(font=FUENTE, height=alto, legend_title_text="", showlegend=leyenda,
                      margin=dict(l=10, r=10, t=100 if fig.layout.title.text else 40, b=10),
                      title_font_size=19, title_y=0.98, title_yanchor="top",
                      legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0))
    return fig


def mostrar(fig, col=None) -> None:
    (col or st).plotly_chart(fig, width=ANCHO, config={"displayModeBar": False})


def ver_tabla(df_: pd.DataFrame, titulo: str = "📋 Ver los números en tabla", **kw) -> None:
    with st.expander(titulo):
        st.dataframe(df_, width=ANCHO, hide_index=True, **kw)


def sube_baja(serie: pd.Series) -> pd.Series:
    return serie.map({"INCREMENTO": "Sube", "DECREMENTO": "Baja"}).fillna("Sin cambio")


# ================================================================ app
if not check_password():
    st.stop()

df_all = cargar()

st.markdown(
    "<h1 style='margin-bottom:0'>📊 Cambios de márgenes autorizados</h1>"
    "<p style='font-size:1.15rem;color:#607d8b;margin-top:4px'>Daniel Montesinos vs. Beatriz Cervantes</p>",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("🔎 Filtros")
    auts = sorted(df_all["Autorizó"].unique())
    f_aut = st.multiselect("👤 ¿Quién autorizó?", auts, default=auts)
    meses = sorted(df_all["Mes"].unique())
    if len(meses) > 1:
        f_mes = st.select_slider("📅 Meses", options=meses, value=(meses[0], meses[-1]), format_func=mes_corto)
    else:
        f_mes = (meses[0], meses[0])
    with st.expander("Más filtros"):
        f_reg = st.multiselect("Región", sorted(df_all["NOMBRE DE SUCURSAL"].unique()))
        f_inv = st.multiselect("Inventario", sorted(df_all["DESCRIPCIÓN DEL INVENTARIO"].unique()))
        f_sol = st.multiselect("Solicitó", sorted(df_all["Solicitó"].unique()))
        f_tipo = st.radio("Tipo de movimiento", ["Todos", "Solo incrementos", "Solo decrementos"])
        f_campo = st.radio("Aplicar tipo a", ["Mínimo o máximo", "Solo mínimo", "Solo máximo"])

    with st.expander("⚙️ Umbrales de alertas", expanded=False):
        u = {
            "u_dmax": st.number_input("Subida de máximo ≥ (pts)", 1.0, 500.0, 40.0, 5.0),
            "u_rmax": st.number_input("…o subida de máximo > (% del actual)", 10, 1000, 100, 10),
            "u_maxalto": st.number_input("Máximo solicitado ≥", 10.0, 500.0, 100.0, 10.0),
            "u_dmin": st.number_input("Baja de mínimo ≥ (pts)", 1.0, 100.0, 10.0, 1.0),
            "u_expres": st.number_input("Autorización exprés < (minutos)", 0.5, 60.0, 2.0, 0.5),
            "u_tarde": st.number_input("Autorización tardía > (horas)", 1, 720, 72, 12),
            "u_hini": st.number_input("Horario laboral desde (h)", 0, 23, 8),
            "u_hfin": st.number_input("Horario laboral hasta (h)", 1, 24, 19),
            "u_dias": st.number_input("Cambio repetido en < (días)", 1, 365, 30, 5),
            "u_lote": st.number_input("Lote: solicitudes en el mismo minuto ≥", 2, 50, 5),
        }

df = marcar(df_all, u)
m = df["Autorizó"].isin(f_aut) & df["Mes"].between(*f_mes)
if f_reg:
    m &= df["NOMBRE DE SUCURSAL"].isin(f_reg)
if f_inv:
    m &= df["DESCRIPCIÓN DEL INVENTARIO"].isin(f_inv)
if f_sol:
    m &= df["Solicitó"].isin(f_sol)
if f_tipo != "Todos":
    v = "INCREMENTO" if f_tipo == "Solo incrementos" else "DECREMENTO"
    mm, mx = df["DETERMINACION_MIN"] == v, df["DETERMINACION_MAX"] == v
    m &= {"Mínimo o máximo": mm | mx, "Solo mínimo": mm, "Solo máximo": mx}[f_campo]
d = df[m].copy()

if d.empty:
    st.warning("No hay movimientos con estos filtros.")
    st.stop()

d["Mes (texto)"] = d["Mes"].map(mes_corto)
ORDEN_MESES = [mes_corto(x) for x in sorted(d["Mes"].unique())]
d["Mínimo"] = sube_baja(d["DETERMINACION_MIN"])
d["Máximo"] = sube_baja(d["DETERMINACION_MAX"])
d["Revisar"] = np.select([d["Núm. alertas"] >= 3, d["Núm. alertas"] >= 1],
                         ["🔴 Revisar con prioridad", "🟡 Revisar"], "🟢 Sin señales")

tabs = st.tabs(["🏠 Resumen", "📅 Mes a mes", "⚖️ Daniel vs. Beatriz", "🚩 Qué revisar",
                "🔍 Caso por caso", "📋 Datos"])

# ================================================================ Resumen
with tabs[0]:
    n_mov, n_sol = len(d), d["Solicitud"].nunique()
    con = int((d["Núm. alertas"] > 0).sum())
    prio = int((d["Núm. alertas"] >= 3).sum())
    pct_con = 100 * con / n_mov

    st.markdown(
        f"<p style='font-size:1.3rem'>De <b>{mes_largo(d['Mes'].min())}</b> a <b>{mes_largo(d['Mes'].max())}</b> "
        f"se autorizaron <b>{n_mov:,} cambios de margen</b> en <b>{n_sol:,} solicitudes</b>.</p>",
        unsafe_allow_html=True,
    )
    c = st.columns(4)
    tarjeta(c[0], "Cambios autorizados", f"{n_mov:,}", "productos con margen modificado", "#37474f", "📦")
    tarjeta(c[1], "Solicitudes", f"{n_sol:,}", "folios distintos", "#455a64", "📝")
    tarjeta(c[2], "Con alguna señal", f"{pct_con:.0f}%", f"{con:,} cambios para revisar",
            color_semaforo(pct_con), "🚩")
    tarjeta(c[3], "Prioridad alta", f"{prio:,}", "cambios con 3 o más señales",
            ROJO if prio else VERDE, "🔴")

    st.write("")
    st.subheader("👤 ¿Cómo le fue a cada quien?")
    st.caption("El medidor muestra qué porcentaje de sus autorizaciones tiene alguna señal para revisar. "
               "🟢 menos de 10% · 🟡 10 a 25% · 🔴 más de 25%")
    grupos = list(d.groupby("Autorizó"))
    cols = st.columns(max(1, len(grupos)))
    for col, (a, x) in zip(cols, grupos):
        pct = 100 * (x["Núm. alertas"] > 0).mean()
        fig = go.Figure(go.Indicator(
            mode="gauge+number", value=round(pct, 1), number={"suffix": "%", "font": {"size": 48}},
            title={"font": {"size": 22}, "text": f"<b>{a}</b><br><span style='font-size:15px;color:gray'>"
                           f"{len(x):,} cambios · {mes_corto(x['Mes'].min())} a {mes_corto(x['Mes'].max())}</span>"},
            gauge={"axis": {"range": [0, 100], "ticksuffix": "%"},
                   "bar": {"color": COLORES_AUT.get(a, "#555"), "thickness": 0.3},
                   "steps": [{"range": [0, 10], "color": "#c8e6c9"}, {"range": [10, 25], "color": "#fff3c4"},
                             {"range": [25, 100], "color": "#ffcdd2"}]},
        ))
        fig.update_layout(height=300, margin=dict(l=30, r=30, t=90, b=10), font=FUENTE)
        mostrar(fig, col)

    c1, c2 = st.columns([3, 2])
    with c1:
        pm = d.groupby(["Mes", "Mes (texto)", "Autorizó"]).size().reset_index(name="Cambios").sort_values("Mes")
        fig = px.bar(pm, x="Mes (texto)", y="Cambios", color="Autorizó", text="Cambios",
                     color_discrete_map=COLORES_AUT, title="📅 Cambios autorizados por mes",
                     category_orders={"Mes (texto)": ORDEN_MESES})
        fig.update_xaxes(title="")
        fig.update_yaxes(title="", showticklabels=False, showgrid=False)
        mostrar(estilo(fig, 420))
    with c2:
        rv = d["Revisar"].value_counts().reindex(["🟢 Sin señales", "🟡 Revisar", "🔴 Revisar con prioridad"]).fillna(0)
        fig = go.Figure(go.Pie(labels=rv.index, values=rv.values, hole=0.55, sort=False,
                               marker_colors=[VERDE, AMARILLO, ROJO], textinfo="percent",
                               textfont_size=16))
        fig.update_layout(title="🚦 Semáforo de todos los cambios",
                          annotations=[dict(text=f"<b>{n_mov:,}</b><br>cambios", showarrow=False, font_size=18)])
        estilo(fig, 420).update_layout(legend=dict(orientation="h", yanchor="top", y=-0.02, x=0.5,
                                                   xanchor="center"))
        mostrar(fig)

    st.subheader("↕️ ¿Los márgenes suben o bajan?")
    c1, c2 = st.columns(2)
    for col, campo, titulo in [(c1, "Mínimo", "Margen mínimo"), (c2, "Máximo", "Margen máximo")]:
        x = d.groupby(["Autorizó", campo]).size().reset_index(name="Cambios")
        x["%"] = (100 * x["Cambios"] / x.groupby("Autorizó")["Cambios"].transform("sum")).round(0)
        etiqueta = np.where(x["%"] >= 8, x["%"].astype(int).astype(str) + "%", "")
        fig = px.bar(x, y="Autorizó", x="%", color=campo, orientation="h", text=etiqueta,
                     color_discrete_map=COLORES_SUBE_BAJA, title=titulo,
                     category_orders={campo: ["Sube", "Sin cambio", "Baja"]}, hover_data=["Cambios"])
        fig.update_xaxes(visible=False, range=[0, 100])
        fig.update_yaxes(title="")
        fig.update_traces(textfont_size=16, insidetextanchor="middle", textposition="inside")
        mostrar(estilo(fig, 260), col)
    st.caption("🔴 Sube = el producto se puede vender más caro · 🔵 Baja = se puede vender más barato")

    st.subheader("🚩 Lo que más se repite entre las señales")
    cuenta = pd.DataFrame({"Señal": [f"{ICONOS[f]} {EXPLICA[f]}" for f in ALERTAS],
                           "Cambios": [int(d[f].sum()) for f in ALERTAS]})
    cuenta = cuenta[cuenta["Cambios"] > 0].sort_values("Cambios").tail(6)
    if cuenta.empty:
        st.success("✅ No hay señales para revisar con estos filtros.")
    else:
        fig = px.bar(cuenta, x="Cambios", y="Señal", orientation="h", text="Cambios")
        fig.update_traces(marker_color=ROJO, textfont_size=16, textposition="outside")
        fig.update_xaxes(visible=False)
        fig.update_yaxes(title="")
        mostrar(estilo(fig, 60 + 55 * len(cuenta), leyenda=False))

    st.subheader("🗺️ ¿Dónde se hacen más cambios?")
    st.caption("Cada cuadro es una región y, adentro, sus unidades operativas. Más grande = más cambios. "
               "Más rojo = más señales para revisar.")
    tm = (d.groupby(["NOMBRE DE SUCURSAL", "NOMBRE DE UNIDAD OP."])
          .agg(Cambios=("Solicitud", "size"), Señales=("Núm. alertas", lambda s: round(100 * (s > 0).mean(), 0)))
          .reset_index())
    fig = px.treemap(tm, path=[px.Constant("Todas"), "NOMBRE DE SUCURSAL", "NOMBRE DE UNIDAD OP."],
                     values="Cambios", color="Señales", range_color=[0, 50],
                     color_continuous_scale=[[0, "#c8e6c9"], [0.5, "#fff3c4"], [1, "#e57373"]])
    fig.update_traces(texttemplate="<b>%{label}</b><br>%{value} cambios", textfont_size=15)
    fig.update_layout(coloraxis_colorbar=dict(title="% con señal", ticksuffix="%"))
    mostrar(estilo(fig, 520))

# ================================================================ Mes a mes
with tabs[1]:
    pm = d.groupby(["Mes", "Mes (texto)", "Autorizó"]).size().reset_index(name="Cambios").sort_values("Mes")
    fig = px.line(pm, x="Mes (texto)", y="Cambios", color="Autorizó", markers=True, text="Cambios",
                  color_discrete_map=COLORES_AUT, title="📈 Tendencia de cambios autorizados",
                  category_orders={"Mes (texto)": ORDEN_MESES})
    fig.update_traces(textposition="top center", line_width=4, marker_size=12)
    fig.update_xaxes(title="")
    mostrar(estilo(fig, 420))

    c1, c2 = st.columns(2)
    for col, campo, titulo in [(c1, "Mínimo", "Margen mínimo: ¿sube o baja?"),
                               (c2, "Máximo", "Margen máximo: ¿sube o baja?")]:
        x = d.groupby(["Mes", "Mes (texto)", campo]).size().reset_index(name="Cambios").sort_values("Mes")
        fig = px.bar(x, x="Mes (texto)", y="Cambios", color=campo, color_discrete_map=COLORES_SUBE_BAJA,
                     title=titulo,
                     category_orders={"Mes (texto)": ORDEN_MESES, campo: ["Sube", "Sin cambio", "Baja"]})
        fig.update_xaxes(title="")
        mostrar(estilo(fig, 420), col)

    x = d.groupby(["Mes", "Mes (texto)", "Revisar"]).size().reset_index(name="Cambios").sort_values("Mes")
    fig = px.bar(x, x="Mes (texto)", y="Cambios", color="Revisar", text="Cambios", title="🚦 Semáforo por mes",
                 color_discrete_map={"🟢 Sin señales": VERDE, "🟡 Revisar": AMARILLO, "🔴 Revisar con prioridad": ROJO},
                 category_orders={"Mes (texto)": ORDEN_MESES,
                                  "Revisar": ["🔴 Revisar con prioridad", "🟡 Revisar", "🟢 Sin señales"]})
    fig.update_xaxes(title="")
    mostrar(estilo(fig, 420))
    ver_tabla(por_mes(d))

# ================================================================ Comparativo
with tabs[2]:
    st.info(
        "⚠️ Los dos **no autorizaron en los mismos meses** "
        f"({'; '.join(f'{a}: {mes_corto(x.Mes.min())} a {mes_corto(x.Mes.max())}' for a, x in d.groupby('Autorizó'))}). "
        "Por eso aquí se comparan **porcentajes**, no totales."
    )
    grupos = list(d.groupby("Autorizó"))
    cols = st.columns(max(1, len(grupos)))
    for col, (a, x) in zip(cols, grupos):
        with col:
            st.markdown(f"<h3 style='color:{COLORES_AUT.get(a, '#333')}'>👤 {a}</h3>", unsafe_allow_html=True)
            k = st.columns(3)
            colr = COLORES_AUT.get(a, "#455a64")
            med = x["Minutos para autorizar"].median()
            tarjeta(k[0], "Cambios", f"{len(x):,}", "", colr, "📦", compacta=True)
            tarjeta(k[1], "Folios", f"{x['Solicitud'].nunique():,}", "", colr, "📝", compacta=True)
            tarjeta(k[2], "Tarda", f"{med:.0f} min" if med < 120 else f"{med / 60:.0f} h",
                    "lo típico", colr, "⏳", compacta=True)
    st.write("")

    pt = pct_tipo(d)
    pt = pt[~pt["Tipo"].str.contains("sin cambios")]
    pt["Tipo"] = (pt["Tipo"].str.replace("Mín.", "Mínimo", regex=False).str.replace("Máx.", "Máximo", regex=False)
                  .str.replace("incremento", "sube", regex=False).str.replace("decremento", "baja", regex=False))
    fig = px.bar(pt, x="Tipo", y="%", color="Autorizó", barmode="group", text=pt["%"].round(0).astype(int).astype(str) + "%",
                 color_discrete_map=COLORES_AUT, title="De cada 100 cambios que autoriza, ¿cuántos son de cada tipo?")
    fig.update_xaxes(title="")
    fig.update_yaxes(visible=False)
    fig.update_traces(textposition="outside", textfont_size=15)
    mostrar(estilo(fig, 430))

    c1, c2 = st.columns(2)
    for col, cambio, titulo in [(c1, "Cambio mín.", "¿Qué tanto movieron el mínimo? (puntos)"),
                                (c2, "Cambio máx.", "¿Qué tanto movieron el máximo? (puntos)")]:
        x = d[d[cambio] != 0]
        prom = x.groupby("Autorizó")[cambio].agg(lambda s: s.abs().mean()).round(1).reset_index()
        fig = px.bar(prom, x="Autorizó", y=cambio, color="Autorizó", text=cambio,
                     color_discrete_map=COLORES_AUT, title=titulo)
        fig.update_yaxes(visible=False)
        fig.update_xaxes(title="")
        fig.update_traces(textposition="outside", textfont_size=18, texttemplate="%{text} pts")
        mostrar(estilo(fig, 360, leyenda=False), col)
    st.caption("Promedio de cuántos puntos se movió el margen, sin importar si subió o bajó.")

    st.subheader("🙋 ¿Quién le pide cambios a cada uno?")
    conc = d.groupby(["Autorizó", "Solicitó"]).size().reset_index(name="Cambios")
    conc["%"] = (100 * conc["Cambios"] / conc.groupby("Autorizó")["Cambios"].transform("sum")).round(0)
    conc = conc.sort_values(["Autorizó", "Cambios"], ascending=[True, False]).groupby("Autorizó").head(6)
    fig = px.bar(conc, x="%", y="Solicitó", color="Autorizó", facet_col="Autorizó", orientation="h",
                 color_discrete_map=COLORES_AUT, text=conc["%"].astype(int).astype(str) + "%",
                 facet_col_spacing=0.3)
    fig.update_traces(textposition="outside", cliponaxis=False)
    fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
    fig.update_yaxes(matches=None, showticklabels=True, title="", categoryorder="total ascending")
    fig.update_xaxes(visible=False)
    mostrar(estilo(fig, 420, leyenda=False))

    with st.expander("📋 Ver los números en tabla"):
        st.dataframe(resumen(d), width=ANCHO, hide_index=True)
        st.dataframe(magnitudes(d), width=ANCHO, hide_index=True)
        comb = pd.crosstab(d["Combinación"], d["Autorizó"], normalize="columns").mul(100).round(1)
        comb.columns = [f"{c} (%)" for c in comb.columns]
        st.dataframe(comb, width=ANCHO)

# ================================================================ Qué revisar
with tabs[3]:
    st.warning(
        "Estas señales **no prueban una mala práctica**: indican cambios que vale la pena preguntar. "
        "Muchos tienen una explicación válida (temporada, ofertas, costos de la región)."
    )
    ta = tabla_alertas(d)
    tasas = ta.melt(id_vars="Alerta", value_vars=[c for c in ta.columns if c.endswith("por 100 mov.")],
                    var_name="Autorizó", value_name="De cada 100 cambios")
    tasas["Autorizó"] = tasas["Autorizó"].str.replace(" · por 100 mov.", "", regex=False)
    tasas["Señal"] = tasas["Alerta"].map(lambda f: f"{ICONOS[f]} {EXPLICA[f]}")
    tasas = tasas[tasas.groupby("Señal")["De cada 100 cambios"].transform("sum") > 0]
    if tasas.empty:
        st.success("✅ No hay señales para revisar con estos filtros.")
    else:
        fig = px.bar(tasas, y="Señal", x="De cada 100 cambios", color="Autorizó", barmode="group",
                     orientation="h", color_discrete_map=COLORES_AUT, text="De cada 100 cambios",
                     title="De cada 100 cambios que autoriza cada uno, ¿cuántos tienen esta señal?")
        fig.update_yaxes(title="", categoryorder="total ascending")
        fig.update_xaxes(visible=False)
        fig.update_traces(textposition="outside")
        mostrar(estilo(fig, 120 + 70 * tasas["Señal"].nunique()))

    c1, c2 = st.columns(2)
    with c1:
        dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
        hm = (d.assign(Día=d["FCHAUT"].dt.weekday.map(dict(enumerate(dias))))
              .groupby(["Día", "Hora autorización"]).size().reset_index(name="Cambios"))
        piv = hm.pivot(index="Día", columns="Hora autorización", values="Cambios").reindex(dias).fillna(0)
        piv = piv.reindex(columns=range(24), fill_value=0)
        fig = px.imshow(piv, color_continuous_scale="Reds", aspect="auto",
                        labels=dict(x="Hora del día", y="", color="Cambios"),
                        title="🕒 ¿A qué hora y qué día se autoriza?")
        fig.update_xaxes(dtick=2, ticksuffix="h")
        mostrar(estilo(fig, 380, leyenda=False), c1)
        st.caption(f"Lo normal es de lunes a viernes entre {u['u_hini']} h y {u['u_hfin']} h. "
                   "Las manchas fuera de ese horario son las que conviene revisar.")
    with c2:
        sol = (d.groupby(["Autorizó", "Solicitud"])["Minutos para autorizar"].first().reset_index(name="Minutos"))
        sol["Tiempo"] = pd.cut(sol["Minutos"], [-1, 2, 60, 1440, 4320, 1e9],
                               labels=["⚡ Menos de 2 min", "⏱️ Menos de 1 hora", "📆 El mismo día",
                                       "🗓️ 1 a 3 días", "🐢 Más de 3 días"])
        sr = sol.groupby(["Autorizó", "Tiempo"], observed=False).size().reset_index(name="Solicitudes")
        sr["%"] = (100 * sr["Solicitudes"] / sr.groupby("Autorizó")["Solicitudes"].transform("sum")).round(0)
        fig = px.bar(sr, x="Tiempo", y="%", color="Autorizó", barmode="group", color_discrete_map=COLORES_AUT,
                     text=sr["%"].astype(int).astype(str) + "%", title="⏳ ¿Cuánto tardan en autorizar?")
        fig.update_traces(textposition="outside", textangle=0)
        fig.update_xaxes(title="", tickangle=0)
        fig.update_yaxes(visible=False)
        mostrar(estilo(fig, 380), c2)

    st.subheader("📈 Margen máximo: antes y después")
    st.caption("Cada punto es un cambio. Los que quedan **muy arriba de la línea gris** son subidas grandes.")
    x = d.copy()
    x["Señales"] = np.where(x["Núm. alertas"] > 0, "Con señal", "Sin señal")
    fig = px.scatter(x, x="SCMARMAXACT", y="SCMARMAXSOL", color="Autorizó", symbol="Señales",
                     color_discrete_map=COLORES_AUT, opacity=0.7,
                     hover_data={"DESCRIPCION DEL PRODUCTO": True, "NOMBRE DE SUCURSAL": True, "Mes": True,
                                 "Alertas": True})
    lim = float(max(x["SCMARMAXACT"].max(), x["SCMARMAXSOL"].max()))
    fig.add_trace(go.Scatter(x=[0, lim], y=[0, lim], mode="lines", line=dict(dash="dot", color="gray"),
                             name="Sin cambio"))
    fig.update_layout(xaxis_title="Máximo antes", yaxis_title="Máximo después")
    mostrar(estilo(fig, 480))

    with st.expander("📋 Ver los números en tabla"):
        for f, txt in ALERTAS.items():
            st.markdown(f"- {ICONOS[f]} **{f}:** {txt.format(**u)}")
        st.dataframe(ta, width=ANCHO, hide_index=True)
        rep = (d.groupby(["Clave producto-UO", "DESCRIPCION DEL PRODUCTO", "NOMBRE DE UNIDAD OP.",
                          "DESCRIPCIÓN DEL INVENTARIO"])
               .agg(Cambios=("Solicitud", "size"), Autorizó=("Autorizó", lambda s: ", ".join(sorted(set(s)))),
                    Meses=("Mes", lambda s: ", ".join(sorted(set(s)))))
               .reset_index())
        rep = rep[rep["Cambios"] >= 2].sort_values("Cambios", ascending=False).drop(columns="Clave producto-UO")
        st.markdown("**Productos que se cambiaron más de una vez en el mismo lugar**")
        st.dataframe(rep, width=ANCHO, hide_index=True)

# ================================================================ Caso por caso
with tabs[4]:
    st.caption("Los cambios con más señales aparecen primero. Elige una señal para ver solo esos casos.")
    opciones = {f"{ICONOS[f]} {EXPLICA[f]}": f for f in ALERTAS}
    sel = st.multiselect("Ver solo cambios con:", list(opciones), default=[])
    minimo = st.slider("Número mínimo de señales", 1, len(ALERTAS), 1)
    r = d[d["Núm. alertas"] >= minimo]
    if sel:
        r = r[r[[opciones[s] for s in sel]].any(axis=1)]
    st.markdown(f"### {len(r):,} cambios en {r['Solicitud'].nunique():,} solicitudes")
    cols = ["Revisar", "Núm. alertas", "Alertas", "Autorizó", "Solicitó", "FCHSOL", "FCHAUT",
            "NOMBRE DE SUCURSAL", "NOMBRE DE UNIDAD OP.", "SCFOLIO", "DESCRIPCION DEL PRODUCTO",
            "SCMARMINACT", "SCMARMINSOL", "SCMARMAXACT", "SCMARMAXSOL"]
    st.dataframe(
        r.sort_values(["Núm. alertas", "Cambio máx."], ascending=False)[cols],
        width=ANCHO, hide_index=True,
        column_config={
            "Revisar": "Semáforo", "Núm. alertas": st.column_config.ProgressColumn(
                "Señales", min_value=0, max_value=6, format="%d"),
            "Alertas": "¿Por qué?", "FCHSOL": "Pedido el", "FCHAUT": "Autorizado el",
            "NOMBRE DE SUCURSAL": "Región", "NOMBRE DE UNIDAD OP.": "Unidad", "SCFOLIO": "Folio",
            "DESCRIPCION DEL PRODUCTO": "Producto", "SCMARMINACT": "Mín. antes", "SCMARMINSOL": "Mín. después",
            "SCMARMAXACT": "Máx. antes", "SCMARMAXSOL": "Máx. después",
        },
    )

# ================================================================ Datos
with tabs[5]:
    st.dataframe(d[[c for c in DETALLE_COLS if c in d.columns]], width=ANCHO, hide_index=True)

st.download_button(
    "⬇️ Descargar Excel con la información filtrada",
    data=a_excel(d),
    file_name="movimientos_filtrados.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
