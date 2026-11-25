"""
Convierte el archivo exportado (.xls) en el bloque [datos] para .streamlit/secrets.toml.

Uso:
    python generar_secret.py RUTA_DEL_ARCHIVO.xls  >> .streamlit/secrets.toml

En Streamlit Community Cloud copia la salida en: App → Settings → Secrets.
"""
import base64
import gzip
import sys

if len(sys.argv) != 2:
    sys.exit("Uso: python generar_secret.py archivo.xls")

with open(sys.argv[1], "rb") as f:
    datos = gzip.compress(f.read(), compresslevel=9)

b64 = base64.b64encode(datos).decode()
print("[datos]")
print(f'archivo_b64 = "{b64}"')
print(f"# {len(b64)/1024:.0f} KB en secrets", file=sys.stderr)
