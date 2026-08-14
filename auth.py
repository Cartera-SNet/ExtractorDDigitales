# -*- coding: utf-8 -*-
"""
auth.py
Login por Servidor + Usuario + Contraseña + Empresa, usando las mismas
APIs que ya usa Esculapio (Certificaciones Médicas):

    - obtener-servidores  -> lista de servidores disponibles (ip/puerto)
    - obtener-empresas    -> valida usuario/clave contra un servidor y
                             devuelve las empresas/IPS a las que tiene
                             acceso ese usuario

La lógica y el flujo (2 pasos: conectar -> elegir empresa -> entrar) son
los mismos que en el proyecto "Esculapio CM Local" que ya tenían armado;
aquí solo se adaptaron los nombres para este bot.
"""

import time
from functools import wraps
from urllib.parse import urlencode

import requests as req_lib
from flask import session, request, jsonify, redirect, url_for

# ─────────────────────────────────────────────────────────────
# Servidor Barú: ajuste de timeout
# ─────────────────────────────────────────────────────────────
# El servidor de Barú responde lento (mediciones reales: 52s-112s por
# archivo). El timeout genérico de 30s que usan los demás servidores
# se queda corto y dispara cancelaciones en casos que sí iban a
# llegar. Aquí solo se sube el TOPE de espera a 180s SOLO para Barú;
# el resto de servidores sigue con 30s como siempre. NO es un tiempo
# fijo: si la API responde en 8s, termina en 8s; el 180s es solo el
# techo a partir del cual se cancela y reintenta.
_BARU_NORMALIZADO = "BARU"
_TIMEOUT_BARU_S = 180
_TIMEOUT_OTROS_S = 30


def _servidor_es_baru(servidor):
    """True si el nombre del servidor (como viene de la sesión) es Barú,
    sin importar mayúsculas/tildes (igual que ya se hace en
    `_conexion_checklist_documentos` en app.py)."""
    if not servidor:
        return False
    s = str(servidor).strip().upper()
    for letra_con, letra_sin in (("Á", "A"), ("É", "E"), ("Í", "I"), ("Ó", "O"), ("Ú", "U")):
        s = s.replace(letra_con, letra_sin)
    return s == _BARU_NORMALIZADO

# Se desactivan los warnings de verify=False (los certificados de estos
# servidores internos a veces no están firmados por una CA pública).
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

API_SERVIDORES = "https://appsintranet.esculapiosis.com/ApiCampbell/api/Usuarios/obtener-servidores"
API_EMPRESAS = "https://appsintranet.esculapiosis.com/ApiCampbell/api/Usuarios/obtener-empresas"

# Catálogo real de documentos y rutas de digitalización — reemplaza los
# listados fijos que había antes en checklist_config.py. Se piden en vivo
# cada vez que se carga la pantalla principal, usando la conexión de la
# sesión activa (mismo ip/puerto/usuario/clave que ya se usa para todo lo
# demás) — así el checklist siempre refleja lo que de verdad existe en el
# sistema del cliente, no una lista copiada a mano.
API_CENTRO_DIGITAL = "https://appsintranet.esculapiosis.com/ApiCampbell/api/ArchivoDigital/obtener-centrodigital"
API_LISTA_TIPO_DOC = "https://appsintranet.esculapiosis.com/ApiCampbell/api/ArchivoDigital/obtener-listatipodocdigitales"
API_ARCHIVO_PDF = "https://appsintranet.esculapiosis.com/ApiCampbell/api/ArchivoDigital/obtener-archivo-pdf"


def construir_url_archivo_pdf(ip, puerto, usuario, password, codigo_emp, no_caso, rutas_archivos):
    """Arma la URL EXACTA (con los parámetros reales) que se le manda a la
    API `obtener-archivo-pdf` — solo para mostrarla/loguearla, así se
    puede copiar y pegar en el navegador para probar a mano cuando algo
    da 404 y no se sabe si es un problema del bot o de dónde vive
    realmente el documento. La llamada real la sigue haciendo
    `get_archivo_pdf_api` con `requests` (esto no reemplaza esa lógica,
    solo reconstruye la misma URL para depurar)."""
    params = [
        ("IpConexion", ip),
        ("BdConexion", "bd"),
        ("PortConexion", puerto),
        ("Usuario", usuario),
        ("PasswordUsu", password),
        ("CodigoEmp", codigo_emp),
        ("NoCaso", no_caso),
    ]
    for ruta in rutas_archivos:
        if ruta:
            params.append(("RutasArchivos", ruta))
    return f"{API_ARCHIVO_PDF}?{urlencode(params)}"


def get_archivo_pdf_api(ip, puerto, usuario, password, codigo_emp, no_caso, rutas_archivos, intentos=3, servidor=""):
    """Trae el/los archivo(s) reales de un caso — reemplaza la necesidad
    de navegar la carpeta de red a mano: esta API ya busca dentro de las
    rutas indicadas (`rutas_archivos`, una lista — se manda como el
    parámetro repetido "RutasArchivos", una vez por cada ruta marcada en
    el checklist) y devuelve el archivo del caso `no_caso`.

    Todavía no se confirmó si la respuesta es el PDF directo (binario) o
    un JSON con el contenido en base64 — por eso se devuelve de forma
    genérica: (contenido, es_binario_directo, content_type), y quien
    llame decide cómo procesarlo. `raise` con el detalle si algo falla,
    en vez de devolver silenciosamente None (para poder ver el error real
    en el log del bot, no solo "no se encontró nada").

    IMPORTANTE — 404 real vs. error de conexión: un 404 significa que EL
    SERVIDOR SÍ RESPONDIÓ y dijo explícitamente "no está ahí" — eso NO se
    reintenta (reintentar no lo va a cambiar, el documento no está en esa
    ruta y ya). Un timeout, una conexión rechazada/reiniciada, o un error
    5xx del servidor son técnicamente distintos: pueden ser un problema
    pasajero de red, así que SÍ se reintentan (`intentos` veces, con una
    pequeña espera creciente entre cada uno) antes de darse por vencido.
    Confundir estos dos casos fue justo lo que pasó en un lote real: 24
    casos quedaron marcados "no encontrado" sin saber si de verdad no
    existían ahí o si fue la red — con esto, un tropiezo de red pasajero
    ya no cuenta como si el documento no existiera.

    Parámetro `servidor` (opcional): si es Barú, se usa un timeout de
    180s en vez de los 30s genéricos. NO es un tiempo fijo de espera: si
    la API responde en 8s, la llamada termina en 8s. El 180s es solo el
    techo a partir del cual se cancela y se reintenta. Esto es por la
    lentitud propia del servidor de Barú (52s-112s por archivo en
    producción) — con el tope viejo de 30s se cancelaban casos que sí
    iban a llegar. Para los demás servidores, el comportamiento es
    EXACTAMENTE el mismo de antes: 30s, sin más cambios.
    """
    params = [
        ("IpConexion", ip),
        ("BdConexion", "bd"),
        ("PortConexion", puerto),
        ("Usuario", usuario),
        ("PasswordUsu", password),
        ("CodigoEmp", codigo_emp),
        ("NoCaso", no_caso),
    ]
    for ruta in rutas_archivos:
        if ruta:
            params.append(("RutasArchivos", ruta))

    timeout_actual = _TIMEOUT_BARU_S if _servidor_es_baru(servidor) else _TIMEOUT_OTROS_S

    ultimo_error_tecnico = None
    resp = None
    for intento in range(1, intentos + 1):
        try:
            resp = req_lib.get(API_ARCHIVO_PDF, params=params, timeout=timeout_actual, verify=False)
            break  # se conectó y hubo respuesta (sea cual sea el código) -- ya no hay que reintentar la conexión
        except req_lib.exceptions.RequestException as e:
            ultimo_error_tecnico = e
            if intento < intentos:
                time.sleep(1.5 * intento)  # 1.5s, 3s, ... -- le da tiempo a que la red se recupere sola
                continue
            raise RuntimeError(
                f"Error de conexión (esto NO es un 404 -- no se sabe todavía si el caso {no_caso} "
                f"existe o no en esta ruta): tras {intentos} intento(s), no se pudo conectar/hubo "
                f"timeout consultando la API. Detalle técnico: {e}. Vale la pena reintentar este "
                f"caso más tarde en vez de asumir que el documento no existe."
            )

    if resp.status_code == 404:
        rutas_intentadas = ", ".join(r for r in rutas_archivos if r)
        raise RuntimeError(
            f"La API respondió 404 (no encontrado) buscando el caso {no_caso} en: "
            f"{rutas_intentadas}. Esto casi siempre significa que el caso SÍ existe, "
            f"pero NO en esa(s) ruta(s) específica(s) — cada centro de digitalización "
            f"guarda un tipo de documento distinto (ej. 'Admisiones' vs 'SIRASCampbell'). "
            f"Prueba marcando otra ruta en el checklist, o verifica en cuál de ellas "
            f"está realmente el documento de este caso."
        )

    if resp.status_code >= 500 and intentos > 1:
        # El servidor respondió, pero con un error propio suyo (no un 404
        # "no está aquí") -- también vale la pena reintentar una vez más
        # antes de rendirse, por si fue una caída momentánea del lado del
        # cliente, no reintentado arriba porque ahí solo se reintentan
        # errores de CONEXIÓN (sin respuesta), no de servidor (con
        # respuesta pero con error).
        for intento in range(2, intentos + 1):
            time.sleep(1.5 * (intento - 1))
            try:
                resp = req_lib.get(API_ARCHIVO_PDF, params=params, timeout=timeout_actual, verify=False)
            except req_lib.exceptions.RequestException:
                continue
            if resp.status_code == 404:
                rutas_intentadas = ", ".join(r for r in rutas_archivos if r)
                raise RuntimeError(
                    f"La API respondió 404 (no encontrado) buscando el caso {no_caso} en: "
                    f"{rutas_intentadas}."
                )
            if resp.status_code < 500:
                break
        if resp.status_code >= 500:
            raise RuntimeError(
                f"Error de conexión (esto NO es un 404): el servidor respondió {resp.status_code} "
                f"repetidamente para el caso {no_caso}. Vale la pena reintentar este caso más "
                f"tarde en vez de asumir que el documento no existe."
            )

    resp.raise_for_status()

    content_type = (resp.headers.get("Content-Type") or "").lower()
    es_binario = "application/pdf" in content_type or "octet-stream" in content_type
    if es_binario:
        return resp.content, True, content_type
    else:
        # No vino como binario -> se asume JSON (con el pdf en base64 en
        # algún campo, formato exacto por confirmar con una respuesta real)
        return resp.json(), False, content_type


def get_centro_digital_api(ip, puerto, usuario, password, codigo_emp):
    """Rutas/centros de digitalización disponibles para la empresa activa
    (ej. "Admisiones", carpeta de SIRAS, etc.) — usados para saber DÓNDE
    buscar los documentos, no QUÉ tipo de documento son.

    Devuelve (datos, error) — si algo falla, `error` trae el detalle
    exacto en vez de quedar en silencio (antes, si esta llamada fallaba
    pero las otras dos sí funcionaban, no había forma de saber por qué
    esta en particular venía vacía)."""
    try:
        params = {
            "IpConexion": ip,
            "BdConexion": "bd",
            "PortConexion": puerto,
            "Usuario": usuario,
            "PasswordUsu": password,
            "CodigoEmp": codigo_emp,
        }
        resp = req_lib.get(API_CENTRO_DIGITAL, params=params, timeout=15, verify=False)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return [], f"la API respondió algo que no es una lista: {str(data)[:200]}"
        return data, None
    except Exception as e:
        return [], str(e)


def get_lista_tipo_documentos_api(ip, puerto, usuario, password, tipo):
    """Catálogo de tipos de documento digital. `tipo`: "AP" = asociados al
    paciente, "AC" = asociados al caso. Devuelve (datos, error), igual
    que `get_centro_digital_api`."""
    try:
        params = {
            "IpConexion": ip,
            "BdConexion": "bd",
            "PortConexion": puerto,
            "Usuario": usuario,
            "PasswordUsu": password,
            "Tipo": tipo,
        }
        resp = req_lib.get(API_LISTA_TIPO_DOC, params=params, timeout=15, verify=False)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return [], f"la API respondió algo que no es una lista: {str(data)[:200]}"
        return data, None
    except Exception as e:
        return [], str(e)

_cache_servidores = None
_cache_servidores_ts = 0
_CACHE_SEGUNDOS = 300


def get_servidores_api():
    """Lista de servidores disponibles (cacheada 5 min para no golpear la
    API en cada carga de la página de login)."""
    global _cache_servidores, _cache_servidores_ts
    if _cache_servidores and (time.time() - _cache_servidores_ts) < _CACHE_SEGUNDOS:
        return _cache_servidores
    try:
        resp = req_lib.get(API_SERVIDORES, timeout=10, verify=False)
        resp.raise_for_status()
        data = resp.json()
        _cache_servidores = data if isinstance(data, list) else []
        _cache_servidores_ts = time.time()
        return _cache_servidores
    except Exception:
        return _cache_servidores or []


def get_empresas_api(ip, puerto, usuario, password):
    """Valida credenciales contra el servidor dado y devuelve la lista de
    empresas/IPS a las que tiene acceso ese usuario. Lista vacía = clave
    inválida o usuario sin empresas asignadas."""
    try:
        params = {
            "IpConexion": ip,
            "BdConexion": "bd",
            "PortConexion": puerto,
            "Usuario": usuario,
            "PasswordUsu": password,
        }
        resp = req_lib.get(API_EMPRESAS, params=params, timeout=15, verify=False)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
# Rate limiting simple de intentos de login (por IP)
# ─────────────────────────────────────────────────────────────

_login_intentos = {}
_LOGIN_MAX = 5
_LOGIN_VENTANA = 600    # 10 min
_LOGIN_BLOQUEO = 900    # 15 min


def ip_solicitante():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


def chequear_rate(ip):
    ahora = time.time()
    d = _login_intentos.get(ip, {})
    if d.get("bloqueado_hasta", 0) > ahora:
        restante = int(d["bloqueado_hasta"] - ahora)
        return False, f"Demasiados intentos fallidos. Intenta de nuevo en {restante // 60}m {restante % 60}s."
    if ahora - d.get("primer_intento", 0) > _LOGIN_VENTANA:
        _login_intentos.pop(ip, None)
    return True, ""


def marcar_fallo(ip):
    ahora = time.time()
    d = _login_intentos.get(ip, {})
    if not d or ahora - d.get("primer_intento", 0) > _LOGIN_VENTANA:
        d = {"intentos": 0, "primer_intento": ahora}
    d["intentos"] += 1
    if d["intentos"] >= _LOGIN_MAX:
        d["bloqueado_hasta"] = ahora + _LOGIN_BLOQUEO
    _login_intentos[ip] = d


def marcar_exito(ip):
    _login_intentos.pop(ip, None)


# ─────────────────────────────────────────────────────────────
# Decorador de sesión requerida
# ─────────────────────────────────────────────────────────────

def login_requerido(f):
    @wraps(f)
    def envoltura(*args, **kwargs):
        if not session.get("autenticado"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "No autorizado", "login_required": True}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return envoltura


def sesion_actual():
    """Devuelve los datos de conexión guardados en la sesión (para armar
    los parámetros de las llamadas a la API de documentos más adelante)."""
    return {
        "usuario": session.get("usuario", ""),
        "servidor": session.get("servidor", ""),
        "ip": session.get("ip", ""),
        "puerto": session.get("puerto", "3306"),
        "password": session.get("password", ""),
        "empresa": session.get("empresa", ""),
        "empresas": session.get("empresas", []),
    }
