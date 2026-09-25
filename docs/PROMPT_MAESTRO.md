# Prompt maestro: cómo usarlo cada día

El prompt vive en **`prompts/prompt_maestro.md`** y es la fuente única:

- En **modo API**, ClipMax lo envía como *system prompt* y le suma el material del día como mensaje. La respuesta llega como JSON validado contra un esquema.
- En **modo manual**, lo pegas tú en claude.ai (costo de API $0, lo cubre tu plan).

Si editas `prompts/prompt_maestro.md` (por ejemplo, para cambiar el tono), el cambio aplica en los dos modos. Las variables `{{evento}}`, `{{pareja_a}}`, `{{pareja_b}}`, `{{duracion_min}}`, `{{duracion_max}}` y `{{hashtag}}` se rellenan con tu configuración. Para ver el texto final:

```bat
python -m clipmax prompt-maestro
```

## Flujo manual diario (claude.ai)

1. Al terminar el día, ClipMax ya grabó, detectó, transcribió y puntuó. Entra a **Sesiones → (fecha)**.
2. Pulsa **"1 · Generar y copiar paquete"**. Se copia al portapapeles un texto con:
   - el prompt maestro;
   - el material del día: streamers, notas de lore, historia de días anteriores (lore y contexto de X), temas y posts de X, menciones por voz detectadas y los candidatos con su transcripción, sus tiempos y lo que decía el chat.
   (También se guarda en `data\sesiones\<fecha>\claude\paquete_para_claude_<fecha>.md`.)
3. En claude.ai abre un chat nuevo y pega el paquete.
   - **Opcional:** si tu plan tiene búsqueda web, actívala y agrega al final: *"Antes de decidir, busca qué se comentó hoy en X sobre el Desafío 4, Westcol y Gear of Nos, y úsalo como contexto."* El prompt ya contempla ese caso y lo resume en `notas_editor`.
   - Si el paquete es muy largo para un solo mensaje, adjúntalo como archivo `.md`.
4. Claude responde con un bloque JSON. Cópialo completo.
5. Pégalo en **"2 · Respuesta JSON de Claude"** y pulsa **"Importar y renderizar"**. ClipMax valida la respuesta (recorta tiempos fuera de rango, descarta candidatos inexistentes y ajusta la duración), arma el video y genera el reporte.

Desde la línea de comandos, lo mismo:
```bat
python -m clipmax exportar-paquete --fecha 2026-09-23
REM ... pega en claude.ai y guarda la respuesta como respuesta.json ...
python -m clipmax importar-respuesta respuesta.json --fecha 2026-09-23
```

## Qué devuelve Claude

```json
{
  "titulo_video": "...",
  "resumen_del_dia": "...",
  "lore_para_manana": "...",
  "guion": [
    {"tipo": "narracion", "texto": "...", "candidato_id": 0, "inicio": 0, "fin": 0, "titulo_en_pantalla": "", "prioridad": 5, "motivo": "",
     "momento_clave": 0, "efecto_sonido": "", "pantalla_dividida_con": 0},
    {"tipo": "clip", "texto": "", "candidato_id": 12, "inicio": 8.5, "fin": 61.0, "titulo_en_pantalla": "GEAR RESPONDE", "prioridad": 5, "motivo": "...",
     "momento_clave": 42.3, "efecto_sonido": "boom", "pantalla_dividida_con": 15}
  ],
  "mejores_momentos": [{"candidato_id": 12, "inicio": 8.5, "fin": 61.0, "titulo": "...", "por_que_importa": "...", "captions_tiktok": ["..."], "hashtags": ["#Desafio4"]}],
  "descartados": [{"candidato_id": 3, "motivo": "gameplay sin conversación"}],
  "notas_editor": "..."
}
```

- `inicio` / `fin` van en segundos desde el inicio de cada candidato. ClipMax los ajusta al límite de la frase más cercana y recorta los silencios internos.
- `prioridad` decide qué se quita primero si el video pasa de 20 minutos.
- `momento_clave` es el segundo del remate: ahí entran el zoom suave y el `efecto_sonido` (si Claude eligió uno).
- `pantalla_dividida_con` muestra a la par a otro candidato del "mismo suceso" (por ejemplo, Gear reaccionando mientras Westcol habla).
- Si pegas una respuesta vieja sin estos campos, funciona igual: se toman como 0 o vacíos.
- `lore_para_manana` se guarda y al día siguiente vuelve como memoria, para que la historia tenga continuidad. El material incluye la *Historia de días anteriores* (lore más un resumen del contexto de X de los últimos 3 días) y los temas de hoy que ya venían de antes. Ver [CONTEXTO_X.md](CONTEXTO_X.md#continuidad-entre-días).

## Consejos para mejores resúmenes

- **Notas fijas de lore** (Configuración → Evento): escribe ahí las rivalidades, los equipos y las reglas del torneo. Claude las recibe todos los días.
- **Contexto de X**: 5–15 posts relevantes bastan. Si copias el enlace del post, se guarda el autor. Los temas repetidos también suben la puntuación de los candidatos que los mencionan.
- **Alias**: agrega los apodos con que el chat y los demás streamers llaman a cada uno. Así mejora mucho la detección de menciones.
- **Re-editar sin pagar de nuevo**: si cambias el formato (vertical/horizontal) o la narración, usa "desde aquí" en el paso **editar**. La decisión de Claude se reutiliza.
