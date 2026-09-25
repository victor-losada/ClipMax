# Arquitectura y plan de implementación

## 1. Objetivo

Automatizar, en un PC con Windows y sin suscripciones extra, el ciclo diario del **Desafío 4** (Kick):

1. Grabar a los streamers configurados, cada uno en su propio MP4, a la hora del evento (15:00 COL, 7–8 h).
2. Detectar momentos candidatos **sin modelos entrenados**: picos de chat, menciones cruzadas (sobre todo **Westcol ↔ Gear of Nos**) y lo que se comenta en X.
3. Dejar que **Claude** decida qué es lore, escriba el guion narrado y devuelva los cortes exactos.
4. Montar con **ffmpeg** un resumen de 10–20 min con cortes limpios y sin silencios largos.
5. Generar un documento con los mejores momentos, por qué importan y captions para TikTok.

Todo es local y gratis salvo la API de Claude, que tiene un tope mensual configurable (por defecto **$10**).

## 2. Diagrama general

```
                         ┌──────────────── EN VIVO (15:00 → 23:15) ────────────────┐
 Kick (HLS) ──yt-dlp──►  │ StreamRecorder ×N ─ ffmpeg -c copy ─► partes .ts         │
                         │        │ hora de pared de cada parte ─────────► SQLite   │
 Kick chat (Pusher) ───► │ ChatListener ×N ─ buckets 5 s, hype, menciones ─► SQLite │
                         │ LiveTranscriber (pareja principal) ─ whisper.cpp base    │
                         │        └─ "mencion_voz" (A nombra a B) ────────► SQLite  │
 X (manual/API/web) ───► │ contexto del día ─────────────────────────────► SQLite   │
                         │ Detección en vivo cada 5 min (panel web)                 │
                         └──────────────────────────────────────────────────────────┘
                                                  │ cierre
                                                  ▼
 ┌──────────────────────── POST-PROCESO (pipeline.py, reanudable) ─────────────────────┐
 │ 1 finalizar   partes .ts ─► 1 MP4 por streamer + desfase de cada parte               │
 │ 2 detectar    z-score robusto del chat, menciones, sincronía ─► momentos puntuados  │
 │ 3 transcribir whisper.cpp small sobre los 40 mejores candidatos                     │
 │ 4 contexto_x  archivos de X / búsqueda web de Claude (opcional)                     │
 │ 5 puntuar     menciones en la transcripción + temas de X + penaliza "poca voz"      │
 │ 6 decidir     Claude (API, JSON estructurado)  ó  paquete .md para claude.ai        │
 │ 7 editar      ffmpeg: recortes, silencios, encuadre, títulos, tarjetas, concat      │
 │ 8 reportar    resumen_<fecha>.md/.html + clips verticales para TikTok               │
 └──────────────────────────────────────────────────────────────────────────────────────┘
                                                  ▲
                       Interfaz web local (Flask) ─ http://127.0.0.1:5000
```

## 3. Módulos

| Módulo | Responsabilidad |
|---|---|
| `config.py` | Carga `config.yaml`, rellena valores por defecto, valida y guarda (lo usa la web). |
| `db.py` | SQLite (modo WAL) con sesiones, partes grabadas, chat, transcripciones, señales, momentos, posts de X, costos de Claude y salidas. |
| `kick.py` | API `kick.com/api/v2/channels/<slug>` con `curl_cffi` (imita a Chrome y pasa Cloudflare, igual que yt-dlp). Da: en vivo, URL HLS y `chatroom_id`. |
| `recorder.py` | Un hilo por streamer. `yt-dlp -g` resuelve la variante ≤ calidad máxima; `ffmpeg -c copy` graba a **MPEG-TS** (sobrevive a cortes de luz). Reconexión automática → partes nuevas. Al cerrar, concatena a **un MP4 por streamer** sin recodificar. `locate_range()` traduce hora de pared → segundo exacto del MP4. |
| `chat.py` | Websocket de Pusher (el mismo que usa la web de Kick). Cuenta mensajes por bucket de 5 s, mensajes de "hype" y menciones a otros streamers; guarda el texto por lotes. |
| `mentions.py` | Normalización (tildes, emotes de Kick `[emote:id:KEKW]`, @) y búsqueda de alias con límites de palabra + coincidencia difusa para errores de whisper ("Wescol"). |
| `transcriber.py` | whisper.cpp local. En vivo (modelo base, tramos de 60 s, solo la pareja) y por candidato (modelo small). Filtra alucinaciones típicas en español ("Amara.org"). |
| `xcontext.py` | Contexto de X: pegado manual ($0), archivos, API v2 con token (opcional). Extrae temas (palabras y bigramas). |
| `detector.py` | Señales y momentos (ver §4). |
| `prompts.py` + `prompts/prompt_maestro.md` | Prompt maestro (fuente única) y armado del material del día. |
| `brain.py` | Llamada a Claude con salida estructurada, fallback por rechazo, control de presupuesto; modo manual (exportar/importar); validación de la decisión. |
| `editor.py` + `cards.py` + `tts.py` | Montaje con ffmpeg; tarjetas y títulos con Pillow; voz opcional (SAPI de Windows o Piper). |
| `report.py` | Documento del día en Markdown y HTML. |
| `pipeline.py` | Orquestación por pasos con estado persistente. |
| `scheduler.py` | Horario, sesión en vivo, bloqueo de suspensión de Windows, reanudación tras reinicio. |
| `web/` | Panel, configuración, sesiones, contexto de X, modo manual, calibración de cámara. |

## 4. Detección sin modelos entrenados

**Pico de chat.** Por cada streamer: serie de mensajes cada 5 s, suavizada (media móvil de 15 s). Para cada punto se calcula un z-score robusto contra los 10 minutos previos: `z = (x − mediana) / (1.4826·MAD + 1)`. Es pico si `z ≥ 3.5`, si es al menos 1.8× el ritmo normal y si hay un mínimo de mensajes. Como cada chat se mide contra sí mismo, un chat de 50 000 espectadores y uno de 2 000 compiten en igualdad. La puntuación sube con la intensidad y con la fracción de mensajes de risa/sorpresa/"clip".

**Mención en el chat.** En el chat de A se dispara la cantidad de mensajes que nombran a B (suma móvil de 30 s ≥ 3× su nivel habitual del día).

**Mención por voz.** A nombra a B en su audio. En vivo para la pareja principal; en post-proceso para todos los candidatos. Pesa 2.5× un pico de chat, porque es exactamente "uno habla del otro".

**Sincronía.** A y B tienen picos de chat con menos de 45 s de diferencia: probablemente están interactuando.

**Temas de X.** Palabras y bigramas más repetidos en los posts del día; si la transcripción de un candidato los toca, suma puntos. Si hay posts con hora (modo API), las ráfagas de posts también suman a los momentos cercanos.

**Momento.** Las señales de un mismo streamer se convierten en ventanas (p. ej. pico de chat: 50 s antes y 25 s después, porque el chat reacciona tarde) y las que se solapan se funden, con un máximo de 3 min. Puntuación = suma ponderada con rendimientos decrecientes por tipo, × **1.6 si involucra a Westcol ↔ Gear of Nos**, × prioridad del streamer, × 0.6 si casi no hay conversación (gameplay callado). Los momentos de distintos streamers que se solapan se marcan como **"mismo suceso"** para que Claude pueda mostrar las dos cámaras.

## 5. Claude como cerebro

- **Una sola llamada por día**, en streaming, con `system` = prompt maestro y `user` = material del día (candidatos con transcripción y tiempos relativos, muestra del chat, menciones, contexto de X y el lore de días anteriores).
- **Salida estructurada** (`output_config.format` con JSON Schema estricto): nunca hay JSON roto. La decisión se valida igual: tiempos recortados al rango de cada candidato, candidatos inexistentes descartados y, si se pasa de 20 min, se quitan primero los clips de prioridad baja.
- **Thinking adaptativo** y `effort` configurable. En Claude Opus 5 se activa `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`): si una transcripción cruda activa un rechazo, la API reintenta en otro modelo sin que tengas que hacer nada.
- **Continuidad**: Claude devuelve `lore_para_manana`, que al día siguiente vuelve como memoria.
- **Modo manual**: el mismo prompt + material se exporta como `.md` para pegar en claude.ai; la respuesta JSON se pega en la web. Costo de API: $0.

### Presupuesto (≤ $10/mes)

Importante: **el plan de claude.ai (Pro/Max) no incluye créditos de API**. La API se paga aparte en console.anthropic.com. Por eso ClipMax:

1. Cuenta los tokens antes de llamar (endpoint gratuito) y estima el costo.
2. Si `gastado_del_mes + estimado > presupuesto_mensual_usd`, **no llama**: deja listo el paquete para el modo manual.
3. Registra el costo real de cada llamada en SQLite y lo muestra en el panel.

Costo típico por día (40 candidatos, ~25 000 tokens de entrada y ~8 000–14 000 de salida con razonamiento):

| Modelo | Precio (entrada / salida por M) | Por día | 30 días |
|---|---|---|---|
| `claude-opus-5` (por defecto) | $5 / $25 | ≈ $0.33–0.48 | ≈ $10–14 |
| `claude-sonnet-5` | $2 / $10 | ≈ $0.13–0.19 | ≈ $4–6 |

Con Opus 5 todos los días del mes se llega al tope; si el evento dura más de ~20 días, cambia a `claude-sonnet-5` en Configuración, baja el esfuerzo a `medium` o reduce `candidatos_max`. El tope protege en cualquier caso: pasado el límite, el sistema cambia solo al modo manual. El modo `claude_web` para X suma unos $0.05–0.30 por día.

## 6. Edición

Por cada clip hay **una sola pasada de ffmpeg**:

1. `-ss/-t` sobre el MP4 del streamer.
2. `trim/atrim` de cada tramo con voz. Los huecos sin voz de más de 2.5 s se eliminan según la transcripción, salvo que recortar deje menos del 25 % del clip: eso es un momento visual o de pura reacción y se deja entero.
3. `concat` de los tramos.
4. Encuadre según el modo del streamer.
5. Efectos (ver abajo).
6. `loudnorm` (todos suenan igual de fuerte) y fundidos de 40 ms en cada corte, para que no haya clics.

Los cortes de Claude se ajustan al límite de la frase más cercana para no cortar palabras.

**Encuadre según el modo:**
- `juego_cara` + horizontal: stream completo.
- `cara`: solo el recuadro de la cámara (se marca una vez con el mouse en la web), ampliado con fondo desenfocado.
- Formato vertical (TikTok) con cámara definida: cámara arriba y juego abajo.

**Efectos** (`clipmax/effects.py`, se activan y desactivan en Configuración → Efectos):

| Efecto | Cómo funciona |
|---|---|
| Subtítulos dinámicos | 2-4 palabras en mayúsculas; la que se está diciendo se ilumina en verde con un pequeño "pop". Tiempos por palabra de whisper (`-ojf`) o, si no los hay, estimados dentro de la frase. Se remapean a la línea de tiempo ya sin silencios. Archivo ASS dibujado por libass. |
| Zoom suave | Acercamiento del 12 % en el `momento_clave` que marca Claude (o en el pico de chat si no marcó ninguno), con entrada y salida de 0.35 s. |
| Pantalla dividida | Cuando Claude pone `pantalla_dividida_con`, el mismo instante de otro streamer (el "mismo suceso") se ve a la par: lado a lado en horizontal, arriba/abajo en vertical, con el nombre de cada uno. Se escucha solo el audio del clip principal (los dos juntos harían eco si están en llamada). |
| Efectos de sonido | Pocos: los que Claude elige para el remate (`boom`, `ding`, `impacto`, `pop` o los tuyos en `sfx/`) y un `whoosh` suave al pasar de una tarjeta a un clip, con tope por video (`sfx_max_por_video`). Los incluidos se generan con ffmpeg: no tienen derechos de autor. |

Sin música, a propósito: da problemas de copyright en TikTok y YouTube. Si un efecto hace fallar a ffmpeg, ese clip se vuelve a renderizar sin efectos: un efecto nunca tumba el video del día.

Las narraciones son tarjetas con el fotograma del clip siguiente desenfocado, y opcionalmente voz local. Todas las piezas se codifican con los mismos parámetros y se unen sin recodificar.

## 7. Decisiones técnicas y por qué

| Decisión | Motivo |
|---|---|
| Grabar a `.ts` y después pasar a MP4 | Un MP4 a medio escribir queda inservible si se va la luz o se cierra el programa; un `.ts` no. La conversión final es sin recodificar (segundos). |
| `ffmpeg -c copy` al grabar | Casi 0 % de CPU: permite grabar decenas de streams en un PC normal. Recortar la cámara se hace al editar, no al grabar. |
| Hora de pared por parte | El chat, las transcripciones en vivo y los posts de X viven en la misma línea de tiempo; así se mapea cualquier señal a un segundo exacto del video. |
| Transcripción en vivo solo para la pareja | whisper en CPU no da abasto para decenas de streams; la pareja es lo que más importa. El resto se transcribe solo en sus candidatos. |
| Una llamada a Claude por día | Minimiza el costo y le da a Claude la visión completa del día para armar arcos narrativos. |
| Pillow para textos en lugar de `drawtext` | Evita el infierno de escapar rutas y texto de Windows dentro de filtros de ffmpeg. |
| SQLite + un lock | Suficiente para el volumen de un PC y sin instalar servicios. |

## 8. Plan de implementación (orden seguido)

1. **Núcleo**: configuración validada, esquema SQLite, localización de binarios, cliente de Kick.
2. **Captura**: grabador con reconexión y partes; lector de chat Pusher; alias y hype; whisper.cpp en vivo y por ventana; contexto de X.
3. **Detección**: señales → momentos → ranking; re-puntuación con transcripción y temas de X.
4. **Cerebro**: prompt maestro, material del día, llamada con salida estructurada y presupuesto, modo manual, validación.
5. **Edición y reporte**: recortes con silencios, encuadres, tarjetas, concat, clips TikTok, documento diario.
6. **Orquestación**: pipeline reanudable y programador por horario con reanudación.
7. **Interfaz**: panel en vivo, configuración, sesiones, cámara; CLI; instaladores `.bat`.
8. **Verificación**: 46 pruebas automáticas (incluye grabación y render reales con ffmpeg) y `python -m clipmax demo`.

## 9. Riesgos conocidos y mitigación

- **Kick cambia su API o la clave de Pusher.** La clave es configurable (`chat.pusher_keys`, con rotación automática si una falla) y `chatroom_id` se puede fijar a mano. Mantén yt-dlp al día (`pip install -U yt-dlp`).
- **Cloudflare bloquea la API.** Se usa `curl_cffi` (lo mismo que yt-dlp); `python -m clipmax doctor` lo verifica.
- **Espacio en disco.** A 720p son ≈ 1.8 GB por hora por streamer (≈ 15 GB por streamer al día). Con decenas de streamers usa 480p (`calidad_max`) o desactiva a los secundarios. `doctor` calcula lo que necesitas.
- **CPU de whisper.** Si el transcriptor en vivo se atrasa, salta al presente (lo verás en el panel). Con GPU NVIDIA: `python -m clipmax descargar --cuda`.
- **Sincronía chat/video.** La latencia HLS es de unos segundos; las ventanas tienen margen (50 s antes, 25 s después) y existe `desfase_chat_s` para ajuste fino.

## 10. Clips en vivo, resumen para TikTok y sincronía

**Clips en vivo** (`liveclips.py`). `LiveClipper` es un hilo más de la sesión (lo arranca `SessionManager.start`). Cada `clips_vivo.intervalo_s` toma los mejores momentos de la detección en vivo, ya terminados y grabados (35 s de margen), que no se solapen con un clip ya hecho. Luego:

1. Transcribe el tramo con el modelo de calidad (queda en la base de datos y el cierre la reutiliza).
2. Pide a Claude, con `curate_live_clip`, salida estructurada `CLIP_SCHEMA` y `prompts/clip_vivo.md`: si se publica, corte, remate, título, caption, hashtags y efecto. El modelo es `clips_vivo.modelo`, Haiku por defecto (~$0.005 por clip), con el mismo control de presupuesto mensual. Sin API, o sin presupuesto, usa reglas: el remate es el pico del chat menos la reacción.
3. Renderiza en 1080x1920 con `editor.render_clip`.

Tope por hora. El botón 🎬 del Panel encola un momento concreto y salta ese tope. `tools.background_priority()` baja la prioridad de whisper/ffmpeg en ese hilo, y un único candado de whisper evita dos transcripciones a la vez.

**Resumen para TikTok** (`editor.render_tiktok_summary`). Si los hechos de `resumen_tiktok` traen narración y está la voz de Piper, lo arma `tiktok_recap.py` según la ficha vertical:
- `narrator.py` sintetiza frase por frase, sin pausas de más de 0.3 s y con el tiempo de cada palabra;
- cada hecho es una pieza: cortes rápidos leídos con `-ss` por plano, clip 16:9 centrado sobre su versión desenfocada, contadores que cambian en la palabra clave, flash de color, ráfaga con cuadro de impacto y la cita con el plan del director (`style.direct`);
- intro con revelado pixel y cierre con primerísimo plano y "sígueme".

Si no, toma los tramos de `resumen_tiktok` o los arma de los mejores momentos (gancho primero y luego cronológico) y los renderiza en vertical con el texto en pantalla. En los dos casos el tope es `edicion.resumen_tiktok_max_s`.

**Sincronía y tiempos** (medidos con grabaciones reales de Kick y ffmpeg 9):
- Kick transmite a 60 fps. Todo el render pasa primero a los fps de salida (el zoompan renumeraba fotogramas: cámara lenta).
- Tras `-ss`, audio y video se alinean al cero común del corte (`fps=…:start_time=0`, `aresample=async=1:first_pts=0`), no cada pista por su cuenta.
- Al conectarse a un directo HLS, ffmpeg baja de golpe 8–18 s ya emitidos. El grabador lo mide a los 20 s y corrige la hora del segundo 0 del archivo; sin eso, los clips salían corridos respecto al chat.
- Subtítulos: tiempos por palabra con DTW de whisper (`-dtw <modelo> -nfa`). Sin DTW, los tiempos por token se desvían hasta ±1 s.
- `diagnostic.py` arma un clip de prueba con las grabaciones del usuario y compara la curva de movimiento del video y la envolvente del audio contra la fuente. Detecta desfases de 30 ms en adelante y sugiere `edicion.desfase_audio_s`.
