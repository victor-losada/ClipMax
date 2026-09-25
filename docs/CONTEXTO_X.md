# Contexto del día (X) para Claude

Claude decide qué momentos son lore con tres fuentes: las transcripciones, el chat y **lo que se comenta hoy en X**. X le dice qué historias están vivas (quién le respondió a quién, qué clip se volvió viral, qué alianza se rompió). Sin ese contexto, Claude igual elige bien por el chat y las menciones, pero con él prioriza lo que la comunidad ya está comentando.

## Dónde se pega

**Sesiones → Sesión de hoy → caja "Contexto de X del día" → Guardar contexto.** La sesión de hoy existe aunque todavía no haya empezado la grabación. Puedes pegar varias veces en el día; cada vez reemplaza lo anterior (pega siempre la versión más completa).

Acepta cualquier texto, pero funciona mejor con un post por bloque, separado por una línea en blanco:

```
@cuenta (hora aprox): qué dijo. https://x.com/cuenta/status/123
```

ClipMax guarda el texto completo para Claude, saca los temas repetidos y con ellos sube la puntuación de los candidatos cuya transcripción toca esos temas.

## Opción recomendada: Grok

Grok tiene acceso nativo a X, así que es la mejor herramienta para esto (Claude con búsqueda web encuentra poco de X, porque X bloquea a los buscadores).

1. En la sesión de hoy pulsa **Copiar prompt para Grok**. El prompt ya trae la fecha, la ventana horaria, la pareja principal (Westcol ↔ Gear of Nos), los demás streamers y las cuentas oficiales del evento (configúralas en **Configuración → Contexto de X**, por ejemplo `dedreviil, dedsafio`).
2. Pégalo en Grok (un chat nuevo o el proyecto que ya usas).
3. Copia toda su respuesta y pégala en la caja de ClipMax → **Guardar contexto**.

También puedes obtener el prompt con `python arrancar.py prompt-grok`. Está en `prompts/grok_contexto_x.md` por si quieres ajustarlo.

## Cuándo pegarlo

El post-proceso arranca solo al cierre del día. Tienes dos formas de trabajar:

- **Pegar antes del cierre** (por ejemplo, correr Grok unos 20 minutos antes de terminar): todo sigue automático.
- **Esperar al cierre**: en **Configuración → Contexto de X** activa *"Antes de llamar a Claude, esperar a que pegue el contexto de X del día"*. El proceso graba, detecta, transcribe y puntúa, y se detiene justo antes de llamar a Claude (estado `esperando_contexto`). Cuando pegas el contexto y guardas, **continúa solo**.

Si ya se procesó sin contexto y quieres rehacerlo con contexto, pega y pulsa **"desde aquí"** en el paso *decidir* (es otra llamada a Claude, unos $0.30–0.50 con Opus).

## Continuidad entre días

El contexto de cada día queda guardado en su sesión, y los días siguientes lo reutilizan de dos formas:

- **Grok**: "Copiar prompt para Grok" incluye los *hilos de días anteriores* (el `lore_para_manana` que escribió Claude o, si ese día no hubo decisión, los PIQUES que trajo Grok). Grok busca cómo siguen y los reporta en una sección nueva, **CONTINUACIONES**.
- **Claude**: recibe la *Historia de días anteriores* (últimos 3 días: su lore y un resumen de lo que se decía en X: PIQUES, CONTINUACIONES, MOMENTOS CLIPEADOS y TEMAS) y la lista de *temas de hoy que ya venían de antes* (por ejemplo, `juicio (09-23)`). Con eso hace callbacks ("ayer…; hoy…") y prioriza lo que continúa un hilo.

No tienes que hacer nada extra: basta con pegar el contexto cada día. Los nombres del evento y de los streamers no cuentan como "tema que continúa", porque salen todos los días.

## Otras opciones

| Modo (`x.modo`) | Costo | Cuándo usarlo |
|---|---|---|
| `manual` (por defecto) | $0 | Pegas lo de Grok o posts sueltos. |
| `claude_web` | ~$0.05–0.30 por día de API | Claude busca en la web al procesar. Automático, pero ve poco de X. |
| `api` | Plan de lectura de la API de X | Solo si tienes un token con lectura. |

Grok por API (xAI) también existe, pero es de pago por uso y aparte de tu plan de Grok, así que no está integrado. El flujo de copiar y pegar usa el plan que ya pagas.
