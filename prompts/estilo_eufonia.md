## Estilo de edición: montaje coral con las voces al frente

La historia la cuentan las voces de los streamers; no hay narrador. Formato 16:9 completo, con la interfaz del juego y los widgets del stream visibles. Cortes secos, sin transiciones. Sin música.

### Estructura
Marca cada elemento del guion con su `bloque`. Las proporciones son sobre la duración total; si el día no tiene material para un bloque (por ejemplo, no hay un clímax claro), sáltalo, no lo fuerces.

1. `gancho` (~1 %): un elemento de tipo `gancho` al principio: una frase gritada o impactante de 1 a 3 segundos (`inicio`/`fin` justo alrededor de la frase, con los tiempos de su transcripción) que se repite `repeticiones` veces (2 o 3) en arranque en frío. Después, el editor pone solo un sting de 4 s con el título del día.
2. `premisa` (~3 %): el diálogo que plantea el conflicto del día.
3. `cuerpo` (~70 %): episodios de 30 a 90 s. Dentro de un episodio alterna perspectivas del mismo suceso: clips seguidos de distintos streamers, usando los candidatos "Mismo suceso", cambiando cada 20 a 60 s siguiendo a quien habla. Elige el `inicio` del siguiente clip leyendo su transcripción para que la conversación siga sin repetir ni saltar frases. Los cambios van más rápidos al principio (unos 3 por minuto) y más lentos hacia el final (unos 2 por minuto).
4. `subida` (~6 %): un momento con presión: cuenta atrás, persecución, algo que se viene.
5. `pausa` (~2 %): 15 a 20 s casi sin voz antes del clímax. Marca `conservar_silencios: true`.
6. `climax` (~9 %): lo más fuerte del día, con gritos. Casi nada de texto en pantalla.
7. `desenlace` (~9 %): las consecuencias, un clip por evento, cada uno con su `rotulo`: "ETIQUETA · NOMBRE", máximo 30 caracteres (por ejemplo "ELIMINADO · WESTCOL" o "ALIANZA ROTA · GIRLOFNOX").
8. `cierre` (~2 %): un último momento tranquilo o un cliffhanger. El editor agrega la pantalla final.

### Narración
Como máximo 2 o 3 elementos `narracion` en todo el video, solo donde sin contexto no se entendería lo que sigue (un cambio de escena, un salto de tiempo, quién es quién). Máximo 25 palabras cada una, con tono de narrador de lore y algo de humor. El chipeo se muestra tal cual, pero la narración no suma insultos ni burlas por lo que alguien es, y no inventa datos que no estén en el material.

### Emociones y textos (dónde hace zoom el editor)
El editor acerca la imagen a la cara del streamer cuando hay emoción y, antes, al texto que la provocó. Tú lees la transcripción y ves el chat; márcalo en cada clip:

- `emociones`: los segundos donde el streamer se queja, se asusta, se enoja, se ríe fuerte, se sorprende o grita. `tipo`: queja | susto | rabia | risa | sorpresa | grito. `texto`: las 1 a 4 palabras literales que dice en ese instante (salen como caption). En discusiones y rabietas, una cada 7 segundos más o menos; ninguna en tramos tranquilos.
- `zoom_texto`: cuando la reacción la provoca un texto en pantalla, como un mensaje del chat que lee o comenta, un aviso del juego (una muerte, una eliminación), un título o una cuenta atrás. `t`: cuando aparece o cuando empieza a leerlo. `zona`: `chat` (el chat del stream), `juego` (el chat de Minecraft: muertes y avisos), `centro` (títulos grandes del juego) o `arriba` (barras y cuenta atrás). `texto`: qué dice, si lo sabes.
- `facecam_completo`: en la reacción más fuerte de un episodio, la cara a pantalla completa de 3 a 8 s (`inicio`/`fin`). Úsalo poco: unas 3 a 5 veces en todo el video.
- `zoom_final: true` al cierre de un monólogo intenso: un zoom continuo que termina en el corte.
- `momento_clave`: el remate del clip.
- `efecto_sonido`: casi nunca; los golpes ya son los gritos y los sonidos del juego. Como mucho, un golpe seco en 2 a 4 cambios de bloque. Disponibles: {{efectos}}.
- `pantalla_dividida_con`: en 0; en este estilo se alternan perspectivas en vez de dividir la pantalla.
- `titulo_en_pantalla`: vacío; en este estilo los únicos rótulos son los del desenlace.

### Ritmo
La voz es el motor: el editor recorta los silencios de más de 1,5 s, salvo donde pidas `conservar_silencios`. Evita tramos sin conversación y dale a cada clip una `prioridad` de 1 a 5 como siempre.
