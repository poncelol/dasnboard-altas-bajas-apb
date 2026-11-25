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
from plotly.subplots import make_subplots
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


COLORES_REVISAR = {"🟢 Sin señales": VERDE, "🟡 Revisar": AMARILLO, "🔴 Revisar con prioridad": ROJO}
COLORES_TIPO = {
    "⬇️ Bajó el mínimo": "#1565c0",
    "⬆️ Subió el máximo": "#c62828",
    "↔️ Bajó mín. y subió máx.": "#8e24aa",
    "🔸 Otros cambios": "#bdbdbd",
}
PALETA = ["#4c78a8", "#f58518", "#54a24b", "#e45756", "#72b7b2", "#eeca3b", "#b279a2", "#9d755d", "#bab0ac"]


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


def pastel(serie: pd.Series, colores: dict | None, titulo: str, centro: str = "",
           alto: int = 340, leyenda: bool = True, texto: str = "percent"):
    """Un solo pastel (dona) con los conteos de la serie."""
    vc = serie.value_counts()
    if colores:
        vc = vc.reindex([k for k in colores if k in vc.index] + [k for k in vc.index if k not in colores])
        cols = [colores.get(k, GRIS) for k in vc.index]
    else:
        if "Otros" in vc.index:
            vc = pd.concat([vc.drop("Otros"), vc[["Otros"]]])
        cols = [(GRIS if k == "Otros" else PALETA[i % len(PALETA)]) for i, k in enumerate(vc.index)]
    fig = go.Figure(go.Pie(labels=vc.index, values=vc.values, hole=0.5, sort=False, marker_colors=cols,
                           textinfo=texto, textfont_size=15, insidetextorientation="horizontal",
                           textposition="inside",
                           hovertemplate="<b>%{label}</b><br>%{value:,} cambios (%{percent})<extra></extra>"))
    fig.update_layout(title=dict(text=titulo, x=0.5, xanchor="center", font_size=18), height=alto,
                      font=FUENTE, showlegend=leyenda, margin=dict(l=10, r=10, t=50, b=10),
                      uniformtext_minsize=11, uniformtext_mode="hide",
                      legend=dict(orientation="h", yanchor="top", y=-0.02, x=0.5, xanchor="center"))
    if centro:
        fig.add_annotation(text=centro, showarrow=False, font_size=16)
    return fig


def pastel_por_persona(data: pd.DataFrame, campo: str, titulo: str, colores: dict | None = None,
                       orden: list | None = None, top: int | None = None, alto: int = 400,
                       centro: str = "cambios", top_por_persona: bool = False):
    """Un pastel (dona) por autorizador, lado a lado, con los mismos colores para cada categoría."""
    grupos = list(data.groupby("Autorizó"))
    n = max(1, len(grupos))
    fig = make_subplots(rows=1, cols=n, specs=[[{"type": "domain"}] * n],
                        subplot_titles=[f"<b>{a}</b>" for a, _ in grupos])
    if top and top_por_persona:  # las principales de cada quien
        data = data.copy()
        principales = data.groupby("Autorizó")[campo].transform(
            lambda s: s.isin(s.value_counts().head(top).index))
        data[campo] = data[campo].where(principales, "Otros")
        grupos = list(data.groupby("Autorizó"))
    elif top:  # mismas categorías "principales" para todos, el resto va a "Otros"
        principales = data[campo].value_counts().head(top).index
        data = data.assign(**{campo: data[campo].where(data[campo].isin(principales), "Otros")})
        grupos = list(data.groupby("Autorizó"))
    for i, (a, x) in enumerate(grupos, 1):
        vc = x[campo].value_counts()
        if orden:
            vc = vc.reindex([o for o in orden if o in vc.index] + [o for o in vc.index if o not in orden])
        elif "Otros" in vc.index:
            vc = pd.concat([vc.drop("Otros"), vc[["Otros"]]])
        fig.add_trace(go.Pie(
            labels=vc.index, values=vc.values, hole=0.5, sort=False, name=a,
            marker=dict(colors=[colores.get(lbl, GRIS) for lbl in vc.index]) if colores else None,
            textinfo="percent", textfont_size=15, insidetextorientation="horizontal", textposition="inside",
            hovertemplate="<b>%{label}</b><br>%{value:,} " + centro + "<br>%{percent}<extra>" + a + "</extra>",
            title=dict(text=f"<b>{int(vc.sum()):,}</b><br>{centro}", font=dict(size=15)),
        ), 1, i)
    fig.update_annotations(font_size=17)
    fig.update_layout(title=titulo, font=FUENTE, height=alto, title_font_size=19,
                      uniformtext_minsize=12, uniformtext_mode="hide",
                      margin=dict(l=10, r=10, t=100, b=10), legend_title_text="",
                      legend=dict(orientation="h", yanchor="top", y=-0.02, x=0.5, xanchor="center"))
    return fig


def _fmt_tiempo(minutos: float) -> str:
    if pd.isna(minutos):
        return "—"
    if minutos < 120:
        return f"{minutos:.0f} min"
    if minutos < 48 * 60:
        return f"{minutos / 60:.0f} h"
    return f"{minutos / 1440:.0f} días"


def versus(data: pd.DataFrame, completo: bool = True) -> None:
    """Panel 'cara a cara': cada renglón muestra el dato de cada persona con una barra proporcional."""
    grupos = list(data.groupby("Autorizó"))
    if len(grupos) != 2:
        st.info("Selecciona a las dos personas en los filtros para ver la comparación cara a cara.")
        return
    pct = lambda s: 100 * s.mean()  # noqa: E731
    filas = [
        ("📦 Cambios autorizados", lambda x: len(x), lambda v: f"{v:,.0f}"),
        ("📝 Solicitudes (folios)", lambda x: x["Solicitud"].nunique(), lambda v: f"{v:,.0f}"),
        ("📅 Meses autorizando", lambda x: x["Mes"].nunique(), lambda v: f"{v:.0f}"),
        ("🚩 Cambios con alguna señal", lambda x: pct(x["Núm. alertas"] > 0), lambda v: f"{v:.0f}%"),
        ("🔴 Cambios con 3+ señales", lambda x: pct(x["Núm. alertas"] >= 3), lambda v: f"{v:.0f}%"),
        ("⬇️ Bajó el margen mínimo", lambda x: pct(x["DETERMINACION_MIN"] == "DECREMENTO"), lambda v: f"{v:.0f}%"),
        ("⬆️ Subió el margen máximo", lambda x: pct(x["DETERMINACION_MAX"] == "INCREMENTO"), lambda v: f"{v:.0f}%"),
        ("📏 Cuánto movió el mínimo (prom.)",
         lambda x: x.loc[x["Cambio mín."] != 0, "Cambio mín."].abs().mean(), lambda v: f"{v:.1f} pts"),
        ("📏 Cuánto movió el máximo (prom.)",
         lambda x: x.loc[x["Cambio máx."] != 0, "Cambio máx."].abs().mean(), lambda v: f"{v:.1f} pts"),
        ("⏳ Tiempo típico para autorizar", lambda x: x["Minutos para autorizar"].median(), _fmt_tiempo),
    ]
    if completo:
        filas += [(f"{ICONOS[f]} {EXPLICA[f]}", (lambda f: lambda x: pct(x[f]))(f), lambda v: f"{v:.0f}%")
                  for f in ["Autorización exprés", "Fuera de horario", "Cambio repetido",
                            "Autorización en lote", "Ampliación doble", "Auto-aprobación"]]
    (a1, x1), (a2, x2) = grupos
    c1, c2 = COLORES_AUT.get(a1, "#555"), COLORES_AUT.get(a2, "#555")
    html = [f"""<div style="display:grid;grid-template-columns:1fr minmax(170px,240px) 1fr;gap:6px 14px;
        align-items:center;font-family:inherit">
        <div style="text-align:right;font-size:1.5rem;font-weight:700;color:{c1}">👤 {a1}</div><div></div>
        <div style="font-size:1.5rem;font-weight:700;color:{c2}">👤 {a2}</div>"""]
    for etiqueta, fn, fmt in filas:
        v1, v2 = fn(x1), fn(x2)
        v1, v2 = (0 if pd.isna(v1) else v1), (0 if pd.isna(v2) else v2)
        mx = max(v1, v2) or 1
        w1, w2 = 100 * v1 / mx, 100 * v2 / mx
        html.append(f"""
        <div style="display:flex;align-items:center;justify-content:flex-end;gap:10px">
          <span style="font-size:1.35rem;font-weight:700;color:{c1};white-space:nowrap">{fmt(v1)}</span>
          <div style="width:65%;display:flex;justify-content:flex-end">
            <div style="height:20px;width:{w1:.0f}%;background:{c1};border-radius:10px 0 0 10px"></div></div>
        </div>
        <div style="text-align:center;font-size:1rem;padding:8px 4px;border-bottom:1px solid rgba(128,128,128,.25)">
          {etiqueta}</div>
        <div style="display:flex;align-items:center;gap:10px">
          <div style="width:65%"><div style="height:20px;width:{w2:.0f}%;background:{c2};
            border-radius:0 10px 10px 0"></div></div>
          <span style="font-size:1.35rem;font-weight:700;color:{c2};white-space:nowrap">{fmt(v2)}</span>
        </div>""")
    html.append("</div>")
    st.markdown(re.sub(r"\n\s*", " ", "".join(html)), unsafe_allow_html=True)


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
d["Tipo de cambio"] = tipo_de_cambio(d)
d["Inventario (corto)"] = d["DESCRIPCIÓN DEL INVENTARIO"].map(inventario_corto)
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
    c1, c2 = st.columns(2)
    with c1:
        quien = d["Autorizó"].value_counts()
        fig = go.Figure(go.Pie(labels=quien.index, values=quien.values, hole=0.5, sort=False,
                               marker_colors=[COLORES_AUT.get(a, GRIS) for a in quien.index],
                               textinfo="percent+value", textfont_size=16,
                               title=dict(text=f"<b>{n_mov:,}</b><br>cambios", font=dict(size=16))))
        fig.update_layout(title="👤 ¿Quién autorizó más cambios?")
        estilo(fig, 400).update_layout(legend=dict(orientation="h", yanchor="top", y=-0.02, x=0.5,
                                                   xanchor="center"))
        mostrar(fig)
    with c2:
        mostrar(pastel_por_persona(d, "Revisar", "🚦 Semáforo de cada quien", COLORES_REVISAR,
                                   orden=list(COLORES_REVISAR), alto=400))
    st.caption("🟢 sin señales · 🟡 1 o 2 señales para revisar · 🔴 3 o más señales (revisar primero)")

    st.subheader("⚖️ Cara a cara")
    versus(d, completo=False)
    st.write("")

    st.subheader("↕️ ¿Los márgenes suben o bajan?")
    st.caption("🔴 Sube = el producto se puede vender más caro · 🔵 Baja = se puede vender más barato · "
               "⚪ Sin cambio")
    c1, c2 = st.columns(2)
    orden_sb = ["Sube", "Baja", "Sin cambio"]
    mostrar(pastel_por_persona(d, "Mínimo", "Margen mínimo", COLORES_SUBE_BAJA, orden_sb, alto=380), c1)
    mostrar(pastel_por_persona(d, "Máximo", "Margen máximo", COLORES_SUBE_BAJA, orden_sb, alto=380), c2)

    st.subheader("🚩 ¿Qué tipo de señales tiene cada quien?")
    sen = d[d["Núm. alertas"] > 0].melt(id_vars=["Autorizó"], value_vars=list(ALERTAS),
                                        var_name="Alerta", value_name="Sí")
    sen = sen[sen["Sí"]]
    if sen.empty:
        st.success("✅ No hay señales para revisar con estos filtros.")
    else:
        sen["Señal"] = sen["Alerta"].map(lambda f: f"{ICONOS[f]} {EXPLICA[f]}")
        mostrar(pastel_por_persona(sen, "Señal", "Reparto de las señales encontradas", top=6, alto=480,
                                   centro="señales"))

    pm = d.groupby(["Mes", "Mes (texto)", "Autorizó"]).size().reset_index(name="Cambios").sort_values("Mes")
    fig = px.bar(pm, x="Mes (texto)", y="Cambios", color="Autorizó", text="Cambios",
                 color_discrete_map=COLORES_AUT, title="📅 Cambios autorizados por mes",
                 category_orders={"Mes (texto)": ORDEN_MESES})
    fig.update_xaxes(title="")
    fig.update_yaxes(title="", showticklabels=False, showgrid=False)
    mostrar(estilo(fig, 400))

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
    st.markdown("<p style='font-size:1.15rem'>Cada pastel es un mes. Los colores muestran <b>qué tipo de "
                "cambio</b> se hizo más.</p>", unsafe_allow_html=True)
    leyenda = " &nbsp; ".join(f"<span style='color:{c};font-size:1.4rem'>●</span> {t}"
                              for t, c in COLORES_TIPO.items())
    st.markdown(f"<p style='font-size:1rem'>{leyenda}</p>", unsafe_allow_html=True)

    # ---- mosaico: un pastel por mes
    meses_d = sorted(d["Mes"].unique())
    por_fila = 4
    for i in range(0, len(meses_d), por_fila):
        cols = st.columns(por_fila)
        for col, mes in zip(cols, meses_d[i:i + por_fila]):
            x = d[d["Mes"] == mes]
            fig = pastel(x["Tipo de cambio"], COLORES_TIPO, f"<b>{mes_largo(mes)}</b>",
                         f"<b>{len(x):,}</b><br>cambios", alto=260, leyenda=False, texto="percent")
            mostrar(fig, col)
            dom = x["Tipo de cambio"].value_counts()
            col.markdown(f"<p style='text-align:center;margin-top:-18px;font-size:.95rem'>"
                         f"Lo más común:<br><b>{dom.index[0]}</b> ({100 * dom.iloc[0] / len(x):.0f}%)</p>",
                         unsafe_allow_html=True)

    st.divider()

    # ---- detalle de un mes
    st.subheader("🔎 Ver un mes a detalle")
    mes_sel = st.selectbox("Elige el mes", meses_d[::-1], format_func=mes_largo, key="mes_sel")
    x = d[d["Mes"] == mes_sel]
    idx = meses_d.index(mes_sel)
    prev = d[d["Mes"] == meses_d[idx - 1]] if idx > 0 else None

    # resumen escrito automático
    tipo = x["Tipo de cambio"].value_counts()
    inv = x["Inventario (corto)"].value_counts()
    reg = x["NOMBRE DE SUCURSAL"].value_counts()
    prod = x["DESCRIPCION DEL PRODUCTO"].value_counts()
    pct_sen = 100 * (x["Núm. alertas"] > 0).mean()
    señales = pd.Series({f: int(x[f].sum()) for f in ALERTAS}).sort_values(ascending=False)
    quien = x["Autorizó"].value_counts()
    puntos = [
        f"Se autorizaron <b>{len(x):,} cambios</b> en <b>{x['Solicitud'].nunique():,} solicitudes</b>"
        + (f" ({'▲' if len(x) > len(prev) else '▼'} {abs(len(x) - len(prev)):,} vs. "
           f"{mes_largo(meses_d[idx - 1]).split()[0].lower()})" if prev is not None else "") + ".",
        f"Autorizó: <b>{', '.join(f'{a} ({n:,})' for a, n in quien.items())}</b>.",
        f"Lo que más se hizo: <b>{tipo.index[0]}</b> ({100 * tipo.iloc[0] / len(x):.0f}% de los cambios).",
        f"Inventario principal: <b>{inv.index[0]}</b> ({100 * inv.iloc[0] / len(x):.0f}%).",
        f"Región con más cambios: <b>{reg.index[0].title()}</b> ({reg.iloc[0]:,}).",
        f"Productos más movidos: <b>{', '.join(p.title() for p in prod.index[:3])}</b>.",
        f"<b>{pct_sen:.0f}%</b> de los cambios tienen alguna señal"
        + (f"; la más común: <b>{ICONOS[señales.index[0]]} {EXPLICA[señales.index[0]].lower()}</b> "
           f"({señales.iloc[0]:,})." if señales.iloc[0] > 0 else "."),
    ]
    color_caja = color_semaforo(pct_sen)
    st.markdown(
        f"<div style='border-left:8px solid {color_caja};background:rgba(127,127,127,.08);border-radius:10px;"
        f"padding:14px 20px;font-size:1.1rem;line-height:1.8'>"
        f"<b style='font-size:1.3rem'>📌 {mes_largo(mes_sel)} en resumen</b><br>"
        + "<br>".join(f"• {p}" for p in puntos) + "</div>",
        unsafe_allow_html=True,
    )
    st.write("")

    c = st.columns(3)
    mostrar(pastel(x["Tipo de cambio"], COLORES_TIPO, "🔄 Tipo de cambio"), c[0])
    mostrar(pastel(x["Autorizó"], COLORES_AUT, "👤 ¿Quién autorizó?"), c[1])
    mostrar(pastel(x["Revisar"], COLORES_REVISAR, "🚦 Semáforo"), c[2])
    c = st.columns(3)
    mostrar(pastel(x["Mínimo"], COLORES_SUBE_BAJA, "⬇️ Margen mínimo"), c[0])
    mostrar(pastel(x["Máximo"], COLORES_SUBE_BAJA, "⬆️ Margen máximo"), c[1])
    mostrar(pastel(x["Inventario (corto)"], None, "📦 Inventario"), c[2])
    c = st.columns(2)
    mostrar(pastel(top_n(x["NOMBRE DE SUCURSAL"].str.title(), 6), None, "🗺️ Región"), c[0])
    mostrar(pastel(top_n(x["DESCRIPCION DEL PRODUCTO"].str.title(), 6), None, "🛒 Productos más movidos"), c[1])

    ver_tabla(x[[c for c in DETALLE_COLS if c in x.columns]].rename(columns=DETALLE_COLS),
              f"📋 Ver los {len(x):,} cambios de {mes_largo(mes_sel)}")

    st.divider()
    st.subheader("📅 Todos los meses en una tabla")
    tabla_mes = (d.groupby("Mes")
                 .agg(**{"Cambios": ("Solicitud", "size"), "Solicitudes": ("Solicitud", "nunique"),
                         "Tipo más común": ("Tipo de cambio", lambda s: s.value_counts().index[0]),
                         "Inventario principal": ("Inventario (corto)", lambda s: s.value_counts().index[0]),
                         "% con señal": ("Núm. alertas", lambda s: round(100 * (s > 0).mean()))})
                 .reset_index())
    tabla_mes["Mes"] = tabla_mes["Mes"].map(mes_largo)
    st.dataframe(tabla_mes, width=ANCHO, hide_index=True, height=38 * (len(tabla_mes) + 1) + 4,
                 column_config={"% con señal": st.column_config.ProgressColumn(
                     "% con señal", min_value=0, max_value=100, format="%d%%")})

# ================================================================ Comparativo
with tabs[2]:
    st.info(
        "⚠️ Los dos **no autorizaron en los mismos meses** "
        f"({'; '.join(f'{a}: {mes_corto(x.Mes.min())} a {mes_corto(x.Mes.max())}' for a, x in d.groupby('Autorizó'))}). "
        "Por eso conviene fijarse en los **porcentajes** más que en los totales."
    )
    versus(d, completo=True)
    st.caption("La barra más larga de cada renglón indica quién tiene el número más alto.")
    st.write("")

    d_tipo = d.assign(**{"Tipo de cambio": "Mínimo " + d["Mínimo"].str.lower() + " · Máximo "
                         + d["Máximo"].str.lower()})
    mostrar(pastel_por_persona(d_tipo, "Tipo de cambio", "🔀 ¿Qué combinación de cambios autoriza cada uno?",
                               top=5, alto=460))

    sol = d.groupby(["Autorizó", "Solicitud"])["Minutos para autorizar"].first().reset_index(name="Minutos")
    orden_t = ["⚡ Menos de 2 min", "⏱️ Menos de 1 hora", "📆 El mismo día", "🗓️ 1 a 3 días", "🐢 Más de 3 días"]
    sol["Tiempo"] = pd.cut(sol["Minutos"], [-1, 2, 60, 1440, 4320, 1e9], labels=orden_t).astype(str)
    colores_t = dict(zip(orden_t, ["#c62828", "#66bb6a", "#2e7d32", "#f9a825", "#8d6e63"]))
    mostrar(pastel_por_persona(sol, "Tiempo", "⏳ ¿Cuánto tarda cada uno en autorizar una solicitud?",
                               colores_t, orden_t, alto=440, centro="solicitudes"))

    c1, c2 = st.columns(2)
    mostrar(pastel_por_persona(d, "Solicitó", "🙋 ¿Quién le pide los cambios?", top=5, alto=520,
                               top_por_persona=True), c1)
    mostrar(pastel_por_persona(d, "DESCRIPCIÓN DEL INVENTARIO", "🏷️ ¿De qué inventario son?", top=5,
                               alto=480), c2)
    mostrar(pastel_por_persona(d, "NOMBRE DE SUCURSAL", "🗺️ ¿De qué regiones son los cambios?", top=6,
                               alto=480))

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
