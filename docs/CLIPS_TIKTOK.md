# Clips para TikTok

ClipMax saca material para TikTok de tres formas.

| Qué | Cuándo | Dónde |
|---|---|---|
| **Clips en vivo** | Mientras se graba, a medida que el chat explota | Panel → "Clips para TikTok (en vivo)" y `data\sesiones\<fecha>\clips_vivo\` |
| **Clips de los mejores momentos** | Al cierre, de 8 a 15 elegidos por Claude | Sesión → "Clips para TikTok" y `clips_tiktok\` |
| **Resumen del día para TikTok** | Al cierre, vertical, máximo 4 minutos | Sesión → "Resumen para TikTok" y `resumen_tiktok_<fecha>.mp4` |

## Clips en vivo

Cada minuto, ClipMax mira los mejores momentos que viene detectando (picos de chat, menciones entre streamers). Cuando uno terminó de grabarse (con unos 35 s de margen):

1. Lo transcribe con whisper (subtítulos palabra por palabra).
2. Claude decide si sirve como clip suelto y, si sirve, dónde cortarlo (15–60 s), qué título poner en pantalla, el caption y los hashtags. Usa **Claude Haiku**, unos **$0.005 por clip** (6 por hora durante 8 horas ≈ $0.25 al día). Si no hay API, o se acaba el presupuesto del mes, usa reglas automáticas: corta alrededor del pico del chat y toma el título de lo que se dijo.
3. Lo renderiza en vertical con subtítulos dinámicos y los efectos del director ([ESTILO_EDICION.md](ESTILO_EDICION.md)): zoom al texto que provoca la reacción (el mensaje del chat, el aviso del juego), punch-in a la cara en las emociones y la cara a pantalla completa en la reacción más fuerte.

En el Panel, cada clip tiene su miniatura y los botones **Descargar**, **Copiar caption** (con hashtags) y **subido**. "Subido" es una marca para que sepas cuáles ya publicaste.

- **🎬 clip** en la tabla de candidatos: pide un clip de ese momento aunque ya se haya llegado al tope por hora.
- Los clips que Claude descarta (gameplay sin conversación, etc.) no aparecen en el Panel; la página de la sesión los muestra con el motivo.
- Los procesos de los clips corren con prioridad baja para no quitarle CPU a la grabación. Si el equipo va justo, baja `clips_vivo.max_por_hora`.

Configuración → "Clips para TikTok en vivo": activar/desactivar, usar Claude o no, máximo por hora y duración mínima/máxima.

**Recomendado:** define el recuadro de la cámara de cada streamer (Configuración → tabla de streamers → Cámara). Con eso el formato vertical pone la cara arriba y el juego abajo; sin él, se ve el stream completo al centro con el fondo desenfocado.

## Resumen del día para TikTok

Sigue la ficha "resumen vertical, formato corto". Un **narrador en off** (voz Piper, local y gratis) cuenta el día en frases cortas de 8 a 15 palabras. Cada frase va sobre cortes rápidos del momento que describe. No lleva música.

**Pantalla (1080x1920)**
- El clip 16:9 va centrado a lo ancho (un tercio de la altura) sobre el mismo clip desenfocado.
- **Contadores** en las esquinas superiores del clip (ícono + cifra: muertes, aliados, diamantes…). Los define Claude para cada día.
- **Subtítulos** justo debajo del clip, en bloques de 1 a 3 palabras. La palabra que se dice va en amarillo y un poco más grande. Nombres y cifras siempre en amarillo.
- Opcional: `assets\perfil.png` (tu tarjeta de perfil) va abajo en todo el video, y `assets\logo.png` aparece en la intro.

**Estructura**
1. **Intro** (unos 5 s). La foto del día se revela con un efecto pixel y hace un paneo-zoom lento. El narrador dice las cifras del día ("Día 4: 3 muertes, una alianza rota…"). Los contadores aparecen grandes, con un flash en cada cifra, y luego pasan a las esquinas.
2. **Hechos**, uno tras otro (10 a 18). Cada uno empieza con un conector ("Pero entonces…", "Mientras tanto…") y lleva:
   - cortes cada ~1.6 s de ese momento, con el juego muy bajo bajo la voz;
   - un **flash de color** en la palabra clave: rojo en muertes y explosiones, verde en alianzas y logros, amarillo en anuncios y piques, naranja en traiciones;
   - el **contador sube o baja** justo en esa palabra, con un "pop";
   - en muertes y explosiones, una **ráfaga de micro-cortes** con un **cuadro de impacto** (líneas radiales) en la palabra;
   - en 3 a 6 hechos, una **cita** de 2 a 5 s con la voz real del streamer ("¡NO PUEDE SER!"). La cita lleva punch-ins a la cara y zoom a textos, como en el resumen horizontal.
3. **Cierre**: un flash, un primerísimo plano de la cara (0.25 s), "SÍGUEME" con una flecha animada y un fundido de 3 s.

El audio va comprimido y fuerte (unos -12 LUFS), como en TikTok. El total nunca pasa de `edicion.resumen_tiktok_max_s` (240 s): si se pasa, se quitan primero los hechos de menor prioridad.

**La voz del narrador**
- Se baja con `python arrancar.py descargar --sin-whisper` (voz `es_MX-claude-high`, unos 110 MB, en `models\`). `doctor` avisa si falta.
- Si un nombre suena mal (p. ej. "Westcol" leído en español), pon en Configuración → streamers → `pronunciacion` cómo debe decirlo (`Güéstcol`). Los subtítulos siguen escribiendo el nombre real.
- `edicion.narrador_velocidad`: 0.95 ≈ 150 palabras por minuto; menos es más rápido.
- Sin voz, o con `edicion.tiktok_narrado: false`, el resumen sale como antes: tramos seguidos con el texto en pantalla.

El caption sale en la página de la sesión, con botón para copiarlo. Para rehacerlo sin volver a pagar a Claude: Sesión → paso **editar** → "desde aquí". Las decisiones de antes de esta función no traen narración y salen con el formato de texto en pantalla.
