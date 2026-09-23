"""ClipMax: captura y edición automática de clips del Desafío 4 (Kick).

Paquetes principales:
    config      -> carga/validación de config.yaml
    db          -> SQLite (sesiones, grabaciones, chat, transcripciones, momentos)
    recorder    -> grabación de streams de Kick con ffmpeg (partes .ts -> MP4)
    chat        -> lector del chat de Kick (Pusher websocket)
    transcriber -> whisper.cpp (en vivo para la pareja principal y por ventana)
    detector    -> picos de chat, menciones cruzadas, sincronía y temas de X
    brain       -> Claude decide qué es lore, escribe guion y cortes
    editor      -> ffmpeg corta, limpia silencios y une el resumen diario
    report      -> documento con mejores momentos y captions para TikTok
    pipeline    -> orquesta los pasos de post-proceso
    scheduler   -> arranque/cierre automático por horario
    web         -> interfaz local en Flask
"""

__version__ = "1.0.0"
