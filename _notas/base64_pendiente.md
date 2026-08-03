# Base64 → archivo — lógica lista, pendiente de decir DÓNDE se usa

Tomado de `Esculapio_CM_Local` (`procesar_firma_base64`, línea ~325 de su `app.py`).
Ahí lo usaban para decodificar la firma escaneada del médico (campo
`FirmaMedico` de su API) y limpiarla (fondo blanco → transparente, recorte
al contenido). Para SOAT no será una firma — falta que el usuario diga
para qué campo/documento se va a usar exactamente.

## Lógica base reutilizable (decodificación pura, sin el recorte de firma)

```python
import base64
from io import BytesIO
from PIL import Image

def decodificar_base64_a_archivo(b64_string, ruta_salida=None, es_imagen=True):
    """Decodifica un string base64 (agregando el padding que le falte,
    algo común cuando el dato viene de una base de datos/API) a bytes, y
    opcionalmente lo guarda en disco.

    - Si es_imagen=True, se abre con PIL para validar que es una imagen
      válida antes de guardar (detecta datos corruptos temprano).
    - Si es_imagen=False (ej. un PDF completo en base64), se guardan los
      bytes tal cual, sin pasar por PIL.
    """
    if not b64_string:
        return None
    faltante = len(b64_string) % 4
    if faltante:
        b64_string += "=" * (4 - faltante)
    datos = base64.b64decode(b64_string)

    if es_imagen:
        img = Image.open(BytesIO(datos))
        if ruta_salida:
            img.save(str(ruta_salida))
            return ruta_salida
        return img
    else:
        if ruta_salida:
            with open(ruta_salida, "wb") as f:
                f.write(datos)
            return ruta_salida
        return datos
```

## Pendiente de confirmar antes de conectarlo
- ¿En qué endpoint/campo va a venir el base64? (¿la futura API de
  documentos por caso, si llega a existir? ¿algo del centro digital?)
- ¿Es una imagen (foto de cédula/tarjeta) o un PDF completo?
- ¿Hace falta el mismo tratamiento de "fondo blanco transparente" que
  tenían para la firma, o se guarda tal cual?
