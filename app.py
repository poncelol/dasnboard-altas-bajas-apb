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
def _bytes_from_secrets() -> bytes:
    """[datos] archivo_b64 (gzip+base64) | url (+token) | ruta"""
    d = st.secrets.get("datos", {})
    if d.get("archivo_b64"):
        raw = base64.b64decode(d["archivo_b64"])
        return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    if d.get("url"):
        headers = {"Authorization": f"Bearer {d['token']}"} if d.get("token") else {}
        r = requests.get(d["url"], headers=headers, timeout=60)
        r.raise_for_status()
        return r.content
    if d.get("ruta"):
        with open(d["ruta"], "rb") as f:
            return f.read()
    st.error("No hay datos configurados en secrets. Revisa la sección [datos] de secrets.toml.")
    st.stop()


def _parse_html_xls(text: str) -> pd.DataFrame:
    """El .xls del sistema es en realidad una tabla HTML."""
    hdr = re.findall(r"<th>(.*?)</th>", text)
    rows = []
    for r in re.findall(r"<tr>(.*?)</tr>", text, flags=re.S):
        cells = re.findall(r"<td>(.*?)</td>", r, flags=re.S)
        if cells:
            rows.append([re.sub(r"<[^>]+>", "", c).strip() for c in cells])
    return pd.DataFrame(rows, columns=hdr)


def _corto(s: str) -> str:
    u = str(s).upper()
    if "CERVANTES" in u:
        return "Beatriz Cervantes"
    if "MONTESINOS" in u:
        return "Daniel Montesinos"
    return re.sub(r"\s*\(.*\)$", "", str(s)).title()


@st.cache_data(ttl=3600, show_spinner="Cargando datos…")
def cargar() -> pd.DataFrame:
    b = _bytes_from_secrets()
    head = b[:200].lower()
    if b"<html" in head or b"<table" in head:
        df = _parse_html_xls(b.decode("utf-8", errors="replace"))
    else:
        df = pd.read_excel(io.BytesIO(b))
    df.columns = [c.strip() for c in df.columns]
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


# ================================================================ app
if not check_password():
    st.stop()

df_all = cargar()

st.title("Movimientos de márgenes autorizados · Daniel vs. Beatriz")
st.caption("Mes = mes de la fecha de solicitud. Cada fila es un producto dentro de una solicitud (folio).")

with st.sidebar:
    st.header("Filtros")
    auts = sorted(df_all["Autorizó"].unique())
    f_aut = st.multiselect("Autorizó", auts, default=auts)
    meses = sorted(df_all["Mes"].unique())
    f_mes = st.select_slider("Meses", options=meses, value=(meses[0], meses[-1]))
    f_reg = st.multiselect("Región", sorted(df_all["NOMBRE DE SUCURSAL"].unique()))
    f_inv = st.multiselect("Inventario", sorted(df_all["DESCRIPCIÓN DEL INVENTARIO"].unique()))
    f_sol = st.multiselect("Solicitó", sorted(df_all["Solicitó"].unique()))
    f_tipo = st.radio("Tipo de movimiento", ["Todos", "Solo incrementos", "Solo decrementos"])
    f_campo = st.radio("Aplicar tipo a", ["Mínimo o máximo", "Solo mínimo", "Solo máximo"])

    with st.expander("Umbrales de alertas", expanded=False):
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
d = df[m]

if d.empty:
    st.warning("No hay movimientos con estos filtros.")
    st.stop()

# ---------------------------------------------------------------- KPIs
k = st.columns(4)
k[0].metric("Movimientos", f"{len(d):,}")
k[1].metric("Solicitudes", f"{d['Solicitud'].nunique():,}")
k[2].metric("Movimientos con ≥1 alerta", f"{(d['Núm. alertas'] > 0).sum():,}",
            f"{100 * (d['Núm. alertas'] > 0).mean():.0f}% del total", delta_color="off")
k[3].metric("Con 3+ alertas", f"{(d['Núm. alertas'] >= 3).sum():,}")
k2 = st.columns(max(1, d["Autorizó"].nunique()))
for c, (a, x) in zip(k2, d.groupby("Autorizó")):
    c.metric(a, f"{len(x):,} mov.",
             f"{100 * (x['Núm. alertas'] > 0).mean():.0f}% con alerta · {x['Mes'].min()} a {x['Mes'].max()}",
             delta_color="off")

tabs = st.tabs(["Por mes", "Comparativo Daniel vs. Beatriz", "Alertas (posibles malas prácticas)",
                "Revisión caso por caso", "Detalle"])

# ---------------------------------------------------------------- Por mes
with tabs[0]:
    pm = d.groupby(["Mes", "Autorizó"]).size().reset_index(name="Movimientos")
    fig = px.bar(pm, x="Mes", y="Movimientos", color="Autorizó", text="Movimientos",
                 color_discrete_map=COLORES_AUT)
    fig.update_layout(legend_title_text="", xaxis_title="", height=400)
    st.plotly_chart(fig, width=ANCHO)
    c1, c2 = st.columns(2)
    for col, campo, titulo in [(c1, "DETERMINACION_MIN", "Margen mínimo"), (c2, "DETERMINACION_MAX", "Margen máximo")]:
        x = d.groupby(["Mes", "Autorizó", campo]).size().reset_index(name="Movimientos")
        fig = px.bar(x, x="Mes", y="Movimientos", color=campo, facet_row="Autorizó",
                     color_discrete_map=COLORES_DET, title=titulo, height=520)
        fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
        fig.update_layout(legend_title_text="", xaxis_title="")
        col.plotly_chart(fig, width=ANCHO)
    st.dataframe(por_mes(d), width=ANCHO, hide_index=True)

# ---------------------------------------------------------------- Comparativo
with tabs[1]:
    st.info(
        "Al comparar, ten en cuenta que los dos autorizadores **no cubren el mismo periodo** "
        f"({', '.join(f'{a}: {x.Mes.min()} a {x.Mes.max()}, {len(x):,} mov.' for a, x in d.groupby('Autorizó'))}). "
        "Usa porcentajes y tasas, no totales, y filtra por inventario o región para comparar casos parecidos."
    )
    st.dataframe(resumen(d), width=ANCHO, hide_index=True)

    st.subheader("Qué tipo de movimiento autoriza cada uno (% de sus movimientos)")
    fig = px.bar(pct_tipo(d), x="Tipo", y="%", color="Autorizó", barmode="group", text="%",
                 color_discrete_map=COLORES_AUT)
    fig.update_layout(legend_title_text="", xaxis_title="", height=400)
    st.plotly_chart(fig, width=ANCHO)

    st.subheader("Tamaño de los cambios (puntos de margen)")
    st.dataframe(magnitudes(d), width=ANCHO, hide_index=True)
    c1, c2 = st.columns(2)
    for col, cambio, titulo in [(c1, "Cambio mín.", "Cambio en el mínimo (solo movimientos con cambio)"),
                                (c2, "Cambio máx.", "Cambio en el máximo (solo movimientos con cambio)")]:
        x = d[d[cambio] != 0]
        fig = px.box(x, x="Autorizó", y=cambio, color="Autorizó", points="all", title=titulo,
                     color_discrete_map=COLORES_AUT, hover_data=["DESCRIPCION DEL PRODUCTO", "NOMBRE DE SUCURSAL", "Mes"])
        fig.update_layout(showlegend=False, xaxis_title="", height=420)
        col.plotly_chart(fig, width=ANCHO)

    st.subheader("Combinaciones mínimo / máximo")
    comb = pd.crosstab(d["Combinación"], d["Autorizó"], normalize="columns").mul(100).round(1)
    comb.columns = [f"{c} (%)" for c in comb.columns]
    st.dataframe(comb.sort_values(comb.columns[0], ascending=False), width=ANCHO)

    st.subheader("Por inventario")
    inv = (d.groupby(["DESCRIPCIÓN DEL INVENTARIO", "Autorizó"])
           .agg(Movimientos=("Solicitud", "size"),
                **{"% mín. a la baja": ("DETERMINACION_MIN", lambda s: round(100 * (s == "DECREMENTO").mean(), 1)),
                   "% máx. al alza": ("DETERMINACION_MAX", lambda s: round(100 * (s == "INCREMENTO").mean(), 1)),
                   "Cambio prom. máx.": ("Cambio máx.", lambda s: round(s.mean(), 2))})
           .reset_index())
    st.dataframe(inv, width=ANCHO, hide_index=True)

    st.subheader("Tiempo entre solicitud y autorización")
    x = d.assign(Horas=d["Minutos para autorizar"].clip(lower=0.1) / 60)
    fig = px.histogram(x, x="Horas", color="Autorizó", log_x=True, nbins=60, barmode="overlay",
                       color_discrete_map=COLORES_AUT, histnorm="percent")
    fig.update_layout(legend_title_text="", yaxis_title="% de movimientos", height=380,
                      xaxis_title="Horas (escala logarítmica)")
    st.plotly_chart(fig, width=ANCHO)

    st.subheader("Quién le solicita a cada autorizador")
    conc = d.groupby(["Autorizó", "Solicitó"]).size().reset_index(name="Movimientos")
    conc["%"] = (100 * conc["Movimientos"] / conc.groupby("Autorizó")["Movimientos"].transform("sum")).round(1)
    conc = conc.sort_values(["Autorizó", "Movimientos"], ascending=[True, False]).groupby("Autorizó").head(8)
    fig = px.bar(conc, x="%", y="Solicitó", color="Autorizó", facet_col="Autorizó", orientation="h",
                 color_discrete_map=COLORES_AUT, text="%", height=420)
    fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
    fig.update_yaxes(matches=None, showticklabels=True, title="", categoryorder="total ascending")
    fig.update_layout(showlegend=False)
    st.plotly_chart(fig, width=ANCHO)

# ---------------------------------------------------------------- Alertas
with tabs[2]:
    st.warning(
        "Estas alertas son **señales para revisar**, no prueba de una mala práctica. Muchas tienen "
        "explicación legítima (temporada escolar, ofertas, regiones con costos distintos). Úsalas para "
        "decidir qué casos pedir que se justifiquen. Los umbrales se ajustan en la barra lateral."
    )
    with st.expander("Qué significa cada alerta"):
        for f, txt in ALERTAS.items():
            st.markdown(f"- **{f}:** {txt.format(**u)}")

    ta = tabla_alertas(d)
    st.dataframe(ta, width=ANCHO, hide_index=True)

    tasas = ta.melt(id_vars="Alerta", value_vars=[c for c in ta.columns if c.endswith("por 100 mov.")],
                    var_name="Autorizó", value_name="Por cada 100 movimientos")
    tasas["Autorizó"] = tasas["Autorizó"].str.replace(" · por 100 mov.", "", regex=False)
    fig = px.bar(tasas, y="Alerta", x="Por cada 100 movimientos", color="Autorizó", barmode="group",
                 orientation="h", color_discrete_map=COLORES_AUT, text="Por cada 100 movimientos",
                 title="Frecuencia de cada alerta (normalizada por volumen)")
    fig.update_layout(height=560, legend_title_text="", yaxis_title="", yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(fig, width=ANCHO)

    c1, c2 = st.columns(2)
    with c1:
        x = d.copy()
        x["Alertado"] = np.where(x["Núm. alertas"] > 0, "Con alerta", "Sin alerta")
        fig = px.scatter(x, x="SCMARMAXACT", y="SCMARMAXSOL", color="Autorizó", symbol="Alertado",
                         color_discrete_map=COLORES_AUT, opacity=0.7,
                         hover_data=["DESCRIPCION DEL PRODUCTO", "NOMBRE DE SUCURSAL", "Mes", "Alertas"],
                         title="Margen máximo: actual vs. solicitado")
        lim = float(max(x["SCMARMAXACT"].max(), x["SCMARMAXSOL"].max()))
        fig.add_trace(go.Scatter(x=[0, lim], y=[0, lim], mode="lines", line=dict(dash="dot", color="gray"),
                                 name="Sin cambio"))
        fig.update_layout(height=460, xaxis_title="Máximo actual", yaxis_title="Máximo solicitado")
        st.plotly_chart(fig, width=ANCHO)
    with c2:
        x = d.copy()
        fig = px.scatter(x, x="SCMARMINACT", y="SCMARMINSOL", color="Autorizó", color_discrete_map=COLORES_AUT,
                         opacity=0.7, hover_data=["DESCRIPCION DEL PRODUCTO", "NOMBRE DE SUCURSAL", "Mes", "Alertas"],
                         title="Margen mínimo: actual vs. solicitado")
        lo = float(min(x["SCMARMINACT"].min(), x["SCMARMINSOL"].min(), 0))
        hi = float(max(x["SCMARMINACT"].max(), x["SCMARMINSOL"].max()))
        fig.add_trace(go.Scatter(x=[lo, hi], y=[lo, hi], mode="lines", line=dict(dash="dot", color="gray"),
                                 name="Sin cambio"))
        fig.add_hline(y=0, line_color="red", line_width=1)
        fig.update_layout(height=460, xaxis_title="Mínimo actual", yaxis_title="Mínimo solicitado")
        st.plotly_chart(fig, width=ANCHO)

    c1, c2 = st.columns(2)
    with c1:
        hh = d.groupby(["Hora autorización", "Autorizó"]).size().reset_index(name="Movimientos")
        hh["%"] = 100 * hh["Movimientos"] / hh.groupby("Autorizó")["Movimientos"].transform("sum")
        fig = px.bar(hh, x="Hora autorización", y="%", color="Autorizó", barmode="group",
                     color_discrete_map=COLORES_AUT, title="Hora de autorización (% de cada autorizador)")
        fig.add_vrect(x0=-0.5, x1=u["u_hini"] - 0.5, fillcolor="red", opacity=0.06, line_width=0)
        fig.add_vrect(x0=u["u_hfin"] - 0.5, x1=23.5, fillcolor="red", opacity=0.06, line_width=0)
        fig.update_layout(height=380, legend_title_text="")
        st.plotly_chart(fig, width=ANCHO)
    with c2:
        sol = (d.groupby(["Autorizó", "Solicitud"])
               .agg(Minutos=("Minutos para autorizar", "first"), Productos=("Solicitud", "size")).reset_index())
        sol["Rango"] = pd.cut(sol["Minutos"], [-1, 2, 10, 60, 480, 1440, 4320, 1e9],
                              labels=["< 2 min", "2–10 min", "10–60 min", "1–8 h", "8–24 h", "1–3 días", "> 3 días"])
        sr = sol.groupby(["Autorizó", "Rango"], observed=False).size().reset_index(name="Solicitudes")
        sr["%"] = 100 * sr["Solicitudes"] / sr.groupby("Autorizó")["Solicitudes"].transform("sum")
        fig = px.bar(sr, x="Rango", y="%", color="Autorizó", barmode="group", color_discrete_map=COLORES_AUT,
                     title="Tiempo de autorización por solicitud (folio)")
        fig.update_layout(height=380, legend_title_text="", xaxis_title="")
        st.plotly_chart(fig, width=ANCHO)

    st.subheader("Productos con cambios repetidos en el mismo lugar")
    rep = (d.groupby(["Clave producto-UO", "DESCRIPCION DEL PRODUCTO", "NOMBRE DE UNIDAD OP.", "DESCRIPCIÓN DEL INVENTARIO"])
           .agg(Cambios=("Solicitud", "size"), Autorizó=("Autorizó", lambda s: ", ".join(sorted(set(s)))),
                Meses=("Mes", lambda s: ", ".join(sorted(set(s)))),
                **{"Máx. inicial": ("SCMARMAXACT", "first"), "Máx. final": ("SCMARMAXSOL", "last"),
                   "Mín. inicial": ("SCMARMINACT", "first"), "Mín. final": ("SCMARMINSOL", "last")})
           .reset_index())
    rep = rep[rep["Cambios"] >= 2].sort_values("Cambios", ascending=False).drop(columns="Clave producto-UO")
    st.dataframe(rep, width=ANCHO, hide_index=True)

# ---------------------------------------------------------------- Revisión
with tabs[3]:
    st.caption("Movimientos ordenados por número de alertas. Filtra por tipo de alerta para revisarlos uno por uno.")
    sel = st.multiselect("Mostrar movimientos con estas alertas", list(ALERTAS), default=[])
    minimo = st.slider("Mínimo de alertas por movimiento", 1, len(ALERTAS), 1)
    r = d[d["Núm. alertas"] >= minimo]
    if sel:
        r = r[r[sel].any(axis=1)]
    st.write(f"**{len(r):,}** movimientos en **{r['Solicitud'].nunique():,}** solicitudes.")
    cols = ["Núm. alertas", "Alertas", "Autorizó", "Solicitó", "Mes", "FCHSOL", "FCHAUT", "Minutos para autorizar",
            "NOMBRE DE SUCURSAL", "NOMBRE DE UNIDAD OP.", "SCFOLIO", "DESCRIPCIÓN DEL INVENTARIO",
            "DESCRIPCION DEL PRODUCTO", "SCMARMINACT", "SCMARMINSOL", "SCMARMAXACT", "SCMARMAXSOL"]
    st.dataframe(r.sort_values(["Núm. alertas", "Cambio máx."], ascending=False)[cols],
                 width=ANCHO, hide_index=True,
                 column_config={"Minutos para autorizar": st.column_config.NumberColumn(format="%.1f")})

# ---------------------------------------------------------------- Detalle
with tabs[4]:
    st.dataframe(d[[c for c in DETALLE_COLS if c in d.columns]], width=ANCHO, hide_index=True)

st.download_button(
    "⬇️ Descargar Excel con la información filtrada (incluye comparativo y alertas)",
    data=a_excel(d),
    file_name="movimientos_filtrados.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
