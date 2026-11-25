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

## Filtros

Autorizó, rango de meses, región, inventario y tipo de movimiento (solo incrementos o solo decrementos, en mínimo, en máximo o en cualquiera de los dos). Las pestañas son: Por mes, Incrementos y decrementos, Comparativo y Detalle.
