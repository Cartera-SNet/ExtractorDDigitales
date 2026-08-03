# -*- coding: utf-8 -*-
"""
ips_mapping.py
Utilidad de referencia para la SIGUIENTE etapa del proyecto (todavía no se
usa dentro de app.py).

Cuando conectemos el bot a la fuente real de documentos, cada IPS/sede se
va a identificar por NIT. Este módulo ya deja lista la carga del archivo
'config/mapeo_ips.xlsx' (la misma copia que se compartió) para poder cruzar
NIT -> Nombre de empresa -> Departamento, y así saber contra qué sede /
convenio corresponde cada factura.

Uso previsto (a futuro):
    from ips_mapping import cargar_mapeo, buscar_por_nit
    mapeo = cargar_mapeo()
    info = buscar_por_nit(mapeo, "900600550-9")
"""

from pathlib import Path

try:
    import openpyxl
except ImportError:
    openpyxl = None

RUTA_MAPEO = Path(__file__).resolve().parent / "config" / "mapeo_ips.xlsx"


def _normalizar_nit(nit: str) -> str:
    """Deja el NIT solo con dígitos, para poder comparar
    '900600550-9', '900600550 9' y '9006005509' como iguales."""
    return "".join(ch for ch in str(nit) if ch.isdigit())


def cargar_mapeo(ruta=RUTA_MAPEO):
    """Carga el Excel de mapeo IPS/Empresas.

    Devuelve una lista de dicts: [{"nombre_empresa": ..., "nit": ...,
    "departamento": ...}, ...]
    """
    if openpyxl is None:
        raise RuntimeError("openpyxl no está instalado")
    if not Path(ruta).exists():
        return []

    wb = openpyxl.load_workbook(ruta, data_only=True)
    ws = wb.active
    filas = list(ws.iter_rows(values_only=True))
    if not filas:
        return []

    encabezado = [str(c).strip().lower() if c else "" for c in filas[0]]
    resultado = []
    for fila in filas[1:]:
        if not any(fila):
            continue
        registro = dict(zip(encabezado, fila))
        resultado.append({
            "nombre_empresa": registro.get("nombre empresa", ""),
            "nit": registro.get("nit", ""),
            "departamento": registro.get("dpto", ""),
        })
    return resultado


def buscar_por_nit(mapeo, nit_buscado: str):
    """Busca una IPS/empresa por NIT (tolerante a guiones/espacios)."""
    nit_norm = _normalizar_nit(nit_buscado)
    for registro in mapeo:
        if _normalizar_nit(registro.get("nit", "")) == nit_norm:
            return registro
    return None


if __name__ == "__main__":
    datos = cargar_mapeo()
    print(f"{len(datos)} registros cargados desde {RUTA_MAPEO.name}")
    for r in datos[:5]:
        print(r)
