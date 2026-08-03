# Bot Extractor de Documentos SOAT — v1 (pruebas locales)

Identifica, dentro de los PDF de una reclamación SOAT, las páginas que
corresponden a:

- **SIRAS**
- **CÉDULA** (cédula de ciudadanía, Permiso por Protección Temporal / doc.
  de migrante, o Registro Civil de menores)
- **TARJETA DE PROPIEDAD** (licencia de tránsito del vehículo)

y arma un PDF nuevo — nombrado con el número de factura — con esos 3
documentos unificados y en ese orden. También genera un Excel con el
resultado de cada archivo procesado (qué se encontró, qué faltó).

**Novedades de esta entrega:**
- **Login** con Servidor + Usuario + Contraseña + Empresa, usando la misma
  API que ya usa Esculapio (`obtener-servidores` / `obtener-empresas`).
- **Checklist de documentos** organizado en "Documentos del paciente" y
  "Documentos del caso" (igual que en el centro de digitalización), con
  Cédula/SIRAS/Tarjeta de Propiedad marcados por defecto. El resto del
  catálogo ya queda armado en pantalla, marcado como "pronto" — se activan
  a medida que se les vaya sumando la identificación.
- **Dos formas de traer casos**: subir PDF a mano (como hasta ahora, para
  seguir probando), o **buscar por factura/caso** — a mano (una por línea)
  o subiendo un Excel/CSV con columnas `NoCuenta`/`NoFactura` (hay botón
  para descargar la plantilla).

⚠️ **Importante:** el modo "Buscar por factura/caso" ya tiene toda la
pantalla y el flujo armados, pero la función que de verdad trae los PDF
desde el sistema real (`_obtener_documentos_caso` en `app.py`) todavía
está pendiente — falta que compartan el endpoint (URL + parámetros) de esa
API. Mientras tanto, muestra un mensaje claro de "falta conectar" y el
modo "Subir PDF" sigue funcionando normal para seguir probando el motor de
clasificación.

---

## 1. Requisitos

- **Windows** con **Python 3.11+** instalado y agregado al PATH.
  - Descarga: https://www.python.org/downloads/ (marcar "Add python.exe to
    PATH" durante la instalación).
- **Un motor de OCR**, para leer páginas escaneadas como imagen (cédulas,
  tarjetas de propiedad, SIRAS escaneado). Hay dos opciones, no hace falta
  instalar las dos:

  **Opción A — Tesseract OCR (recomendada, más rápida y liviana)**
  - Descarga: https://github.com/UB-Mannheim/tesseract/wiki
  - Durante la instalación, en "Additional language data" marca **Spanish**.
  - Déjalo en la ruta por defecto y que quede agregado al PATH (el
    instalador de UB-Mannheim lo pregunta).

  **Opción B — EasyOCR (sin instalador aparte, solo si no quieres/puedes
  instalar Tesseract)**
  - No requiere nada fuera de Python. No se instala por defecto (es
    pesado, ~500 MB-1 GB con PyTorch); `iniciar.bat` la instala solo
    automáticamente la primera vez que detecta que Tesseract no está.
  - La primera vez que se usa descarga sus modelos de reconocimiento
    (~65 MB adicionales, necesita internet); luego funciona sin conexión.
  - Es más pesado y más lento por página que Tesseract.

  **El bot detecta solo cuál usar:** si encuentra Tesseract instalado en el
  sistema, lo usa (es lo más rápido). Si no lo encuentra, cae
  automáticamente a EasyOCR. No hay que configurar nada a mano.

Sin ninguno de los dos instalados, el bot funciona igual, pero **no podrá
leer las páginas que son solo una foto/escaneo** (la mayoría de cédulas y
tarjetas de propiedad).

## 2. Arranque

1. Doble clic en **`iniciar.bat`**.
   - La primera vez va a crear un entorno virtual e instalar las
     dependencias (puede tardar 1-2 minutos). Las siguientes veces arranca
     directo.
2. Abre el navegador en: **http://localhost:5057**
3. Arrastra uno o varios PDF (o haz clic para seleccionarlos) y pulsa
   **"Procesar PDF(s)"**.
4. En la tabla de resultados vas a ver, por cada archivo:
   - Si se encontró SIRAS, Cédula/Migrante y Tarjeta de propiedad (y en
     qué página).
   - El **estado**: `COMPLETO`, `INCOMPLETO` o `SIN DOCUMENTOS`.
   - Un link para descargar el **PDF unificado** (nombrado con el número
     de factura).
5. Puedes descargar también:
   - El **informe en Excel** con el detalle de todos los archivos del lote.
   - Un **ZIP** con todos los PDF generados + el Excel.

## 3. Cómo clasifica las páginas (checklist)

El bot NO usa un modelo de IA para "leer" el documento; usa un motor de
**checklist por palabras clave** (ver `clasificador.py`), igual a la lógica
que describiste: "si tiene esto + esto + esto, es tal documento".

1. Para cada página del PDF, intenta extraer el texto nativo (si el PDF
   trae texto seleccionable, como el FURIPS o la epicrisis).
2. Si la página es una **imagen/escaneo** (cédulas, tarjetas de
   propiedad, SIRAS escaneado, etc.), la renderiza y le aplica **OCR**
   (texto completo + recortes de la página, para poder leer bien cuando
   hay varios documentos fotografiados juntos en una sola página).
3. Compara el texto contra el checklist de cada categoría y suma un
   puntaje por cada coincidencia:
   - **SIRAS**: título institucional ("Sistema de Información de Reportes
     de Atención en Salud..."), nombres de las secciones numeradas
     ("Datos de la víctima", "Tipo de ingreso", "Datos del conductor", etc.)
   - **CÉDULA**: "Cédula de Ciudadanía", "Identificación Personal",
     "Registrador Nacional"; o, para el documento de migrante: "Permiso
     por Protección Temporal", "Migración Colombia".
   - **TARJETA DE PROPIEDAD**: "Licencia de Tránsito", placa, marca/línea/
     modelo, clase de vehículo, número de motor/chasis. Se excluyen a
     propósito las páginas de "Consulta Automotores" (RUNT en línea), que
     mencionan campos parecidos pero no son la tarjeta física.
   - Si el puntaje de una categoría llega al mínimo (`UMBRAL_PUNTAJE`), la
     página queda marcada con esa categoría.
4. **Documento de migrante (PPT) = documento de identificación**: el PPT
   cae en la misma categoría que la cédula (`CEDULA`), así que si no viene
   cédula de ciudadanía pero sí viene el PPT, el bot lo toma como
   documento de identificación válido — igual que pediste.
5. **Separación de documentos pegados**: cuando una página escaneada
   resuelve con 2 categorías a la vez (por ejemplo, tarjeta de propiedad
   arriba + cédula abajo en la misma foto), el bot NO la deja como un solo
   documento. Busca el punto de corte entre las dos imágenes, revisa cada
   mitad por separado, y si cada una resuelve claramente a un documento
   distinto, las separa en **dos páginas independientes** dentro del PDF
   de salida (cada una ya identificada con su propia categoría). Si el
   corte no se puede determinar con confianza (fotos muy pegadas o
   sobrepuestas), la página se deja completa y marcada con ambas
   categorías, para no arriesgar a cortar mal un documento.
6. Arma el PDF final tomando, en orden, las páginas/mitades SIRAS →
   CÉDULA → TARJETA, sin repetir contenido que ya haya salido antes.

**Es normal que falte alguno de los 3 documentos** (por ejemplo, no todas
las reclamaciones traen SIRAS). Cuando eso pasa, el bot no falla: marca la
factura como `INCOMPLETO` y anota en la columna "Observaciones" cuál
documento no se encontró, para que se pueda revisar manualmente. Cuando el
bot separó documentos pegados en alguna página, también lo anota ahí.

### Ajustar la clasificación

Si en las pruebas el bot se equivoca (por ejemplo, confunde una página, no
detecta un documento por mala calidad del escaneo, o no logra separar dos
documentos pegados), los puntajes y palabras clave se ajustan en
`clasificador.py`, en las listas `KEYWORDS_SIRAS`, `KEYWORDS_CEDULA` y
`KEYWORDS_TARJETA`, y en las variables `UMBRAL_PUNTAJE` (umbral normal) y
`UMBRAL_PUNTAJE_MITAD` (umbral más permisivo al revisar cada mitad de una
página combo). No hace falta tocar nada más.

## 4. Estructura del proyecto

```
SOAT_Extractor_Bot/
├── app.py                 → servidor web (Flask) y rutas de descarga
├── clasificador.py         → el "cerebro": extrae texto/OCR y clasifica páginas
├── ips_mapping.py           → utilidad de referencia para la próxima etapa
│                              (cruce por NIT con config/mapeo_ips.xlsx)
├── config/
│   └── mapeo_ips.xlsx       → copia del mapeo de IPS/Empresas compartido
├── templates/
│   └── index.html           → interfaz web (subir PDF, ver resultado)
├── uploads/                 → carpeta temporal (se limpia sola tras procesar)
├── outputs/                 → PDFs unificados + Excel de cada lote procesado
├── requirements.txt
├── iniciar.bat               → arranque con un doble clic
└── README.md
```

## 5. Qué falta para la versión conectada a la API

Este v1 trabaja con PDF subidos manualmente a propósito, para poder
afinar la clasificación con casos reales antes de conectar la fuente de
datos. Los siguientes pasos, cuando estén listas las rutas/API:

1. Reemplazar la carga manual (`request.files`) por la consulta a la API
   que entregue el PDF de cada factura.
2. Usar `ips_mapping.py` para saber, a partir del NIT del prestador, a
   qué IPS/sede pertenece cada documento.
3. Definir si el procesamiento se dispara por lote/factura o de forma
   continua (cola de radicados nuevos).

El motor de clasificación (`clasificador.py`) no debería necesitar
cambios grandes para ese salto; sólo cambia **de dónde** llega el PDF de
entrada.
