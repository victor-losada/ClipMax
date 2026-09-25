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

Claude elige de 4 a 10 tramos cortos (8–40 s) que cuentan el día: primero el momento más fuerte (gancho) y luego en orden. No lleva tarjetas de narración: el texto en pantalla de cada tramo cuenta la historia. El total nunca pasa de `edicion.resumen_tiktok_max_s` (240 s por defecto). El caption sale en la página de la sesión, con botón para copiarlo.

Para rehacerlo sin volver a pagar a Claude: Sesión → paso **editar** → "desde aquí". Si la decisión es de antes de esta función, el resumen se arma solo a partir de los mejores momentos.
