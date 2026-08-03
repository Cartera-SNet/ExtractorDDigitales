# -*- coding: utf-8 -*-
"""
checklist_config.py
Antes este archivo traía dos listas fijas de tipos de documento (copiadas
a mano de una captura de pantalla). Ya no — ahora el catálogo real se
trae en vivo desde las APIs del cliente (ver auth.py:
`get_lista_tipo_documentos_api` y `get_centro_digital_api`), así que
siempre refleja lo que de verdad existe configurado en su sistema, para
la empresa/IPS con la que se inició sesión.

Este módulo solo se encarga de:
  1) Decidir cuáles de esos documentos (los que vengan de la API) el bot
     YA sabe identificar hoy (para marcarlos por defecto y sin la
     etiqueta "pronto") — ver `_es_implementado`.
  2) Convertir la respuesta cruda de cada API al formato que espera la
     pantalla: {id, nombre, implementado, default}.

Importante: el bot hoy solo IDENTIFICA de verdad 3 cosas (ver
clasificador.py): SIRAS, CÉDULA (incluye cédula de ciudadanía, PPT/permiso
de migrante y Registro Civil) y TARJETA DE PROPIEDAD. El resto del
catálogo que traiga la API queda visible en pantalla, marcado como
"pronto", para cuando se le vaya sumando la identificación de cada uno.
"""

# Observación que se muestra en el checklist de Rutas/Centro Digital,
# pedida explícitamente por el cliente.
OBSERVACION_RUTAS = (
    "Los documentos asociados al paciente se encuentran normalmente en el "
    "centro de digitalización \"Admisiones\", y los documentos relacionados "
    "a SIRAS en su carpeta respectiva a su nombre."
)


def _campo(item, *nombres, default=""):
    """Los nombres exactos de los campos que devuelve cada API todavía no
    se han confirmado con una respuesta real — se prueban varias
    variantes comunes (may/minúsculas, con/sin tilde) para no depender de
    adivinar un solo nombre. Si en la práctica ninguna calza, hay que
    ajustar esta lista con el nombre real una vez se vea una respuesta de
    ejemplo."""
    for n in nombres:
        if n in item and item[n] not in (None, ""):
            return item[n]
    return default


def _sin_tildes(texto: str) -> str:
    n = (texto or "").upper()
    for vocal_con, vocal_sin in [("Á", "A"), ("É", "E"), ("Í", "I"), ("Ó", "O"), ("Ú", "U")]:
        n = n.replace(vocal_con, vocal_sin)
    return n


def categoria_para_nombre(nombre: str):
    """¿A cuál de las 3 categorías que el bot sí sabe identificar
    (ver clasificador.py) corresponde este nombre de documento? Devuelve
    "SIRAS", "CEDULA", "TARJETA_PROPIEDAD" o None si no es ninguna de
    esas tres. Se usa para decidir la etiqueta "(P)" en el checklist, y
    para filtrar, al procesar un caso, qué categorías incluir en el PDF
    final según lo que el usuario haya marcado. OJO: esto es distinto de
    `_es_default_paciente`/`_es_default_caso` — una cosa es si el bot
    puede leer el documento, otra distinta es si viene pre-marcado."""
    n = _sin_tildes(nombre)

    if "CEDULA" in n or "REGISTRO CIVIL" in n or ("PERMISO" in n and ("PROTECCION" in n or "PERMANENCIA" in n)):
        return "CEDULA"
    if "SIRAS" in n:
        return "SIRAS"
    if "TARJETA" in n and ("PROPIEDAD" in n or "VEHICULO" in n or "TRANSITO" in n):
        return "TARJETA_PROPIEDAD"
    return None


def _es_default_paciente(nombre: str) -> bool:
    """Selección curada a mano (pedida explícitamente): de todo el
    catálogo de "Documentos del paciente", solo estos 3 vienen
    pre-marcados — Cédula de Ciudadanía, Tarjeta de Identidad y Registro
    Civil. El resto (Cédula de Extranjería, PPT, etc.) queda visible
    pero sin marcar."""
    n = _sin_tildes(nombre)
    if "CEDULA DE CIUDADANIA" in n:
        return True
    if "TARJETA DE IDENTIDAD" in n or "TARJETA DE INDENTIDAD" in n:  # el dato real trae este typo
        return True
    if "REGISTRO CIVIL" in n:
        return True
    return False


def _es_default_caso(nombre: str) -> bool:
    """Selección curada a mano para "Documentos del caso": solo Tarjeta
    Propiedad Vehículo y SIRAS Documentos vienen pre-marcados."""
    n = _sin_tildes(nombre)
    if "TARJETA" in n and ("PROPIEDAD" in n or "VEHICULO" in n):
        return True
    if "SIRAS" in n:
        return True
    return False


def _es_implementado(nombre: str) -> bool:
    """¿El bot ya identifica este tipo de documento hoy? (ver
    clasificador.py). Se decide por el nombre, sin importar mayúsculas ni
    tildes."""
    return categoria_para_nombre(nombre) is not None


def normalizar_ip_ruta(ruta_unc: str) -> str:
    """Algunas rutas de red que devuelve `obtener-centrodigital` traen una
    IP interna (192.168.2.6, el servidor Campbell) que no es la que
    realmente hay que usar para llegar ahí — el cliente confirmó que en
    la práctica hay que reemplazarla por 192.168.2.244 (mismo servidor
    Campbell, pero por la interfaz de red que sí es alcanzable). Si en el
    futuro aparecen más casos así con otros servidores, se agregan aquí
    mismo."""
    if not ruta_unc:
        return ruta_unc
    return ruta_unc.replace("192.168.2.6", "192.168.2.244")


def _es_ruta_default(nombre: str) -> bool:
    """Rutas que conviene dejar marcadas por defecto — confirmado con
    casos reales: la cédula/tarjeta se encuentra normalmente en
    "Documentos Paciente" y el SIRAS en su propia carpeta SIRAS.
    Se compara sin puntos ni espacios porque el nombre real trae puntos
    ("S.I.R.A.S. CAMPBELL"). Si con el tiempo se confirman más rutas
    típicas (para otras empresas/servidores), se agregan aquí mismo."""
    n = (nombre or "").upper().replace(".", "").replace(" ", "")
    return "SIRAS" in n or "DOCUMENTOSPACIENTE" in n


def construir_grupo(items_api, prefijo_id):
    """Convierte la respuesta cruda de `get_lista_tipo_documentos_api` (o
    de `get_centro_digital_api`) al formato {id, nombre, implementado,
    default} que ya espera la pantalla — y, para las rutas, además guarda
    `cod_centro` y `ruta` (ya con la IP normalizada), porque eso es lo que
    de verdad hace falta para ir a buscar los documentos del caso: la
    pantalla solo MUESTRA el nombre, pero el bot necesita el código y la
    ruta real de red para navegar esa carpeta.

    Nombres de campo confirmados con una respuesta real de las APIs:
      - obtener-listatipodocdigitales -> "TipoDoc" (código corto, ej.
        "CED", "PPT") y "Descripcion" (nombre a mostrar).
      - obtener-centrodigital -> "CodCentro" (código numérico), "Nombre"
        (nombre a mostrar) y "ruta" (la ruta de red real donde están los
        archivos, ej. \\\\192.168.2.6\\C30\\Admisiones\\) y "tipo"
        ("D"=documentos, "F"=fotos, "DS"=otro).

    Nota importante: las RUTAS nunca vienen marcadas por defecto —
    los nombres de los centros de digitalización cambian de una
    empresa/servidor a otro (lo que en uno se llama "Admisiones" en otro
    puede tener otro nombre), así que adivinar cuál marcar por defecto
    generaría más confusión que ayuda. El usuario elige a mano cuáles
    marcar cada vez.
    """
    grupo = []
    for i, item in enumerate(items_api or []):
        codigo = _campo(item, "TipoDoc", "CodCentro", "Codigo", "codigo", "Id", "id",
                         "IdTipoDocumento", "IdTipoDocDigital", "CodigoTipoDoc", default=str(i))
        nombre = _campo(item, "Descripcion", "Nombre", "descripcion", "nombre",
                         "NombreTipoDocumento", "NombreTipoDocDigital",
                         "NombreCentroDigital", "NombreRuta", default=f"(sin nombre #{i})")
        ruta_cruda = _campo(item, "ruta", "Ruta", default="")
        implementado = _es_implementado(nombre)
        if prefijo_id == "paciente":
            default = _es_default_paciente(nombre)
        elif prefijo_id == "caso":
            default = _es_default_caso(nombre)
        else:  # rutas: nunca vienen marcadas por defecto (ver nota arriba)
            default = False
        grupo.append({
            "id": f"{prefijo_id}_{codigo}",
            "nombre": nombre,
            "implementado": implementado,
            "default": default,
            "cod_centro": codigo,
            "ruta": normalizar_ip_ruta(ruta_cruda),
            "tipo_centro": _campo(item, "tipo", "Tipo", default=""),
        })
    return grupo


def ids_por_defecto(*grupos):
    """IDs que vienen pre-marcados al abrir el bot, de cualquier cantidad
    de grupos ya construidos con `construir_grupo`."""
    ids = []
    for grupo in grupos:
        for doc in grupo:
            if doc.get("default"):
                ids.append(doc["id"])
    return ids
