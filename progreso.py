# -*- coding: utf-8 -*-
"""
progreso.py
===========
Persistencia de progreso por lote de "Buscar por factura/caso", para que
el bot nunca pierda el avance ni quede en un estado inconsistente si se
detiene a mitad de camino (botón "Detener", cierre inesperado, apagón,
o el usuario vuelve a cargar el mismo caso).

Diseño (resumen)
----------------
- Cada lote de casos tiene una CLAVE ESTABLE (`clave_lote`), calculada a
  partir de usuario + empresa + el conjunto de números de caso (ver
  `calcular_clave_lote`). Esta clave es SIEMPRE la misma si se vuelve a
  enviar el mismo conjunto de casos, sin importar cuándo — eso es lo que
  permite detectar automáticamente una reanudación en vez de depender de
  que el usuario recuerde un ID de lote.
- `progreso.json` vive en `estado/progreso_<clave_lote>.json`.
- TODA escritura es transaccional: primero se escribe un archivo `.tmp`,
  se hace `flush()` + `fsync()`, se valida que el JSON quedó bien
  formado, y solo entonces se reemplaza el archivo real con
  `os.replace()` (atómico dentro del mismo filesystem). Un corte de luz
  a mitad de una escritura nunca deja `progreso.json` corrupto: o queda
  el archivo anterior completo, o el nuevo completo, nunca algo a medias.
- Cada función que modifica el progreso toma un lock específico de esa
  `clave_lote` (no un lock global), para no bloquear lotes distintos
  entre sí, pero sí evitar que dos hilos pisen la misma escritura.

Uso típico desde app.py
------------------------
    clave = progreso.calcular_clave_lote(casos, usuario, empresa)
    existente = progreso.cargar_progreso(clave)
    if existente and existente["estado"] in ("en_proceso", "detenido"):
        # preguntar al usuario: ¿continuar o limpiar todo?
        ...
    data = progreso.crear_progreso(clave, casos, sesion_datos)
    ...
    for cada caso ya resuelto:
        progreso.marcar_registro_completado(clave, no_caso, resumen, pdf)
    ...
    progreso.marcar_estado(clave, "completado")
"""

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path


def _carpeta_datos_escribibles():
    """Ver la misma función en app.py -- junto al .exe real cuando está
    empaquetado, no en la carpeta temporal de PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _carpeta_datos_escribibles()
ESTADO_DIR = BASE_DIR / "estado"
ESTADO_DIR.mkdir(exist_ok=True)

ESTADOS_VALIDOS = {"en_proceso", "detenido", "completado", "error"}

# Un lock por cada clave_lote (no uno global) -- así, procesar dos lotes
# distintos al mismo tiempo no se bloquea entre sí, pero escribir el
# mismo progreso.json desde 2 hilos a la vez sí queda serializado.
_locks = {}
_locks_guard = threading.Lock()


def _lock_de(clave_lote: str) -> threading.Lock:
    with _locks_guard:
        if clave_lote not in _locks:
            _locks[clave_lote] = threading.Lock()
        return _locks[clave_lote]


def _liberar_lock(clave_lote: str):
    with _locks_guard:
        _locks.pop(clave_lote, None)


# ─────────────────────────────────────────────────────────────
# Clave estable del lote
# ─────────────────────────────────────────────────────────────

def calcular_clave_lote(casos, usuario, empresa) -> str:
    """Hash corto y estable: mismo usuario + empresa + mismo conjunto de
    NoCaso (sin importar el orden) -> siempre la misma clave. Es lo que
    permite reconocer automáticamente "este es el mismo caso/lote de
    antes" sin que el usuario tenga que guardar ningún ID a mano."""
    numeros = sorted(str(c.get("NoCaso", "")).strip() for c in casos if c.get("NoCaso"))
    base = f"{usuario}|{empresa}|" + ",".join(numeros)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def ruta_progreso(clave_lote: str) -> Path:
    return ESTADO_DIR / f"progreso_{clave_lote}.json"


# ─────────────────────────────────────────────────────────────
# Escritura transaccional
# ─────────────────────────────────────────────────────────────

def _escribir_atomico(ruta: Path, data: dict):
    """Escribe `data` en `ruta` de forma transaccional:
    1) escribe a un archivo .tmp
    2) flush() + fsync() -- fuerza que quede físicamente en disco
    3) valida releyendo el .tmp como JSON
    4) reemplaza el archivo real con os.replace() (atómico)

    Si el proceso se corta en cualquier punto de los pasos 1-3, el
    archivo real (`ruta`) NUNCA se toca -- sigue siendo el progreso
    válido anterior. Solo si los 3 pasos salen bien se hace el swap.
    """
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    contenido = json.dumps(data, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(contenido)
        fh.flush()
        os.fsync(fh.fileno())
    with open(tmp, "r", encoding="utf-8") as fh:
        json.load(fh)  # valida que quedó bien escrito antes de reemplazar
    os.replace(tmp, ruta)


def cargar_progreso(clave_lote: str):
    """Devuelve el dict de progreso, o None si no existe (lote nuevo) o
    si el archivo está corrupto por alguna causa externa a este módulo
    (ej. editado a mano) -- en ese caso se trata como "no hay progreso
    todavía" en vez de tumbar el lote completo con una excepción."""
    ruta = ruta_progreso(clave_lote)
    if not ruta.exists():
        return None
    try:
        with open(ruta, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def existe_progreso_activo(clave_lote: str) -> bool:
    """True si hay un progreso previo que tiene sentido ofrecer para
    reanudar (en_proceso o detenido) -- uno ya completado no cuenta,
    porque no hay nada que reanudar (si el usuario reenvía los mismos
    casos de un lote ya completado, se trata como lote nuevo)."""
    data = cargar_progreso(clave_lote)
    return bool(data and data.get("estado") in ("en_proceso", "detenido"))


def crear_progreso(clave_lote: str, casos, sesion_datos: dict, tipos_documento=None, rutas=None) -> dict:
    """Crea (o reinicia desde cero) el progreso.json de un lote.

    Se guardan también `casos`, `tipos_documento` y `rutas` tal cual se
    recibieron -- no solo para saber CUÁNTO falta, sino para poder
    reanudar el lote SIN depender de que el navegador todavía tenga esa
    información en memoria (ej. si se refrescó la página o se cerró y
    se volvió a abrir): el botón "Reanudar" puede reconstruir la
    petición completa leyendo esto desde el servidor."""
    lock = _lock_de(clave_lote)
    with lock:
        ahora = time.strftime("%Y-%m-%d %H:%M:%S")
        data = {
            "clave_lote": clave_lote,
            "caso_actual": None,
            "inicio": ahora,
            "ultima_actualizacion": ahora,
            "estado": "en_proceso",  # en_proceso | detenido | completado | error
            "usuario": sesion_datos.get("usuario", "?"),
            "empresa": sesion_datos.get("empresa", "?"),
            "casos": list(casos),  # lista original completa (NoCaso/NoFactura) -- así se puede
                                    # reanudar sin que el usuario tenga que volver a escribirlos,
                                    # incluso después de recargar la página o reiniciar el servidor
            "total_registros": len(casos),
            "indice_actual": 0,
            "ultimo_registro_procesado": None,
            "completados": [],            # [no_caso, ...]
            "con_error": [],               # [{"no_caso":.., "mensaje":..}, ...]
            "archivos_descargados": [],    # [no_caso, ...] -- ya se trajo el PDF de la API
            "documentos_generados": [],    # [nombre_pdf, ...]
            "resumenes": {},               # {no_caso: resumen_dict} -- para no reprocesar al reanudar
            "porcentaje": 0.0,
            "casos": list(casos),
            "tipos_documento": list(tipos_documento or []),
            "rutas": list(rutas or []),
        }
        _escribir_atomico(ruta_progreso(clave_lote), data)
        return data


def _guardar(clave_lote: str, data: dict):
    data["ultima_actualizacion"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _escribir_atomico(ruta_progreso(clave_lote), data)


def marcar_caso_actual(clave_lote: str, no_caso: str):
    lock = _lock_de(clave_lote)
    with lock:
        data = cargar_progreso(clave_lote)
        if data is None:
            return
        data["caso_actual"] = no_caso
        _guardar(clave_lote, data)


def registrar_archivo_descargado(clave_lote: str, no_caso: str):
    """Se llama apenas se trae el PDF real de la API para un caso -- si
    el proceso se corta justo después (antes de terminar de clasificar),
    una reanudación sabe que el archivo YA se descargó y no debe volver
    a pedirlo a la API para nada (ver `registros_pendientes`, que igual
    evita reprocesar el caso completo una vez está en completados/error;
    este campo queda además como bitácora explícita para auditoría)."""
    lock = _lock_de(clave_lote)
    with lock:
        data = cargar_progreso(clave_lote)
        if data is None:
            return
        if no_caso not in data["archivos_descargados"]:
            data["archivos_descargados"].append(no_caso)
            _guardar(clave_lote, data)


def marcar_registro_completado(clave_lote: str, no_caso: str, resumen: dict, pdf_generado=None):
    """Se llama INMEDIATAMENTE después de terminar un registro con
    éxito -- no se espera a que termine el lote entero. `pdf_generado`
    es la tupla (nombre_pdf, contenido_bytes) si se generó un PDF para
    este caso, o None si no aplicó."""
    lock = _lock_de(clave_lote)
    with lock:
        data = cargar_progreso(clave_lote)
        if data is None:
            return
        if no_caso not in data["completados"]:
            data["completados"].append(no_caso)
        # Si el mismo caso ya había quedado marcado con error en un
        # intento anterior (reanudación) y ahora sí funcionó, se saca de
        # la lista de errores para no dejar 2 estados contradictorios.
        data["con_error"] = [e for e in data["con_error"] if e["no_caso"] != no_caso]
        data["resumenes"][no_caso] = resumen
        if pdf_generado:
            nombre = pdf_generado[0]
            if nombre not in data["documentos_generados"]:
                data["documentos_generados"].append(nombre)
        data["ultimo_registro_procesado"] = no_caso
        data["indice_actual"] = len(data["completados"]) + len(data["con_error"])
        data["porcentaje"] = round(100 * data["indice_actual"] / max(1, data["total_registros"]), 1)
        _guardar(clave_lote, data)


def marcar_registro_error(clave_lote: str, no_caso: str, mensaje: str, resumen=None):
    """Se llama INMEDIATAMENTE después de que un registro termine en
    error -- igual que el de éxito, no se espera al final del lote."""
    lock = _lock_de(clave_lote)
    with lock:
        data = cargar_progreso(clave_lote)
        if data is None:
            return
        data["con_error"] = [e for e in data["con_error"] if e["no_caso"] != no_caso]
        data["con_error"].append({"no_caso": no_caso, "mensaje": str(mensaje)})
        if resumen is not None:
            data["resumenes"][no_caso] = resumen
        data["ultimo_registro_procesado"] = no_caso
        data["indice_actual"] = len(data["completados"]) + len(data["con_error"])
        data["porcentaje"] = round(100 * data["indice_actual"] / max(1, data["total_registros"]), 1)
        _guardar(clave_lote, data)


def marcar_estado(clave_lote: str, estado: str):
    if estado not in ESTADOS_VALIDOS:
        raise ValueError(f"Estado de progreso inválido: {estado!r}")
    lock = _lock_de(clave_lote)
    with lock:
        data = cargar_progreso(clave_lote)
        if data is None:
            return
        data["estado"] = estado
        _guardar(clave_lote, data)


def registros_pendientes(clave_lote: str, casos):
    """Devuelve (pendientes, progreso_actual).

    `pendientes` es la sub-lista de `casos` que hace falta (re)procesar
    en esta corrida:
      - Nunca vuelve a tocar un caso que ya está en `completados` (sea
        COMPLETO, INCOMPLETO o SIN DOCUMENTOS -- ya se descargó su
        archivo real, si lo tenía, y ya se sabe su resultado).
      - SÍ vuelve a intentar los casos que quedaron en `con_error`
        (nunca se llegó a descargar nada) -- por diseño: si el usuario
        vuelve a darle "Buscar y procesar" con el mismo lote SIN borrar
        el progreso, la intención natural es "reintenta lo que falló",
        no "ignóralo para siempre a menos que borre todo". Si el
        reintento tiene éxito, `marcar_registro_completado` ya se
        encarga de sacarlo de `con_error` y pasarlo a `completados`; si
        vuelve a fallar, `marcar_registro_error` simplemente actualiza
        el mismo registro (no se duplica).

    Si no hay progreso previo, `pendientes` es la lista completa (lote
    nuevo) y `progreso_actual` es None."""
    data = cargar_progreso(clave_lote)
    if data is None:
        return list(casos), None
    ya_resueltos = set(data["completados"])  # los con_error NO cuentan como resueltos -- se reintentan solos
    pendientes = [c for c in casos if str(c.get("NoCaso", "")).strip() not in ya_resueltos]
    return pendientes, data


def resumenes_ya_completados(clave_lote: str):
    """Lista de (no_caso, resumen) de todo lo que ya quedó resuelto en
    corridas anteriores de este mismo lote -- se usa para que el
    Excel/ZIP final de una reanudación incluya TODO (lo viejo + lo
    nuevo), no solo lo que se procesó en esta última corrida."""
    data = cargar_progreso(clave_lote)
    if data is None:
        return []
    return list(data.get("resumenes", {}).items())


def limpiar_todo(clave_lote: str, carpetas_extra=None):
    """Botón 'Limpiar todo': borra progreso.json (+ su .tmp si quedó
    alguno suelto) y cualquier carpeta de trabajo asociada a este lote
    (uploads temporales, descargas, PDFs de salida, caché) que se le
    pase en `carpetas_extra`. Al terminar, el caso queda exactamente
    como si nunca se hubiera ejecutado -- se puede volver a cargar sin
    ningún residuo ni conflicto de estado."""
    lock = _lock_de(clave_lote)
    with lock:
        ruta = ruta_progreso(clave_lote)
        tmp = ruta.with_suffix(ruta.suffix + ".tmp")
        for f in (ruta, tmp):
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass
        for carpeta in (carpetas_extra or []):
            try:
                shutil.rmtree(carpeta, ignore_errors=True)
            except Exception:
                pass
    _liberar_lock(clave_lote)


def buscar_progreso_pendiente(usuario: str, empresa: str):
    """Recorre estado/ buscando un progreso SIN TERMINAR (en_proceso o
    detenido) de este mismo usuario+empresa — sin necesitar de antemano
    la clave_lote ni la lista de casos. Es lo que permite que, al
    recargar la página o reiniciar el servidor, la interfaz sepa por su
    cuenta que hay algo para reanudar. Si hay más de uno (no debería ser
    lo normal), devuelve el más reciente."""
    candidatos = []
    for archivo in ESTADO_DIR.glob("progreso_*.json"):
        if archivo.suffix == ".tmp":
            continue
        try:
            with open(archivo, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue  # archivo corrupto o a medio escribir -- se ignora, no se ofrece para reanudar
        if (data.get("usuario") == usuario and data.get("empresa") == empresa
                and data.get("estado") in ("en_proceso", "detenido")):
            candidatos.append(data)
    if not candidatos:
        return None
    candidatos.sort(key=lambda d: d.get("ultima_actualizacion", ""), reverse=True)
    return candidatos[0]


def resumen_para_mostrar(data: dict) -> dict:
    """Versión cortica del progreso, pensada para mandar al navegador
    (no hace falta mandarle los resumenes completos de cada caso)."""
    if not data:
        return {}
    return {
        "estado": data.get("estado"),
        "inicio": data.get("inicio"),
        "ultima_actualizacion": data.get("ultima_actualizacion"),
        "total_registros": data.get("total_registros", 0),
        "completados": len(data.get("completados", [])),
        "con_error": len(data.get("con_error", [])),
        "porcentaje": data.get("porcentaje", 0.0),
        "ultimo_registro_procesado": data.get("ultimo_registro_procesado"),
    }
