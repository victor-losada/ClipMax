# Efectos de sonido propios

Pon aquí archivos `.wav`, `.mp3`, `.ogg`, `.m4a` o `.flac`. El nombre del archivo (sin extensión, en minúsculas) es el nombre del efecto; por ejemplo, `bruh.mp3` → `bruh`.

- Claude recibe la lista de efectos disponibles y elige cuál suena en el remate de cada clip (con tope por video).
- Un archivo con el mismo nombre que uno incluido (`boom`, `whoosh`, `ding`, `pop`, `impacto`) lo reemplaza.
- Usa sonidos cortos (menos de 2 s) y con licencia libre para redes, para evitar reclamos de copyright en TikTok o YouTube.

Los efectos incluidos se generan con ffmpeg la primera vez (no son grabaciones con derechos de autor) y se guardan en `data/sfx_generados/`.
