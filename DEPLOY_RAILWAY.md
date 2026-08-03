# Desplegar en Railway

## Qué ya viene listo en este proyecto
- `railway.json` — `sleepApplication: true` (Railway apaga el contenedor
  cuando nadie lo usa) + arranque con gunicorn (`--workers 1 --threads 4`,
  necesario porque el estado del bot vive en memoria — con más de 1
  worker cada uno tendría su propia copia y se desincronizarían).
- `nixpacks.toml` — instala Tesseract-OCR (español + inglés) como
  dependencia de sistema — no es un paquete de Python, así que hace
  falta este paso aparte.
- `requirements.txt` — incluye `gunicorn`.
- El polling liviano del navegador se pausa solo cuando la pestaña no
  está visible (para que Railway sí pueda detectar que nadie lo está
  usando y lo pueda dormir).
- No hay conexiones largas (SSE/WebSockets) — todo es peticiones cortas
  repetidas, así que no hay riesgo de que una plataforma en la nube corte
  una conexión a los ~15 minutos.

## Antes de darle "Deploy"
1. **Variable de entorno obligatoria:** `SECRET_KEY` — pon cualquier
   cadena larga y aleatoria. Sin esto, usa una clave de respaldo fija
   pensada solo para pruebas locales — nunca la dejes así en producción
   real (cualquiera podría falsificar sesiones).
2. Railway pone `PORT` automáticamente — no hay que configurarlo a mano,
   el proyecto ya lo usa solo.
3. Revisa que el repo que conectes a Railway tenga esta carpeta
   (`SOAT_Extractor_Bot/`) como raíz del servicio, o ajusta el "root
   directory" en la configuración del servicio en Railway si el repo
   tiene más cosas alrededor.

## Cosas a tener en cuenta (no son bugs, son la naturaleza de Railway)
- **Primer request después de dormir ("cold start"):** unos segundos de
  demora la primera vez que alguien entra tras un rato de inactividad.
  Después responde normal hasta que se vuelva a dormir.
- **El disco no es permanente entre despliegues.** `progreso.json` (para
  reanudar un lote detenido) y las carpetas `uploads/`, `outputs/`,
  `estado/` sobreviven mientras el mismo contenedor esté vivo (incluso
  dormido y despertado), pero un nuevo *deploy* las reinicia desde cero.
  Si necesitas que el progreso sobreviva entre despliegues, hay que
  agregar un volumen persistente de Railway — avísame si lo necesitas y
  lo agregamos.
- **Red hacia el sistema del cliente:** el bot llama a
  `https://appsintranet.esculapiosis.com` (API pública de Esculapio/
  Campbell) — es HTTPS normal, no necesita VPN ni red privada para
  llegar desde Railway.
