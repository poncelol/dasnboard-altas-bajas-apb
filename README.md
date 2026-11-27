# Dashboard de movimientos de márgenes por autorizador

Separa los movimientos por **Daniel Montesinos** y **Beatriz Cervantes**, por mes, y muestra los incrementos y decrementos de margen mínimo y máximo. Incluye un botón para descargar en Excel la información filtrada.

## Correr en tu equipo

```bash
pip install -r requirements.txt
python generar_secret.py CERVANTESG_CONSPREDEFINIDA.xls > .streamlit/secrets.toml
streamlit run app.py
```

(Opcional: agrega a `secrets.toml` la sección `[app]` con `password = "..."` para pedir contraseña.)

## Publicar en Streamlit Community Cloud

1. Sube a GitHub solo `app.py`, `requirements.txt`, `generar_secret.py`, `README.md`, `.gitignore` y `.streamlit/secrets.toml.example`. **No subas el .xls ni `secrets.toml`**: el `.gitignore` ya los excluye.
2. En share.streamlit.io crea la app apuntando a `app.py`.
3. En **Settings → Secrets** pega el contenido que generó `generar_secret.py` (y la sección `[app]` si quieres contraseña).
4. Para actualizar los datos, vuelve a generar el bloque con el archivo nuevo y reemplázalo en Secrets.

## Alertas (posibles malas prácticas)

La pestaña de alertas marca cada movimiento con 12 señales para revisar: cambio extremo en máximo, máximo solicitado muy alto, baja fuerte del mínimo, mínimo en cero o negativo, rango invertido, ampliación doble, autorización exprés, autorización tardía, fuera de horario, cambio repetido, autorización en lote y auto-aprobación. Los umbrales se ajustan en la barra lateral ("Umbrales de alertas"). Las alertas son señales para pedir justificación, no prueba de una falta.

## Política del 15%

La página "Política del 15%" compara cada rango autorizado contra la *Política de Precios de Venta* (29/01/2020): precio de venta = costo × 1.1765, o sea 15% de utilidad sobre la venta (6% tienda, 1.5% Consejos Comunitarios, 7.5% gastos de operación).

- **Franjas del mínimo:** abajo del costo (< 0), no paga tienda ni Consejos (0–7.5), no cubre operación (7.5–15), en política (15) y arriba (> 15). El máximo se marca si pasa de 15.
- **¿Corrige o empeora?:** compara qué tan lejos queda el rango del 15% (|mín − 15| + |máx − 15|) antes y después del cambio.
- **Señales de posible mala práctica:** mínimo abajo del costo, mínimo que no paga tienda ni Consejos, cambio que se aleja del 15%, ventana de precio bajo (bajan el mínimo y lo suben otra vez en menos de 30 días) y mínimo fuera de lo normal para el producto (10 pts o más contra la mediana de las demás unidades).
- **Quién y dónde:** porcentajes por autorizador, solicitante y unidad operativa; una unidad es *reincidente* si se aleja del 15% 1.5 veces más que el promedio.

No revisa la regla de "5% abajo de la competencia" porque el archivo no trae costos ni precios. Las señales sirven para pedir la justificación (incisos h e i del Manual permiten vender abajo del margen con autorización por escrito).

## Buscador de listas

Las tablas de casos, las de la política del 15% y las listas largas (regiones, productos, personas) tienen un buscador: no distingue acentos ni mayúsculas, cada palabra puede ir en cualquier orden y también busca datos que no se ven (folio, clave, mes completo, tipo de cambio). Al hacer clic en el campo se despliegan sugerencias por sección (señal, producto, región, unidad, persona, mes…); se eligen con el mouse o con ↑ ↓ y Enter. Requiere Streamlit 1.66 o más.

## Filtros

Autorizó, rango de meses, región, inventario y tipo de movimiento (solo incrementos o solo decrementos, en mínimo, en máximo o en cualquiera de los dos). Las pestañas son: Por mes, Incrementos y decrementos, Comparativo y Detalle.
