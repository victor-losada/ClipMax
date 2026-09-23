# ClipMax · clips automáticos del Desafío 4 (Kick)

ClipMax graba a los streamers del **Desafío 4** a la hora del evento, detecta sin modelos entrenados los momentos con más hype (picos de chat, menciones cruzadas y lo que se comenta en X) y deja que **Claude** decida qué es *lore*: sobre todo el **chipeo entre Westcol y Gear of Nos**. Con eso escribe el guion y los cortes, y **ffmpeg** monta un resumen diario de 10–20 minutos más un documento con los mejores momentos y captions para TikTok.

Todo corre local y gratis (ffmpeg, yt-dlp, whisper.cpp, SQLite, Flask). Lo único que se paga es la API de Claude, con un tope mensual (por defecto $10), o puedes usar el **modo manual**, que genera un texto para pegar en claude.ai y no gasta API.

```
15:00 ─► graba N streams (MP4 por streamer) + lee sus chats + escucha a la pareja en vivo
cierre ─► candidatos puntuados ─► whisper ─► Claude (guion + cortes) ─► ffmpeg ─► resumen + reporte + clips TikTok
```

## Inicio rápido (Windows)

1. Instala **Python 3.12** marcando "Add python.exe to PATH".
2. Doble clic en **`instalar.bat`** (dependencias, ffmpeg, whisper.cpp y modelos).
3. Pega tu `ANTHROPIC_API_KEY` en `.env`, o elige el modo manual.
4. Doble clic en **`iniciar.bat`**: se abre http://127.0.0.1:5000. Configura streamers y horario.
5. Prueba sin esperar al evento: `python -m clipmax demo`

Guía completa: [docs/INSTALACION_WINDOWS.md](docs/INSTALACION_WINDOWS.md)

## Documentación

- [Arquitectura y plan de implementación](docs/ARQUITECTURA.md): módulos, algoritmo de detección, costos, decisiones y riesgos.
- [Instalación paso a paso en Windows](docs/INSTALACION_WINDOWS.md)
- [Prompt maestro y flujo diario con claude.ai](docs/PROMPT_MAESTRO.md). El prompt está en [`prompts/prompt_maestro.md`](prompts/prompt_maestro.md).
- [Configuración de ejemplo comentada](config.example.yaml)

## Estructura

```
clipmax/
  config.py        configuración validada (config.yaml)
  db.py            SQLite: sesiones, partes, chat, transcripciones, señales, momentos, costos
  kick.py          API de Kick (curl_cffi pasa Cloudflare) + selección de variante HLS
  recorder.py      grabación ffmpeg -c copy a .ts por partes -> MP4 por streamer
  chat.py          chat de Kick por Pusher: actividad, hype, menciones
  mentions.py      alias con límites de palabra + coincidencia difusa
  transcriber.py   whisper.cpp en vivo (pareja) y por candidato
  xcontext.py      contexto de X: manual / API / búsqueda web de Claude
  detector.py      picos (z-score robusto), menciones, sincronía -> momentos
  prompts.py       material del día + esquema JSON de salida
  brain.py         Claude: salida estructurada, fallback, presupuesto, modo manual
  editor.py        ffmpeg: silencios, encuadres, títulos, tarjetas, concat
  cards.py tts.py  gráficos (Pillow) y voz local opcional
  report.py        resumen .md/.html con captions
  pipeline.py      post-proceso por pasos, reanudable
  scheduler.py     horario, sesión en vivo, anti-suspensión, reanudación
  web/             interfaz Flask
  demo.py          demo de punta a punta con datos sintéticos
prompts/prompt_maestro.md
tests/             46 pruebas (incluye grabación y render reales con ffmpeg)
```

## Pruebas

```bat
.venv\Scripts\activate
pip install pytest
python -m pytest -q
```
