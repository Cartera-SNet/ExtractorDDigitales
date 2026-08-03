# -*- coding: utf-8 -*-
"""
clasificador.py
Núcleo de identificación de documentos dentro de un PDF de reclamación SOAT.

Identifica 3 tipos de documento dentro de un PDF (que puede traer mezclados:
FURIPS, facturas, epicrisis, órdenes médicas, RUNT, etc.):

    - SIRAS                 -> Reporte SIRAS (víctima accidente de tránsito)
    - CEDULA                -> Cédula de ciudadanía o Permiso por Protección
                               Temporal (documento de migrante venezolano)
    - TARJETA_PROPIEDAD     -> Licencia de tránsito / tarjeta de propiedad del
                               vehículo

Checklist de identificación (ver KEYWORDS_SIRAS / KEYWORDS_CEDULA /
KEYWORDS_TARJETA más abajo): cada categoría se reconoce por una lista de
frases/campos típicos de su formato, con un peso cada una. Si el texto de
la página (nativo o por OCR) junta suficiente puntaje de una categoría
(>= UMBRAL_PUNTAJE), la página queda marcada con esa categoría — igual que
un checklist: "si tiene esto + esto + esto, es tal documento". Por
ejemplo, para SIRAS se busca el título institucional y los nombres de sus
secciones numeradas; para cédula/PPT se busca "Cédula de Ciudadanía" o
"Permiso por Protección Temporal" + Migración Colombia; para tarjeta de
propiedad se busca "Licencia de Tránsito" + placa + datos del vehículo.

Cuando cédula y tarjeta de propiedad vienen fotografiadas juntas en una
misma página (caso típico: tarjeta arriba, cédula abajo), el bot NO las dej
a como un solo documento: intenta encontrar el punto de corte entre las dos
fotos y las separa en dos páginas independientes en el PDF de salida (ver
`_revisar_documentos_pegados` y `construir_pdf_unificado`).

Este módulo NO depende todavía de ninguna API externa: trabaja sobre un PDF
que ya está en disco. Cuando se conecte la fuente real (API + mapeo de IPS),
sólo hay que reemplazar la forma en la que se obtiene el PDF de entrada; toda
la lógica de clasificación puede reutilizarse tal cual.
"""

import io
import os
import re
import shutil
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from dataclasses import dataclass, field

import fitz  # PyMuPDF

# ─────────────────────────────────────────────────────────────
# Motor de OCR: se usa Tesseract si está instalado en el sistema
# (más rápido y liviano). Si NO está instalado, se cae automáticamente
# a EasyOCR (se instala solo con pip, sin instalador aparte de Windows;
# la primera vez que se usa descarga sus modelos, ~65 MB, y luego
# funciona sin internet). No hace falta configurar nada: el bot elige
# solo cuál usar.
#
# Nota sobre instalaciones silenciosas (winget): a veces el instalador
# copia los archivos de Tesseract correctamente pero no queda agregado
# al PATH del sistema. Por eso, además de buscarlo en el PATH, también
# se revisan las carpetas donde el instalador oficial lo deja por
# defecto — así funciona sin tener que tocar el PATH de Windows a mano.
# ─────────────────────────────────────────────────────────────

RUTAS_TESSERACT_CONOCIDAS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%USERPROFILE%\AppData\Local\Tesseract-OCR\tesseract.exe"),
]


def _detectar_tesseract():
    """Busca el ejecutable de Tesseract. Prioriza la instalación OFICIAL
    dedicada (carpeta "Tesseract-OCR" del instalador de UB-Mannheim) por
    encima de cualquier otra copia de "tesseract" que aparezca suelta en
    el PATH — algunas herramientas (como PDF24) traen su propio Tesseract
    empaquetado con datos de idioma más livianos/menos precisos para no
    inflar su instalador, y si esa copia queda primero en el PATH, el bot
    la usaría por error en vez de la instalación completa y más precisa.

    Orden de búsqueda:
      1) Carpetas oficiales conocidas (instalador de UB-Mannheim).
      2) Si no está ahí, lo que haya en el PATH del sistema (cubre otras
         instalaciones válidas, aunque no sea en la ruta de siempre).
    """
    for candidata in RUTAS_TESSERACT_CONOCIDAS:
        if candidata and os.path.isfile(candidata):
            return candidata
    ruta = shutil.which("tesseract")
    if ruta:
        return ruta
    return None


MOTOR_OCR = None  # "tesseract" | "easyocr" | None

try:
    import pytesseract
    from PIL import Image, ImageOps, ImageFilter
    _ruta_tesseract = _detectar_tesseract()
    if _ruta_tesseract:
        MOTOR_OCR = "tesseract"
        # Si se encontró por fuera del PATH, se le indica a pytesseract
        # exactamente dónde está el .exe (no depende del PATH para nada).
        pytesseract.pytesseract.tesseract_cmd = _ruta_tesseract
    OCR_DISPONIBLE = True
except Exception:
    OCR_DISPONIBLE = False

_easyocr_reader = None


def _obtener_lector_easyocr():
    """Crea (una sola vez) el lector de EasyOCR. Se importa aquí, no al
    inicio del archivo, porque es una dependencia pesada (PyTorch) y solo
    hace falta cargarla si de verdad no hay Tesseract instalado."""
    global _easyocr_reader
    if _easyocr_reader is not None:
        return _easyocr_reader
    try:
        import easyocr
        _easyocr_reader = easyocr.Reader(["es", "en"], gpu=False, verbose=False)
    except Exception:
        _easyocr_reader = False  # marca de "no disponible" para no reintentar
    return _easyocr_reader


def _elegir_motor_ocr():
    """Decide qué motor usar, con EasyOCR como respaldo si Tesseract no
    está instalado en el sistema."""
    global MOTOR_OCR
    if MOTOR_OCR == "tesseract":
        return "tesseract"
    if MOTOR_OCR == "easyocr":
        return "easyocr"
    # Todavía no se sabe: probar EasyOCR como respaldo
    lector = _obtener_lector_easyocr()
    if lector:
        MOTOR_OCR = "easyocr"
        return "easyocr"
    return None


# ─────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────

# Umbral de caracteres nativos por debajo del cual se considera que la
# página es "escaneada" (imagen) y por lo tanto necesita OCR.
UMBRAL_TEXTO_NATIVO = 40


def _texto_nativo_es_confiable(texto: str) -> bool:
    """Algunos PDF traen texto nativo TÉCNICAMENTE presente pero
    ilegible: la app que los generó embebió una fuente sin un mapa
    Unicode válido (sin "ToUnicode CMap"), así que PyMuPDF extrae una
    fila de caracteres de control / símbolos sin sentido en vez del
    texto real, aunque visualmente la página se vea perfecta.

    Si se usa ese texto "nativo" tal cual para clasificar, cualquier
    coincidencia de palabra clave que ocurra ahí es pura casualidad —
    un caso real: una racha de caracteres de control coincidió por azar
    con la palabra clave de la zona MRZ ("<<<<"), marcando como CÉDULA
    una página que en realidad es un formulario administrativo con la
    fuente rota.

    Se considera "no confiable" si una fracción alta del texto son
    caracteres de control (fuera de espacio/salto de línea/tab) — eso
    es la huella típica de una fuente sin mapa Unicode. En ese caso, es
    mejor tratar la página como si NO tuviera texto nativo utilizable y
    pasarla por OCR (que lee la imagen renderizada, no le importa cómo
    esté armada la fuente por dentro)."""
    texto = texto.strip()
    if not texto:
        return True  # vacío no es "corrupto", simplemente no hay texto -> ya se maneja aparte
    control = sum(1 for c in texto if ord(c) < 32 and c not in "\n\r\t")
    return (control / len(texto)) < 0.05

# DPI de renderizado para OCR de página completa / recortes.
DPI_OCR = 220

# Con EasyOCR (CPU, sin Tesseract) cada imagen tarda mucho más mientras más
# grande sea. Se limita el lado más largo a este tamaño antes de pasarla al
# lector — sigue siendo suficiente para leer el texto, pero corta bastante
# el tiempo de proceso. Con Tesseract no hace falta, es rápido de por sí.
EASYOCR_LADO_MAXIMO = 1400

# Idiomas para tesseract, en orden de preferencia. Si "spa" no está
# instalado en el sistema, se cae a "eng" automáticamente.
IDIOMAS_OCR_PREFERIDOS = ["spa", "eng"]

ORDEN_SALIDA = ["SIRAS", "CEDULA", "TARJETA_PROPIEDAD"]

NOMBRES_LEGIBLES = {
    "SIRAS": "SIRAS",
    "CEDULA": "Cédula / Doc. migrante",
    "TARJETA_PROPIEDAD": "Tarjeta de propiedad",
}

# ─────────────────────────────────────────────────────────────
# Palabras clave por categoría (ya normalizadas: mayúsculas, sin tildes)
# Cada tupla es (palabra_clave, peso)
# ─────────────────────────────────────────────────────────────

KEYWORDS_SIRAS = [
    # Nota: se evita la palabra suelta "SIRAS" porque el FURIPS también la
    # menciona (campo "Número de radicado (SIRAS)"), lo que generaba falsos
    # positivos. Se usan frases completas del encabezado/formulario real.
    ("SISTEMA DE INFORMACION DE REPORTES DE", 5),
    ("ATENCION EN SALUD A VICTIMAS DE ACCIDENTES", 5),
    # Secciones numeradas del formulario (checklist: "Datos de la víctima",
    # "Tipo de ingreso", "Información del transporte", "Datos del accidente")
    ("DATOS DE LA VICTIMA DEL ACCIDENTE DE TRANSITO", 4),
    ("TIPO DE INGRESO A LOS SERVICIOS DE SALUD", 3),
    ("INFORMACION DEL TRANSPORTE AL PRIMER SITIO DE ATENCION", 3),
    ("DATOS DEL ACCIDENTE", 3),
    ("DATOS DEL CONDUCTOR DEL VEHICULO INVOLUCRADO", 3),
    ("INFORMACION DE LA PERSONA QUE REPORTA LA ATENCION", 3),
    ("CLASIFICACION DEL TRIAGE", 3),
    ("VICTIMA FUE TRASLADADA EN", 2),
]

KEYWORDS_CEDULA = [
    ("CEDULA DE CIUDADANIA", 5),
    ("IDENTIFICACION PERSONAL", 4),
    ("REGISTRADOR NACIONAL", 3),
    ("PERMISO POR PROTECCION TEMPORAL", 5),
    ("PROTECCION TEMPORAL", 4),
    ("MIGRACION COLOMBIA", 4),
    ("MIGRACION", 4),  # logo/marca de agua típico del permiso de migrante
    ("MINISTERIO DE RELACIONES EXTERIORES", 2),
    ("INDICE DERECHO", 2),
    ("REPUBLICA DE COLOMBIA", 1),  # aparece también en tarjeta -> peso bajo
    ("NACIONALIDAD", 2),
    ("APELLIDOS", 2),
    ("FECHA Y LUGAR DE EXPEDICION", 3),
    ("LUGAR DE NACIMIENTO", 3),  # reverso de la cédula (muy distintivo, no aparece en historias clínicas)
    ("ESTATURA", 2),  # aparece junto a "G.S. RH SEXO" en el reverso de la cédula
    ("CEDULA DE", 3),  # tolera OCR imperfecto de "CEDULA DE CIUDADANIA"
    # Zona legible por máquina (MRZ) de la parte posterior del PPT/cédula:
    # una fila larga de "<" es prácticamente exclusiva de este tipo de
    # documento (checklist: "código QR o zona legible por máquina").
    # Se exige una racha larga (8+) y no solo "<<<<" -- una racha corta
    # es más fácil que coincida por azar con basura de una fuente rota
    # (ver `_texto_nativo_es_confiable`, que ya cubre el caso principal;
    # esto es una segunda capa de seguridad).
    ("<<<<<<<<", 4),
    # Registro Civil de Nacimiento (documento de identidad de menores sin
    # cédula/tarjeta de identidad). Aún no tenemos un ejemplo real para
    # afinarlo, así que se usan los términos estándar de la Registraduría;
    # si falla en algún caso real, mandar un ejemplo para ajustar.
    ("REGISTRO CIVIL DE NACIMIENTO", 5),
    ("REGISTRO CIVIL", 4),
    ("NOTARIA", 2),
    ("INDICATIVO SERIAL", 3),
    ("NUIP", 3),  # Número Único de Identificación Personal (aparece en registro civil)
    ("SERIAL", 1),
]

KEYWORDS_TARJETA = [
    # ── Frente ──
    ("LICENCIA DE TRANSITO", 5),
    ("MINISTERIO DE TRANSPORTE", 3),
    ("PLACA", 2),
    ("CLASE DE VEHICULO", 3),
    ("CILINDRAJE", 2),
    ("NUMERO DE CHASIS", 2),
    ("NUMERO DE MOTOR", 2),
    ("TIPO CARROCERIA", 2),
    ("ORGANISMO DE TRANSITO", 2),
    # ── Reverso ── (antes solo "ORGANISMO DE TRANSITO" cubría algo del
    # reverso, y con puntaje 2 -- por debajo del umbral mínimo de 4, el
    # reverso casi nunca se identificaba solo. Estas frases se sacaron
    # del texto que Tesseract lee de verdad en un reverso real (no de
    # cómo se ve la tarjeta a simple vista), para asegurarse de que
    # coinciden con el OCR imperfecto y no con una transcripción ideal.
    ("RESTRICCION MOVILIDAD", 4),
    ("DECLARACION DE IMPORTACION", 4),
    ("LIMITACION A LA PROPIEDAD", 4),
    ("FECHA MATRICULA", 3),
    ("BLINDAJE", 2),
    ("POTENCIA", 1),  # palabra sola, peso bajo a propósito (puede aparecer en otros contextos)
]

# Si aparecen estas palabras, la página es un formulario administrativo
# digital (Anexo Técnico / FURIPS de autorización, referencia o
# contrarreferencia) que solo MENCIONA el tipo de documento del paciente
# como un campo más ("Tipo de documento: CC - CEDULA DE CIUDADANIA") —
# no es una cédula física escaneada, y NO debe contar como CEDULA.
EXCLUSIONES_CEDULA = [
    "ANEXO TECNICO",
    "ACTUALIZACION DE DATOS DE CONTACTO",
    "REFERENCIA Y CONTRARREFERENCIA",
    "NIT DE OBLIGADO A REPORTAR",
]

# Si aparecen estas palabras, la página es un reporte RUNT / consulta en
# línea (no la tarjeta física) y NO debe contar como TARJETA_PROPIEDAD.
EXCLUSIONES_TARJETA = [
    "CONSULTA AUTOMOTORES",
    "HISTORICO VEHICULAR",
    "GRAVAMENES A LA PROPIEDAD",
    "POLIZA SOAT",
    "@COPYRIGHT",
]

# Encabezados institucionales que dejan clarísimo que la página es un
# documento administrativo (orden médica, consulta RUNT en línea, etc.)
# que NUNCA va a ser SIRAS/Cédula/Tarjeta. Se usan para saltarse el
# análisis completo (9 lecturas, caro) cuando la pasada rápida ya viene
# vacía Y además trae uno de estos encabezados — así no se pierde tiempo
# escalando algo que ya sabemos que no es ninguno de los 3 documentos.
MARCADORES_PAGINA_IRRELEVANTE = [
    "ORDENES MEDICAS GENERADAS EN HISTORIAS CLINICAS",
    "ORDENES DE MEDICAMENTOS GENERADAS",
    "ORDENES DE PARACLINICOS GENERADAS",
    "HISTORIA CLINICA DE CONSULTA EXTERNA",
    "PLACA DEL VEHICULO:",  # firma típica de la consulta RUNT en línea
    "CONSULTA AUTOMOTORES",
    "DECLARACION DE RETIRO VOLUNTARIO",
    "EVOLUCION MEDICA",
]


def _es_pagina_irrelevante(texto_rapido: str) -> bool:
    """Detecta, a partir de una sola pasada de OCR (barata), si la página
    es claramente un documento administrativo que nunca va a ser
    SIRAS/Cédula/Tarjeta (orden médica, historia clínica, consulta RUNT
    en línea). Si es así, no vale la pena pagar el análisis completo."""
    norm = normalizar(texto_rapido)
    return any(marcador in norm for marcador in MARCADORES_PAGINA_IRRELEVANTE)

UMBRAL_PUNTAJE = 4  # puntaje mínimo para aceptar una categoría
UMBRAL_PUNTAJE_MITAD = 3  # umbral más permisivo al revisar mitades de una página combo


# ─────────────────────────────────────────────────────────────
# Utilidades de texto
# ─────────────────────────────────────────────────────────────

def normalizar(texto: str) -> str:
    """Mayúsculas, sin tildes, espacios colapsados."""
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.upper()
    texto = re.sub(r"\s+", " ", texto)
    return texto


def _puntaje(texto_norm: str, keywords) -> int:
    total = 0
    for palabra, peso in keywords:
        if palabra in texto_norm:
            total += peso
    return total


# ─────────────────────────────────────────────────────────────
# OCR
# ─────────────────────────────────────────────────────────────

_idioma_ocr_cache = None


def _idioma_ocr():
    """Detecta qué idiomas tiene instalados tesseract y arma el string
    de idioma a usar (ej: 'spa+eng'). Solo aplica al motor Tesseract."""
    global _idioma_ocr_cache
    if _idioma_ocr_cache is not None:
        return _idioma_ocr_cache
    if not OCR_DISPONIBLE:
        _idioma_ocr_cache = ""
        return _idioma_ocr_cache
    try:
        disponibles = set(pytesseract.get_languages(config=""))
    except Exception:
        disponibles = set()
    elegidos = [l for l in IDIOMAS_OCR_PREFERIDOS if l in disponibles]
    if not elegidos:
        elegidos = ["eng"] if "eng" in disponibles else list(disponibles)[:1]
    _idioma_ocr_cache = "+".join(elegidos) if elegidos else "eng"
    return _idioma_ocr_cache


def _preprocesar_para_tesseract(img):
    """Prepara la imagen antes de mandarla a Tesseract.

    A diferencia de EasyOCR (una red neuronal, más tolerante a fotos
    "crudas" de baja calidad), Tesseract es un motor más clásico que
    depende mucho de que la imagen tenga buen contraste y esté "limpia".
    Las cédulas viejas o amarillentas fotografiadas pierden bastante
    contraste, y sin este paso Tesseract puede leer notablemente peor
    (esto explica buena parte de por qué algunos casos fallaban en unas
    máquinas y en otras no: pequeñas diferencias de nitidez/color en el
    escaneo original hacían que, sin preprocesar, el resultado quedara
    al filo de la moneda).

    Pasos: pasar a escala de grises y estirar el contraste (autocontrast,
    aprovecha todo el rango de grises en vez de quedarse en un rango
    apagado). Se probó sumarle además un filtro de nitidez (SHARPEN), pero
    se descartó: en algunos casos generaba confusiones de letras (ej. una
    "C" leída como "G") que no aparecían con la imagen sin afinar — el
    autocontraste solo ya da la mejora real sin ese efecto secundario.
    """
    try:
        gris = img.convert("L")
        gris = ImageOps.autocontrast(gris, cutoff=1)
        return gris
    except Exception:
        return img


def _ocr_imagen(img) -> str:
    motor = _elegir_motor_ocr()
    if motor == "tesseract":
        img_prep = _preprocesar_para_tesseract(img)
        # 3 intentos con una pausa cortita entre cada uno: los fallos de
        # Tesseract bajo carga suelen ser transitorios (varios procesos
        # corriendo a la vez, antivirus interceptando el subproceso,
        # etc.), no permanentes — con un respiro de por medio casi
        # siempre se resuelven solos.
        for intento in range(3):
            try:
                return pytesseract.image_to_string(img_prep, lang=_idioma_ocr())
            except Exception:
                if intento < 2:
                    time.sleep(0.4 * (intento + 1))
        try:
            return pytesseract.image_to_string(img_prep)
        except Exception as e:
            # No se traga el error en silencio: se avisa por consola para
            # poder diagnosticar si una página salió "vacía" por esto y
            # no porque de verdad no tuviera nada.
            print(f"[clasificador] AVISO: fallo de OCR en una página tras 4 intentos, "
                  f"se sigue sin ese texto (no se detiene el proceso). Detalle: {e}")
            return ""
    elif motor == "easyocr":
        try:
            import numpy as np
            lector = _obtener_lector_easyocr()
            # Achicar la imagen si es muy grande: EasyOCR en CPU tarda
            # bastante más mientras más pixeles tenga que procesar, y no
            # gana casi nada de precisión pasado cierto tamaño.
            w, h = img.size
            lado_mayor = max(w, h)
            if lado_mayor > EASYOCR_LADO_MAXIMO:
                factor = EASYOCR_LADO_MAXIMO / lado_mayor
                img = img.resize((int(w * factor), int(h * factor)))
            resultado = lector.readtext(
                np.array(img), detail=0, paragraph=True, decoder="greedy"
            )
            return "\n".join(resultado)
        except Exception:
            return ""
    return ""


def _texto_da_una_categoria_clara(texto: str, margen: int = 8) -> bool:
    """True si el texto acumulado hasta ahora ya resuelve a UNA sola
    categoría con bastante ventaja sobre las demás — señal de que seguir
    haciendo más pasadas de OCR sobre la misma imagen no va a cambiar el
    resultado. Se usa para cortar `_ocr_texto_robusto` antes de tiempo en
    el caso más común (una página, un solo documento, lectura clara)."""
    _, puntajes = clasificar_pagina(texto)
    ganador = max(puntajes, key=puntajes.get)
    valor_ganador = puntajes[ganador]
    if valor_ganador < UMBRAL_PUNTAJE + margen:
        return False
    otros = [p for cat, p in puntajes.items() if cat != ganador]
    return valor_ganador >= max(otros, default=0) + margen


def _ocr_texto_robusto(img, texto_base: str = None) -> str:
    """OCR de una imagen (página completa o una mitad recortada) haciendo
    varias pasadas: la imagen completa + 4 cuadrantes + 4 mitades (con
    solape), agrandados 2x. Con Tesseract esto mejora mucho la lectura
    cuando el escaneo trae varias fotos/documentos juntos o está borroso/
    descolorido. Con EasyOCR no hace falta: su detector ya encuentra
    todas las regiones de texto en una sola pasada.

    `texto_base`: si el llamador YA hizo la pasada de la imagen completa
    antes de decidir escalar a este análisis más a fondo (que es
    justamente lo que pasa en `clasificar_pdf`), se le pasa ese texto en
    vez de volver a correr Tesseract sobre la misma imagen completa —
    ahorra una pasada de OCR completa cada vez que se llama.

    Corte temprano: después de la pasada completa + los 4 cuadrantes, si
    ya hay una sola categoría clara con margen de sobra (ver
    `_texto_da_una_categoria_clara`), se salta las 4 mitades restantes —
    en la gran mayoría de páginas (un solo documento, bien legible) esas
    4 pasadas extra no cambian nada, porque los cuadrantes ya cubren toda
    la imagen con solape. Las mitades quedan solo para el caso más raro
    de texto que cruza justo el centro de la imagen entre 2 cuadrantes;
    si la página no resulta clara con los cuadrantes, se siguen haciendo
    igual que antes — no se pierde precisión en los casos ambiguos, que
    son los que de verdad importan."""
    motor = _elegir_motor_ocr()
    if motor is None:
        return ""
    if motor == "easyocr":
        return _ocr_imagen(img)

    textos = [texto_base if texto_base is not None else _ocr_imagen(img)]

    w, h = img.size
    ov = 0.12  # solape entre recortes para no cortar palabras a la mitad
    cuadrantes = {
        "TL": (0, 0, int(w * (0.5 + ov)), int(h * (0.5 + ov))),
        "TR": (int(w * (0.5 - ov)), 0, w, int(h * (0.5 + ov))),
        "BL": (0, int(h * (0.5 - ov)), int(w * (0.5 + ov)), h),
        "BR": (int(w * (0.5 - ov)), int(h * (0.5 - ov)), w, h),
    }
    mitades = {
        "TOP": (0, 0, w, int(h * (0.5 + ov))),
        "BOTTOM": (0, int(h * (0.5 - ov)), w, h),
        "LEFT": (0, 0, int(w * (0.5 + ov)), h),
        "RIGHT": (int(w * (0.5 - ov)), 0, w, h),
    }

    def _pasada(caja):
        try:
            recorte = img.crop(caja)
            recorte = recorte.resize((recorte.width * 2, recorte.height * 2))
            textos.append(_ocr_imagen(recorte))
        except Exception:
            pass

    for caja in cuadrantes.values():
        _pasada(caja)

    if _texto_da_una_categoria_clara("\n".join(textos)):
        return "\n".join(textos)

    for caja in mitades.values():
        _pasada(caja)

    return "\n".join(textos)


def _renderizar_pagina(page):
    """Renderiza la página completa a una imagen PIL (una sola vez), para
    poder reutilizarla tanto en el OCR normal como en la revisión de
    mitades (separación de documentos pegados)."""
    try:
        pix = page.get_pixmap(dpi=DPI_OCR)
        return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# Extracción de texto por página (nativo + OCR si hace falta)
# ─────────────────────────────────────────────────────────────

@dataclass
class PaginaClasificada:
    indice: int              # 0-based
    numero: int               # 1-based (para mostrar al usuario)
    texto: str = ""
    ocr_usado: bool = False
    categorias: set = field(default_factory=set)
    puntajes: dict = field(default_factory=dict)
    # Si la página trae 2 o más documentos pegados en una sola foto (ej.
    # cédula + tarjeta de propiedad, acomodados en fila o en cuadrícula),
    # se detecta y se separa en varias páginas independientes en el PDF
    # de salida — ver `_revisar_documentos_pegados`.
    dividir: bool = False
    partes: list = field(default_factory=list)  # [{"categorias":set,"rect":(x0,y0,x1,y1) 0..1,"etiqueta":str}]
    diagnostico_separacion: str = ""  # detalle de qué pasó al intentar separar (para depurar)


def extraer_texto_pagina(doc, indice: int):
    """Extrae el texto de una página: nativo si el PDF lo trae, o por OCR
    si es una página escaneada (misma lógica de dos niveles que usa
    `clasificar_pdf` internamente; esta función queda disponible para
    clasificar una sola página suelta si hiciera falta).

    El OCR se hace en dos niveles para no gastar tiempo de más: primero
    una sola pasada rápida (página completa, sin recortes). Si esa pasada
    ya resuelve la página a UNA sola categoría clara, se usa ese
    resultado tal cual. Solo si el resultado es ambiguo (no se identificó
    nada, o se identificaron 2 categorías a la vez — posible documento
    pegado) se hace el análisis completo con recortes, que es más lento
    pero más preciso.
    """
    page = doc[indice]
    texto_nativo = page.get_text() or ""
    if len(texto_nativo.strip()) >= UMBRAL_TEXTO_NATIVO and _texto_nativo_es_confiable(texto_nativo):
        return texto_nativo, False, None

    img = _renderizar_pagina(page)
    if img is None:
        return texto_nativo.strip(), True, None

    texto_rapido = _ocr_imagen(img)
    categorias_rapidas, _ = clasificar_pagina(texto_rapido)

    if not categorias_rapidas and _es_pagina_irrelevante(texto_rapido):
        # Orden médica / historia clínica / consulta RUNT: nunca va a ser
        # ninguno de los 3 documentos, no vale la pena escalar.
        texto_total = (texto_nativo + "\n" + texto_rapido).strip()
        return texto_total, True, img

    posible_combo = _hueco_entre_documentos(img) is not None

    if len(categorias_rapidas) == 1 and not posible_combo:
        texto_total = (texto_nativo + "\n" + texto_rapido).strip()
        return texto_total, True, img

    # Ambiguo (nada claro, o posible combo de 2 documentos): analisis completo
    texto_completo = _ocr_texto_robusto(img, texto_base=texto_rapido)
    texto_total = (texto_nativo + "\n" + texto_completo).strip()
    return texto_total, True, img


def clasificar_pagina(texto: str, umbral: int = None):
    """Devuelve (set_categorias, dict_puntajes) para el texto de una
    página. `umbral` permite bajar el mínimo requerido (se usa un umbral
    más permisivo al revisar las mitades de una página que ya se sabe que
    trae 2 documentos pegados, porque cada mitad da menos texto/contexto
    al OCR que la página completa)."""
    if umbral is None:
        umbral = UMBRAL_PUNTAJE
    norm = normalizar(texto)
    puntajes = {
        "SIRAS": _puntaje(norm, KEYWORDS_SIRAS),
        "CEDULA": _puntaje(norm, KEYWORDS_CEDULA),
        "TARJETA_PROPIEDAD": _puntaje(norm, KEYWORDS_TARJETA),
    }

    excluir_tarjeta = any(ex in norm for ex in EXCLUSIONES_TARJETA)
    if excluir_tarjeta:
        puntajes["TARJETA_PROPIEDAD"] = 0

    excluir_cedula = any(ex in norm for ex in EXCLUSIONES_CEDULA)
    if excluir_cedula:
        puntajes["CEDULA"] = 0

    categorias = {
        cat for cat, p in puntajes.items() if p >= umbral
    }
    return categorias, puntajes


def _bloques_de_contenido(mask):
    """Devuelve lista de (inicio, fin) de corridas continuas de True en
    `mask` (usada para encontrar bloques de contenido real, fila por
    fila)."""
    bloques = []
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            bloques.append((i, j))
            i = j
        else:
            i += 1
    return bloques


def _hueco_en_densidad(densidad, con_diagnostico=False):
    """Núcleo compartido: busca el hueco (franja sin contenido) más
    grande entre dos bloques de contenido real, a partir de un arreglo
    1D de densidad de "tinta" ya calculado — por FILA para separar
    arriba/abajo, o por COLUMNA para separar izquierda/derecha (ver
    `_hueco_entre_documentos` y `_hueco_vertical_entre_documentos`, que
    son los únicos que arman `densidad` y llaman a esto).

    A diferencia de un simple "punto medio de todo lo que no es blanco",
    esto ignora artefactos sueltos (una línea o borde de un par de
    píxeles cerca de un extremo puede arruinar el cálculo) exigiendo que
    cada bloque tenga un tamaño mínimo.

    Devuelve (inicio_total, fin_bloque_A, inicio_bloque_B, fin_total) en
    índices del arreglo `densidad`. Devuelve None si no se encuentra un
    hueco claro entre dos bloques.
    """
    h = len(densidad)
    es_contenido = densidad > 0.015

    minimo_bloque = max(3, int(h * 0.01))  # ignora artefactos/líneas sueltas
    todos_bloques = _bloques_de_contenido(es_contenido)
    bloques = [(a, b) for a, b in todos_bloques if (b - a) >= minimo_bloque]

    if len(bloques) < 2:
        diag = (f"largo={h} bloques_brutos={len(todos_bloques)} "
                f"bloques_validos(>=1%)={len(bloques)} tamanos={[b - a for a, b in bloques]} "
                f"-> menos de 2 bloques, no hay como encontrar un hueco entre dos")
        return (None, diag) if con_diagnostico else None

    mejor_tam, mejor_ini, mejor_fin = 0, None, None
    for k in range(len(bloques) - 1):
        ini_hueco = bloques[k][1]
        fin_hueco = bloques[k + 1][0]
        tam = fin_hueco - ini_hueco
        centro_rel = (ini_hueco + fin_hueco) / 2 / h
        # el hueco tiene que estar en una zona razonablemente central
        # (ni pegado a un extremo del todo)
        if tam > mejor_tam and 0.1 < centro_rel < 0.9:
            mejor_tam, mejor_ini, mejor_fin = tam, ini_hueco, fin_hueco

    if mejor_ini is not None:
        y0, y1 = bloques[0][0], bloques[-1][1]
        resultado = (y0, mejor_ini, mejor_fin, y1)
        if con_diagnostico:
            diag = (f"bloques_validos={[(a, b, b - a) for a, b in bloques]} "
                    f"hueco_elegido=({mejor_ini},{mejor_fin},tam={mejor_tam})")
            return resultado, diag
        return resultado

    diag = (f"bloques_validos={[(a, b, b - a) for a, b in bloques]} "
            f"-> ningun hueco entre bloques quedo en zona central (0.1<centro<0.9)")
    return (None, diag) if con_diagnostico else None


def _hueco_entre_documentos(img, con_diagnostico=False):
    """Busca el hueco HORIZONTAL (franja en blanco de arriba a abajo)
    más grande entre dos bloques de contenido real en la imagen, para
    partir ahí entre dos documentos pegados uno ENCIMA del otro (ej.
    tarjeta arriba, cédula abajo).

    Devuelve (y0, fin_bloque_superior, inicio_bloque_inferior, y1) en
    píxeles, donde y0/y1 son el borde superior/inferior de TODO el
    contenido real (sin el margen en blanco de la hoja). Devuelve None
    si no se encuentra un hueco claro.

    Si `con_diagnostico=True`, devuelve (resultado, texto_diagnostico).
    """
    try:
        import numpy as np
        arr = np.array(img.convert("L"))
        densidad = (arr < 200).mean(axis=1)  # fracción de píxeles "con tinta" por FILA
        return _hueco_en_densidad(densidad, con_diagnostico)
    except Exception as e:
        diag = f"excepcion durante el analisis de huecos: {e}"
        return (None, diag) if con_diagnostico else None


def _hueco_vertical_entre_documentos(img, con_diagnostico=False):
    """Igual que `_hueco_entre_documentos`, pero busca el hueco VERTICAL
    (franja en blanco de izquierda a derecha) — para separar documentos
    puestos uno AL LADO del otro en la misma fila (ej. tarjeta de
    propiedad a la izquierda, cédula a la derecha), que es el caso que
    `_hueco_entre_documentos` por sí solo no detecta porque solo corta
    horizontalmente.

    Devuelve (x0, fin_bloque_izquierdo, inicio_bloque_derecho, x1) en
    píxeles. Se usa como segundo corte, aplicado solo DENTRO de una
    franja que ya salió de un primer corte horizontal (ver
    `_revisar_documentos_pegados`) — no sobre la página completa — para
    no confundir el espacio entre 2 columnas de texto de un mismo
    documento con un hueco real entre 2 documentos distintos.
    """
    try:
        import numpy as np
        arr = np.array(img.convert("L"))
        densidad = (arr < 200).mean(axis=0)  # fracción de píxeles "con tinta" por COLUMNA
        return _hueco_en_densidad(densidad, con_diagnostico)
    except Exception as e:
        diag = f"excepcion durante el analisis de huecos (vertical): {e}"
        return (None, diag) if con_diagnostico else None


def _reclasificar_recorte(img_pagina, rect_px, umbral=UMBRAL_PUNTAJE_MITAD):
    """OCR + clasificación de un recorte de la página (en píxeles de la
    imagen ya renderizada). Devuelve (texto, categorias, puntajes)."""
    w, h = img_pagina.size
    x0, y0, x1, y1 = rect_px
    recorte = img_pagina.crop((max(0, x0), max(0, y0), min(w, x1), min(h, y1)))
    texto = _ocr_texto_robusto(recorte)
    categorias, puntajes = clasificar_pagina(texto, umbral=umbral)
    return texto, categorias, puntajes


def _asignar_dos_mitades(cat_a, cat_b, pts_a, pts_b, categorias_totales):
    """Decide cómo repartir `categorias_totales` entre 2 mitades (sirve
    igual para arriba/abajo que para izquierda/derecha — es la misma
    decisión). 3 niveles, del más al menos confiable:

      nivel 1: cada mitad se identifica sola, y son distintas entre sí.
      nivel 2 ("por eliminación"): una mitad se identifica sola; a la
        otra se le asigna lo que sobra, SI sobra una sola categoría.
      nivel 3 (respaldo): se compara el puntaje crudo de cada categoría
        en cada mitad, sin exigir el umbral mínimo — gana la mitad con
        más puntaje, siempre que entre las dos cubran TODAS las
        categorías de la página y ninguna quede empatada.

    Devuelve (ok: bool, cat_a_final: set, cat_b_final: set, motivo: str).
    """
    # Caso especial (revisado ANTES que todo lo demás): las 2 mitades
    # traen exactamente el mismo combo de 2+ categorías a la vez -- por
    # ejemplo, una cuadrícula 2x2 donde CADA fila trae los 2 mismos
    # tipos de documento lado a lado. Acá NO hay que forzar un reparto a
    # ciegas (el nivel 3 de más abajo, comparando puntaje crudo, se
    # equivocaría asignando cada categoría entera a una sola mitad,
    # perdiendo la otra mitad de cada documento). Se acepta el corte
    # geométrico -- ya se sabe que arriba y abajo son físicamente
    # distintos -- pero cada mitad se queda con el combo COMPLETO, para
    # que el llamador intente un segundo corte (perpendicular) dentro de
    # CADA una por separado.
    if cat_a and cat_b and cat_a == cat_b and len(cat_a) >= 2:
        return True, set(cat_a), set(cat_b), "ambas mitades con el mismo combo ambiguo -> se dejan completas para partir cada una por separado"

    if cat_a and cat_b and cat_a != cat_b:
        return True, set(cat_a), set(cat_b), "nivel1 OK"

    if cat_a and not cat_b:
        restante = categorias_totales - cat_a
        if len(restante) == 1:
            return True, set(cat_a), set(restante), "nivel2 OK (B por eliminacion)"
    if cat_b and not cat_a:
        restante = categorias_totales - cat_b
        if len(restante) == 1:
            return True, set(restante), set(cat_b), "nivel2 OK (A por eliminacion)"

    if len(categorias_totales) >= 2:
        asignacion_a, asignacion_b = set(), set()
        empatados = False
        for cat in categorias_totales:
            pa = pts_a.get(cat, 0)
            pb = pts_b.get(cat, 0)
            if pa == pb:
                empatados = True
                continue
            (asignacion_a if pa > pb else asignacion_b).add(cat)
        cubre_todo = (asignacion_a | asignacion_b) == categorias_totales
        if not empatados and cubre_todo and asignacion_a and asignacion_b and asignacion_a != asignacion_b:
            return True, asignacion_a, asignacion_b, "nivel3 OK"
        return False, set(), set(), (f"NINGUN nivel aplico (empatados={empatados} "
                                      f"cubre_todo={cubre_todo} asig_a={asignacion_a} asig_b={asignacion_b})")

    return False, set(), set(), "menos de 2 categorias para repartir"


def _intentar_corte(img_pagina, rect_px, categorias_region, orientacion):
    """Intenta UN corte (horizontal o vertical) dentro de `rect_px`
    (píxeles de la imagen COMPLETA de la página) y, si encuentra un
    hueco y logra repartir `categorias_region` en 2 grupos válidos con
    `_asignar_dos_mitades`, devuelve ((rect_a, cat_a, rect_b, cat_b), diagnostico).
    Si no, devuelve (None, diagnostico).

    `rect_a`/`rect_b` van en el mismo orden que lee una persona: para
    horizontal, A=arriba/B=abajo; para vertical, A=izquierda/B=derecha.
    """
    x0, y0, x1, y1 = rect_px
    franja = img_pagina.crop((x0, y0, x1, y1))
    buscar_hueco = _hueco_entre_documentos if orientacion == "horizontal" else _hueco_vertical_entre_documentos
    hueco, diag_hueco = buscar_hueco(franja, con_diagnostico=True)
    if hueco is None:
        return None, f"[{orientacion}] {diag_hueco}"

    a0, fin_a, ini_b, a1 = hueco
    margen = max(5, int((ini_b - fin_a) * 0.15))
    if orientacion == "horizontal":
        rect_a = (x0, y0 + max(0, a0 - margen), x1, y0 + min(y1 - y0, fin_a + margen))
        rect_b = (x0, y0 + max(0, ini_b - margen), x1, y0 + min(y1 - y0, a1 + margen))
    else:
        rect_a = (x0 + max(0, a0 - margen), y0, x0 + min(x1 - x0, fin_a + margen), y1)
        rect_b = (x0 + max(0, ini_b - margen), y0, x0 + min(x1 - x0, a1 + margen), y1)

    _, cat_a, pts_a = _reclasificar_recorte(img_pagina, rect_a)
    _, cat_b, pts_b = _reclasificar_recorte(img_pagina, rect_b)
    ok, cat_a_final, cat_b_final, motivo = _asignar_dos_mitades(cat_a, cat_b, pts_a, pts_b, categorias_region)
    diag = f"[{orientacion}] cat_a={cat_a} pts_a={pts_a} cat_b={cat_b} pts_b={pts_b} -> {motivo}"
    if not ok:
        return None, diag
    return (rect_a, cat_a_final, rect_b, cat_b_final), diag


def _revisar_documentos_pegados(img, categorias_pagina):
    """Revisa si la página trae dos o más documentos pegados en la misma
    foto, y los separa en tantas partes como haga falta para que CADA
    parte del PDF final tenga un solo documento — nunca un documento
    partido a la mitad entre 2 páginas, y nunca 2 documentos distintos
    metidos en la misma página de salida.

    Los documentos pegados pueden venir acomodados de 3 formas, y las 3
    se cubren:

      • Uno ENCIMA del otro (ej. tarjeta arriba, cédula abajo) -> se
        resuelve con un corte HORIZONTAL.
      • Uno AL LADO del otro en la misma fila (ej. tarjeta a la
        izquierda, cédula a la derecha, sin nada arriba/abajo) -> un
        corte horizontal no encuentra ningún hueco (es una sola franja
        de contenido de arriba a abajo), así que se prueba un corte
        VERTICAL en su lugar.
      • Una CUADRÍCULA 2x2 (tarjeta y cédula arriba, sus reversos abajo)
        -> el corte horizontal separa las 2 filas, y si alguna fila
        TODAVÍA tiene 2+ categorías a la vez (señal de que esa fila
        trae 2 documentos lado a lado), se prueba un segundo corte,
        esta vez vertical, SOLO dentro de esa franja.

    Si ese segundo corte no logra separar con confianza una franja que
    quedó con 2+ categorías, esa franja se deja completa (mejor eso, con
    una nota en el diagnóstico, que partir a ciegas y arriesgarse a
    cortar un documento por la mitad).

    Devuelve (dividir: bool, partes: list[dict], diagnostico: str).
    Cada parte es {"categorias": set, "rect": (x0,y0,x1,y1) en fracción
    0..1 de la página COMPLETA, "etiqueta": "sup"|"inf"|"izq"|"der"|"sup-izq"|...}.
    Si dividir=False, partes queda vacía y la página se trata como
    siempre (un solo documento, usa p.categorias tal cual).
    """
    if img is None:
        return False, [], "sin imagen"
    if _elegir_motor_ocr() != "tesseract":
        return False, [], "motor no es tesseract"
    if not categorias_pagina:
        return False, [], "pagina sin ninguna categoria detectada"

    w, h = img.size
    rect_completo = (0, 0, w, h)

    # Primer nivel: se prueba horizontal primero (el caso más común,
    # documentos apilados), y si no resuelve nada, vertical (documentos
    # lado a lado sin nada arriba/abajo).
    resultado, diag1 = _intentar_corte(img, rect_completo, categorias_pagina, "horizontal")
    orientacion1 = "horizontal"
    if resultado is None:
        resultado, diag_v = _intentar_corte(img, rect_completo, categorias_pagina, "vertical")
        orientacion1 = "vertical"
        diag1 = diag1 + " | " + diag_v

    if resultado is None:
        return False, [], diag1

    rect_a, cat_a, rect_b, cat_b = resultado
    etiqueta_a, etiqueta_b = ("sup", "inf") if orientacion1 == "horizontal" else ("izq", "der")
    orientacion2 = "vertical" if orientacion1 == "horizontal" else "horizontal"

    partes = []
    detalle_nivel2 = []
    for etiqueta, cats, rect_px in ((etiqueta_a, cat_a, rect_a), (etiqueta_b, cat_b, rect_b)):
        if len(cats) <= 1:
            x0p, y0p, x1p, y1p = rect_px
            partes.append({"categorias": set(cats), "rect": (x0p / w, y0p / h, x1p / w, y1p / h), "etiqueta": etiqueta})
            continue

        # Esta franja todavía trae 2+ documentos -- se prueba la
        # orientación CONTRARIA a la del primer corte, solo dentro de
        # esta franja (nunca sobre la página completa de nuevo).
        resultado2, diag2 = _intentar_corte(img, rect_px, cats, orientacion2)
        detalle_nivel2.append(f"{etiqueta}: {diag2}")
        if resultado2 is None:
            x0p, y0p, x1p, y1p = rect_px
            partes.append({"categorias": set(cats), "rect": (x0p / w, y0p / h, x1p / w, y1p / h), "etiqueta": etiqueta})
            continue

        rect_a2, cat_a2, rect_b2, cat_b2 = resultado2
        sub_a, sub_b = ("sup", "inf") if orientacion2 == "horizontal" else ("izq", "der")
        for sub_etq, sub_cats, sub_rect in ((f"{etiqueta}-{sub_a}", cat_a2, rect_a2), (f"{etiqueta}-{sub_b}", cat_b2, rect_b2)):
            x0p, y0p, x1p, y1p = sub_rect
            partes.append({"categorias": set(sub_cats), "rect": (x0p / w, y0p / h, x1p / w, y1p / h), "etiqueta": sub_etq})

    diag_final = f"corte 1 [{orientacion1}]: {diag1}"
    if detalle_nivel2:
        diag_final += " | corte 2 dentro de la franja: " + " || ".join(detalle_nivel2)
    return True, partes, diag_final


HILOS_OCR = min(2, max(1, (os.cpu_count() or 4)))


def clasificar_pdf(ruta_pdf: str, debe_detener=None):
    """Clasifica todas las páginas de un PDF.

    Devuelve una lista de PaginaClasificada (una por página).

    `debe_detener` es una función opcional (sin argumentos, devuelve
    bool) que se revisa PERIÓDICAMENTE — entre página y página, no solo
    al principio — para poder cortar el análisis de un archivo grande a
    la mitad cuando el usuario le da "Detener". Sin esto, un PDF de 60-90
    páginas que necesita OCR completo puede tardar 20-90+ minutos en
    terminar por su cuenta, y el botón "Detener" quedaría esperando todo
    ese tiempo sin poder hacer nada — porque antes solo se revisaba entre
    ARCHIVOS completos, nunca entre páginas de un mismo archivo grande.
    Las páginas que no alcanzaron a analizarse quedan con lo que ya se
    tenía (texto nativo si lo había, sin OCR) — no se pierde el archivo
    completo, solo se deja de invertir más tiempo en él.

    El trabajo con PyMuPDF (abrir el PDF, sacar texto nativo, renderizar
    la imagen de cada página) se hace primero y en orden — PyMuPDF no es
    seguro para usar en paralelo sobre el mismo documento. Una vez que ya
    se tienen las imágenes en memoria, el OCR de cada página (que es lo
    que realmente tarda) se reparte entre varios hilos: cada llamada a
    Tesseract es un proceso aparte del sistema operativo, así que se
    pueden correr varias a la vez aprovechando los núcleos del CPU — esto
    es lo que más importa para lotes grandes (100-300+ casos).

    Con EasyOCR NO se paraleliza a propósito: ya es pesado para el CPU/
    memoria de por sí, y correr varias instancias al mismo tiempo compite
    por los mismos recursos en vez de ayudar.
    """
    def _detener():
        return bool(debe_detener and debe_detener())

    doc = fitz.open(ruta_pdf)
    n = len(doc)

    # Fase 1 (secuencial): texto nativo + render de imagen si hace falta.
    # Se cierra el documento apenas termina esta fase; todo lo que sigue
    # trabaja sobre texto/imágenes que ya están en memoria.
    pendientes = []  # (indice, texto_nativo, img_o_None)
    for i in range(n):
        page = doc[i]
        texto_nativo = page.get_text() or ""
        if len(texto_nativo.strip()) >= UMBRAL_TEXTO_NATIVO and _texto_nativo_es_confiable(texto_nativo):
            pendientes.append((i, texto_nativo, None))
        else:
            img = _renderizar_pagina(page)
            pendientes.append((i, texto_nativo, img))
    doc.close()

    def _procesar(item):
        i, texto_nativo, img = item
        if not _texto_nativo_es_confiable(texto_nativo):
            # Fuente rota / sin mapa Unicode -- ese texto es basura, no
            # se debe mezclar ni con el resultado del OCR (ver
            # `_texto_nativo_es_confiable`).
            texto_nativo = ""
        if img is None:
            return i, texto_nativo, False, None
        texto_rapido = _ocr_imagen(img)
        categorias_rapidas, _ = clasificar_pagina(texto_rapido)

        if not categorias_rapidas and _es_pagina_irrelevante(texto_rapido):
            # Orden médica / historia clínica / consulta RUNT: nunca va a
            # ser ninguno de los 3 documentos. Esto evita ~15-30s de
            # análisis completo desperdiciados por página en archivos
            # grandes que traen varias de estas páginas administrativas.
            texto_total = (texto_nativo + "\n" + texto_rapido).strip()
            return i, texto_total, True, img

        # Chequeo barato (sin OCR, solo análisis de píxeles) para saber si
        # hay señales de que la foto trae dos documentos pegados (un hueco
        # real entre dos bloques de contenido). Si lo hay, vale la pena
        # escalar al análisis completo aunque la pasada rápida ya haya
        # encontrado una sola categoría — porque esa categoría puede ser
        # solo la del documento con el texto más fuerte/visible, dejando
        # el segundo documento sin detectar.
        posible_combo = _hueco_entre_documentos(img) is not None

        if len(categorias_rapidas) == 1 and not posible_combo:
            texto_total = (texto_nativo + "\n" + texto_rapido).strip()
        else:
            texto_completo = _ocr_texto_robusto(img, texto_base=texto_rapido)
            texto_total = (texto_nativo + "\n" + texto_completo).strip()
        return i, texto_total, True, img

    # Fase 2 (en paralelo si hay Tesseract y más de 1 página con OCR).
    # Se usa submit+as_completed (no ex.map) justamente para poder
    # revisar `debe_detener()` según van terminando páginas, y cancelar
    # las que todavía no habían arrancado -- ex.map no permite cortar a
    # la mitad de forma limpia.
    paginas_con_ocr = sum(1 for _, _, img in pendientes if img is not None)
    usar_hilos = _elegir_motor_ocr() == "tesseract" and paginas_con_ocr > 1

    resultados = {}
    if usar_hilos:
        with ThreadPoolExecutor(max_workers=HILOS_OCR) as ex:
            futuros = {ex.submit(_procesar, item): item[0] for item in pendientes}
            detenido = False
            for futuro in as_completed(futuros):
                try:
                    i, texto, uso_ocr, img = futuro.result()
                except CancelledError:
                    continue  # se completa más abajo con el resultado más barato disponible
                resultados[i] = (texto, uso_ocr, img)
                if detenido:
                    continue
                if _detener():
                    detenido = True
                    for f in futuros:
                        f.cancel()  # las que ya estaban corriendo igual terminan solas
    else:
        detenido = False
        for item in pendientes:
            if detenido:
                break
            i, texto, uso_ocr, img = _procesar(item)
            resultados[i] = (texto, uso_ocr, img)
            if _detener():
                detenido = True

    # Cualquier página que se haya quedado sin procesar (cancelada o
    # nunca alcanzada por la detención) se completa con lo más barato
    # que se tenga -- su texto nativo si lo había, sin OCR -- para que
    # la Fase 3 no reviente por falta de datos, y el archivo quede con
    # el resto de páginas ya analizadas en vez de perderse entero.
    for i, texto_nativo, img in pendientes:
        if i not in resultados:
            texto_nativo_ok = texto_nativo if _texto_nativo_es_confiable(texto_nativo) else ""
            resultados[i] = (texto_nativo_ok, False, None)

    # Fase 3 (secuencial): comparar palabras clave (barato, siempre se
    # hace) y revisar documentos pegados (caro -- hace OCR extra -- solo
    # si no se pidió detener; si ya se pidió, se deja esa página tal cual
    # sin el segundo análisis, para no seguir gastando tiempo).
    resultado = []
    detenido_fase3 = False
    for i in range(n):
        texto, uso_ocr, img = resultados[i]
        categorias, puntajes = clasificar_pagina(texto)

        if not detenido_fase3 and _detener():
            detenido_fase3 = True
        if detenido_fase3:
            dividir, partes, diag = False, [], "detencion solicitada -- se omite el analisis de documentos pegados"
        else:
            dividir, partes, diag = _revisar_documentos_pegados(img, categorias)

        resultado.append(
            PaginaClasificada(
                indice=i,
                numero=i + 1,
                texto=texto,
                ocr_usado=uso_ocr,
                categorias=categorias,
                puntajes=puntajes,
                dividir=dividir,
                partes=partes,
                diagnostico_separacion=diag,
            )
        )
    return resultado


# ─────────────────────────────────────────────────────────────
# Número de factura
# ─────────────────────────────────────────────────────────────

PATRON_FACTURA_TEXTO = re.compile(
    r"NO\.?\s*FACTURA\s*:?\s*([A-Z]{0,4}\s?-?\s?\d{3,})", re.IGNORECASE
)
PATRON_FACTURA_NOMBRE = re.compile(r"([A-Za-z]{0,4}\d{5,})")


def extraer_numero_factura(paginas, nombre_archivo: str) -> str:
    """Intenta encontrar el número de factura:
    1) Buscando 'No. Factura' en el texto de las páginas.
    2) Si no aparece, usa el nombre del archivo (sin extensión).
    """
    for p in paginas:
        norm = normalizar(p.texto)
        m = PATRON_FACTURA_TEXTO.search(norm)
        if m:
            candidato = m.group(1).replace(" ", "")
            if candidato:
                return candidato

    base = nombre_archivo.rsplit(".", 1)[0]
    m = PATRON_FACTURA_NOMBRE.search(base)
    if m:
        return m.group(1)
    return base


# ─────────────────────────────────────────────────────────────
# Construcción del PDF unificado y ordenado
# ─────────────────────────────────────────────────────────────

def construir_pdf_unificado(ruta_pdf_original: str, paginas, categorias_permitidas=None):
    """Arma un nuevo PDF (bytes) con las páginas de las 3 categorías, en el
    orden SIRAS -> CEDULA -> TARJETA_PROPIEDAD, sin duplicar páginas que
    pertenezcan a más de una categoría.

    Cuando una página viene con dos documentos pegados en la misma foto
    (`p.dividir == True`), NO se inserta la página completa: se recorta en
    mitad superior / mitad inferior y cada mitad se inserta como una
    página independiente, para que cada documento quede separado en el
    PDF final (por ejemplo: tarjeta de propiedad arriba, cédula abajo).

    Cada "unidad" del PDF de salida queda identificada como
    (indice_pagina_original, "full" | "sup" | "inf").

    `categorias_permitidas`: si se pasa (un set con nombres como
    {"CEDULA", "TARJETA_PROPIEDAD"}), el PDF final solo incluye esas
    categorías aunque el bot haya detectado más — esto es lo que le
    permite al checklist de "Documentos del paciente"/"Documentos del
    caso" decidir QUÉ traer, mientras que las rutas de red deciden DÓNDE
    buscarlo. Si es None (el caso de siempre, ej. modo "Subir PDF"), se
    incluyen las 3 categorías que el bot identifique, sin filtrar nada.
    """

    paginas_por_categoria = {cat: [] for cat in ORDEN_SALIDA}
    rects_partes = {}  # (indice_pagina, etiqueta) -> (x0,y0,x1,y1) en fraccion 0..1
    for p in paginas:
        if p.dividir:
            for parte in p.partes:
                unidad = (p.indice, parte["etiqueta"])
                rects_partes[unidad] = parte["rect"]
                for cat in ORDEN_SALIDA:
                    if categorias_permitidas is not None and cat not in categorias_permitidas:
                        continue
                    if cat in parte["categorias"]:
                        paginas_por_categoria[cat].append(unidad)
        else:
            for cat in ORDEN_SALIDA:
                if categorias_permitidas is not None and cat not in categorias_permitidas:
                    continue
                if cat in p.categorias:
                    paginas_por_categoria[cat].append((p.indice, "full"))

    orden_final = []
    for cat in ORDEN_SALIDA:
        for unidad in paginas_por_categoria[cat]:
            if unidad not in orden_final:
                orden_final.append(unidad)

    doc_original = fitz.open(ruta_pdf_original)
    doc_salida = fitz.open()

    for idx, etiqueta in orden_final:
        if etiqueta == "full":
            doc_salida.insert_pdf(doc_original, from_page=idx, to_page=idx)
            continue

        # Recortar la parte correspondiente (mitad, o cuadrante si la
        # página se separó en 2 niveles) y meterla como página nueva
        # independiente. El rect queda guardado en fracción 0..1 del
        # tamaño de la página original, sin importar si salió de un
        # corte horizontal, vertical, o de ambos.
        pagina_original = doc_original[idx]
        rect = pagina_original.rect
        x0f, y0f, x1f, y1f = rects_partes[(idx, etiqueta)]
        clip = fitz.Rect(
            rect.x0 + rect.width * x0f,
            rect.y0 + rect.height * y0f,
            rect.x0 + rect.width * x1f,
            rect.y0 + rect.height * y1f,
        )

        pix = pagina_original.get_pixmap(dpi=200, clip=clip)
        # La página de salida usa el tamaño COMPLETO de la hoja original
        # (no el del recorte) para que no se vea "achicada"/cortada: el
        # documento recortado se coloca arriba a la izquierda, y el
        # resto de la hoja queda en blanco, igual que si fuera una hoja
        # normal con un solo documento.
        nueva_pagina = doc_salida.new_page(width=rect.width, height=rect.height)
        area_destino = fitz.Rect(rect.x0, rect.y0, rect.x0 + clip.width, rect.y0 + clip.height)
        nueva_pagina.insert_image(area_destino, stream=pix.tobytes("png"))

    buffer = io.BytesIO()
    if len(doc_salida) > 0:
        doc_salida.save(buffer)
    doc_salida.close()
    doc_original.close()
    buffer.seek(0)
    return buffer.getvalue(), paginas_por_categoria, orden_final


# ─────────────────────────────────────────────────────────────
# Resumen / reporte de un archivo procesado
# ─────────────────────────────────────────────────────────────

def resumir_resultado(nombre_archivo, factura, paginas_por_categoria, paginas):
    """Arma el diccionario de resumen que luego alimenta el Excel."""

    def paginas_str(lista):
        etiquetas = []
        for idx, parte in lista:
            etiqueta = str(idx + 1)
            if parte != "full":
                etiqueta += f" ({parte}.)"
            etiquetas.append(etiqueta)
        return ", ".join(etiquetas) if etiquetas else ""

    encontrados = {
        cat: bool(paginas_por_categoria.get(cat))
        for cat in ORDEN_SALIDA
    }
    faltantes = [NOMBRES_LEGIBLES[c] for c, ok in encontrados.items() if not ok]

    if not faltantes:
        estado = "COMPLETO"
        observaciones = "Se encontraron los 3 documentos (SIRAS, Cédula, Tarjeta de propiedad)."
    elif len(faltantes) == 3:
        estado = "SIN DOCUMENTOS"
        observaciones = "No se identificó ninguno de los 3 documentos requeridos. Revisar manualmente."
    else:
        estado = "INCOMPLETO"
        observaciones = "Falta(n): " + ", ".join(faltantes) + "."

    divididas = [p.numero for p in paginas if p.dividir]
    if divididas:
        observaciones += (
            " Se separaron documentos pegados en la(s) página(s): "
            + ", ".join(str(n) for n in divididas) + "."
        )

    hay_ocr = any(p.ocr_usado for p in paginas)
    if hay_ocr:
        observaciones += " (Se usó OCR en al menos una página; verificar calidad de imagen.)"

    return {
        "Archivo original": nombre_archivo,
        "No. Factura": factura,
        "SIRAS": "Sí" if encontrados["SIRAS"] else "No",
        "Pág. SIRAS": paginas_str(paginas_por_categoria.get("SIRAS", [])),
        "Cédula/Migrante": "Sí" if encontrados["CEDULA"] else "No",
        "Pág. Cédula": paginas_str(paginas_por_categoria.get("CEDULA", [])),
        "Tarjeta Propiedad": "Sí" if encontrados["TARJETA_PROPIEDAD"] else "No",
        "Pág. Tarjeta": paginas_str(paginas_por_categoria.get("TARJETA_PROPIEDAD", [])),
        "Estado": estado,
        "Observaciones": observaciones,
    }
