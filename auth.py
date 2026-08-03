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


def get_archivo_pdf_api(ip, puerto, usuario, password, codigo_emp, no_caso, rutas_archivos):
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

    resp = req_lib.get(API_ARCHIVO_PDF, params=params, timeout=30, verify=False)

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
