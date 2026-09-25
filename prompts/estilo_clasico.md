## Guion

Escribe el resumen como la historia del día, no como una lista de clips:

- Abre con un gancho: una línea de narración que prometa el mejor momento, o directamente el clip más fuerte.
- Agrupa por arcos (el pique principal, las alianzas, el caos del día) y respeta el orden cronológico dentro de cada arco.
- Entre clips, narración de 1 a 3 frases cortas (unas 35 palabras como máximo), con tono de narrador de lore y algo de humor, en español neutro con toques colombianos. Explica lo mínimo necesario para entender el siguiente clip.
- Cierra con un cliffhanger o una pregunta para mañana.
- El chipeo se muestra tal cual, pero la narración no suma insultos, ataques personales ni burlas por lo que alguien es. No inventes datos (puntos, muertes, premios, eliminaciones) que no estén en el material.
- `titulo_en_pantalla`: frase corta en mayúsculas (unas 6 palabras) que se superpone al inicio del clip.

## Efectos (con criterio)

El video lleva subtítulos dinámicos automáticos. Además, en cada clip puedes marcar:

- `momento_clave`: el segundo exacto del remate (la frase que pega, la reacción), en el mismo reloj que `inicio`/`fin` y dentro de ese rango. Ahí entra un zoom suave. Usa 0 si el clip no tiene un remate claro.
- `efecto_sonido`: suena en el `momento_clave`. Disponibles: {{efectos}}. Úsalo solo cuando sume: un golpe en la humillación, una campana cuando alguien suelta algo que no debía. Como máximo {{max_efectos}} en todo el video; lo normal es menos. Deja "" en el resto.
- `pantalla_dividida_con`: si el candidato tiene "Mismo suceso que: candidato N", puedes poner ese N para mostrar a los dos streamers a la par (se escucha solo el audio del clip principal). Úsalo cuando ver la cara del otro al mismo tiempo sea el chiste, por ejemplo en el chipeo en vivo. Usa 0 en los demás.

Aunque este video lleva narración, marca igual `emociones` y `zoom_texto` en cada clip (ver el formato de respuesta): los usan los clips verticales para los zooms a la cara y a los textos. `bloque` va vacío, `tipo` es `narracion` o `clip`, `facecam_completo` vacío, `rotulo` vacío, `conservar_silencios` y `zoom_final` en false y `repeticiones` en 0.
