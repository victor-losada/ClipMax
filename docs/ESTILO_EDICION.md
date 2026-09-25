# Estilo de edición "Eufonía" (por defecto)

El resumen diario se arma como un montaje coral. La historia la cuentan las voces de los streamers: sin narrador (como máximo 2 o 3 tarjetas), sin música y sin transiciones. Hay zooms a la cara cuando hay emoción y al texto que la provocó. Se basa en la ficha de referencia de estilo (solo la gramática de edición, no el contenido ni la marca).

`edicion.estilo: "clasico"` vuelve al formato anterior: tarjetas de narración entre clips y subtítulos continuos.

## Estructura del resumen
Claude marca cada parte del guion con su bloque y el editor aplica las reglas de cada uno:

| Bloque | Qué pasa |
|---|---|
| **gancho** | Arranque en frío: una frase gritada de 1–3 s, repetida 2–3 veces con zoom creciente a la cara. El editor la recorta a las palabras más fuertes. |
| **sting** | 4 s del primer clip con la voz abajo y el título del día en fuente pixel (lo agrega el editor). |
| **premisa, cuerpo** | Episodios de 30–90 s alternando perspectivas del mismo suceso según quién habla. |
| **subida** | Presión: cuenta atrás, persecución. |
| **pausa** | 15–20 s casi sin voz antes del clímax (no se recortan silencios). |
| **clímax** | Lo más fuerte, a más volumen y sin texto en pantalla. |
| **desenlace** | Las consecuencias, con rótulo pixel abajo a la derecha ("ELIMINADO · WESTCOL"). |
| **cierre** | Un momento tranquilo y la pantalla final "SUSCRÍBETE" en pixel. |

- **Ritmo:** se recortan los silencios de más de 1,5 s (salvo en las pausas pedidas).
- **Volumen:** crece hacia el clímax y baja en el cierre.

## Dónde hace zoom (el "director" de efectos)
Para cada clip, `clipmax/style.py` junta cuatro señales:

1. **Claude**, que lee la transcripción y marca emociones (queja, susto, rabia, risa, sorpresa, grito) y los textos que provocan reacciones.
2. **El volumen**: los gritos, comparados con el resto del mismo clip.
3. **Un léxico** de exclamaciones, insultos, risas y repeticiones ("no no no").
4. **El chat**: si el streamer lee en voz alta un mensaje del chat, el sistema lo reconoce.

Con eso decide:

- **Zoom al texto que provoca la reacción:** el mensaje del chat que lee, un aviso del juego (muerte, eliminación: chat de Minecraft abajo a la izquierda), un título o una cuenta atrás. Se acerca ~2 s justo antes de la reacción.
- **Punch-in a la cara:** 1,3–1,5× durante 2–4 s, anclado a la esquina de la cámara para que la cara crezca sin salirse. Como mucho uno cada ~4 s y ninguno en pausas.
- **Cara a pantalla completa** en la reacción más fuerte de un tramo (3–6 s). Si la cámara no tiene la proporción del video, se muestra entera con fondo desenfocado, así nunca se corta la cara.
- **Remate de un monólogo:** zoom continuo hasta 2× que termina en el corte.
- **Captions** (solo en el resumen horizontal): 1–4 palabras literales, sincronizadas con la palabra, en fuente redondeada con contorno y **un color fijo por streamer**. Solo en frases clave o gritadas y nunca en el clímax.

Esto mismo (zoom a textos, punch-ins y cara completa) se aplica a los **clips en vivo**, a los **clips de TikTok** y al **resumen diario de TikTok**. En esos videos se mantienen los subtítulos dinámicos palabra por palabra.

## Lo que tienes que configurar (una vez por streamer)
Configuración → tabla de streamers → **Cámara**:

- **Cámara:** el recuadro de la cara. Sin él no hay punch-ins ni cara completa; solo un zoom suave en el remate.
- **Chat en pantalla:** si el streamer muestra su chat en el stream, marca dónde. Así el video puede acercarse al mensaje que lo hizo reaccionar.
- **Color** de sus captions (opcional, `streamers[].color: "#FFD400"`). Si no lo pones, se usa una paleta: amarillo y naranja para la pareja principal, y luego rosa, verde, cian…

**Ojo:** si un streamer cambia de escena en OBS y su cámara se mueve de lugar, en esa escena el zoom apuntará al lugar marcado. Marca el recuadro en la escena que usa durante el evento.

## Fuentes
En `assets/fonts/`, libres (licencia OFL, incluida): **Titan One** (captions y tarjetas) y **Press Start 2P** (rótulos, sting y pantalla final).
