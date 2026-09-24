# Instalación paso a paso en Windows 10/11

Tiempo estimado: 20–30 minutos (la mayor parte son descargas).

## Requisitos

- Windows 10 u 11 de 64 bits.
- **Python 3.12** (sirve 3.10 o superior).
- Espacio en disco: ≈ 15 GB por streamer y por día a 720p (ver la tabla del final).
- Recomendado: 8 núcleos de CPU y 16 GB de RAM si vas a transcribir en vivo a la pareja principal. Con GPU NVIDIA todo va mucho más rápido.
- Una clave de API de Anthropic (opcional si usas el modo manual con claude.ai).

## 1. Instalar Python

1. Descarga Python 3.12 desde https://www.python.org/downloads/windows/
2. En el instalador marca **"Add python.exe to PATH"** y pulsa *Install Now*.
3. Comprueba en una terminal nueva (tecla Windows → escribe `cmd`):
   ```
   py --version
   ```

## 2. Descargar ClipMax

- Con Git: `git clone <url-del-repo> C:\ClipMax`
- Sin Git: descarga el ZIP del repositorio y descomprímelo en `C:\ClipMax`.

Evita rutas dentro de OneDrive, porque la sincronización interfiere con las grabaciones.

## 3. Instalación automática

Haz doble clic en **`instalar.bat`**. Este script:

1. Crea un entorno virtual `.venv` e instala las dependencias de `requirements.txt`.
2. Instala **ffmpeg** con `winget` (si no hay winget, lo descarga en `bin\ffmpeg`).
3. Descarga **whisper.cpp** (`whisper-cli.exe`) en `bin\whisper` y los modelos `ggml-base.bin` (≈ 150 MB) y `ggml-small.bin` (≈ 480 MB) en `models\`.
4. Crea `config.yaml` y `.env` a partir de los ejemplos.
5. Ejecuta `python -m clipmax doctor` para verificar.

> Si instaló ffmpeg con winget, **cierra y vuelve a abrir** la terminal para que Windows actualice el PATH.

### Instalación manual (si el .bat falla)

```bat
cd C:\ClipMax
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
winget install --id Gyan.FFmpeg -e
python -m clipmax descargar --modelo base small
copy config.example.yaml config.yaml
copy .env.example .env
```

`python arrancar.py <comando>` equivale a `python -m clipmax <comando>`, y además verifica y corrige el nombre de la carpeta del código. Es lo que usan los `.bat`.

Si `descargar` no puede bajar whisper.cpp: entra a https://github.com/ggml-org/whisper.cpp/releases, busca la versión más reciente que traiga `whisper-bin-x64.zip` y descomprímelo en `C:\ClipMax\bin\whisper\`. Los modelos están en https://huggingface.co/ggerganov/whisper.cpp/tree/main (van en `models\`).

**GPU NVIDIA (opcional):**
```bat
python -m clipmax descargar --cuda --modelo base small
```
Para edición acelerada, pon `codec: h264_nvenc` en la configuración (en Intel es `h264_qsv` y en AMD, `h264_amf`).

## 4. Clave de Claude

1. Crea una clave en https://console.anthropic.com/ → *API Keys*. **La API se factura aparte del plan de claude.ai**: carga saldo en *Billing*. Para no pasarte, ClipMax respeta el tope mensual que configures (por defecto $10).
2. Abre `C:\ClipMax\.env` con el Bloc de notas y pega la clave:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
3. Si prefieres no usar la API, en Configuración elige **Claude → Manual (pegar en claude.ai)**. El costo de API es $0: ClipMax te da un texto para pegar en claude.ai y tú pegas la respuesta.

## 5. Primer arranque

Doble clic en **`iniciar.bat`**. Se abre el navegador en http://127.0.0.1:5000

1. **Configuración → Streamers**: revisa las URL de Kick. ⚠ El ejemplo trae `kick.com/gearofnos`, que **no existe**: abre el canal real de Gear of Nos en Kick, copia su URL y selecciónalo como pareja principal.
   - *Voz en vivo*: actívalo solo en Westcol y Gear of Nos.
   - *Modo*: `juego + cara` (stream completo) o `solo cara`.
2. **Evento**: hora de inicio (15:00 por defecto), duración y días.
3. **Guardar**.
4. Cuando un streamer esté en vivo, entra a **Cámara → definir**, pulsa *Tomar captura* y arrastra un recuadro sobre su cámara. Solo hace falta para `solo cara` y para el formato vertical.
5. Verifica todo con:
   ```bat
   .venv\Scripts\activate
   python -m clipmax doctor
   ```
   `doctor` comprueba ffmpeg, whisper, los modelos, la clave, la zona horaria, el espacio en disco y que **cada canal de Kick exista**.

## 6. Probar sin esperar al evento

```bat
python -m clipmax demo
```
Genera dos streams sintéticos con chat, menciones y chipeo, y produce un resumen, clips de TikTok y el reporte en `data_demo\sesiones\<fecha>\`. No gasta API. Con `--con-claude` usa la API real (unos centavos).

## 7. Día del evento

- Deja **`iniciar.bat` abierto** (puedes minimizarlo). A las 15:00 empieza a grabar solo y al cierre procesa todo.
- ClipMax impide que Windows entre en suspensión mientras graba (la pantalla sí puede apagarse). Revisa además que el plan de energía no apague los discos.
- Durante el día puedes pegar posts de X en **Sesiones → hoy → Contexto de X**.
- Si cierras ClipMax a mitad del evento y lo vuelves a abrir dentro del horario, retoma la grabación.
- Resultados en `data\sesiones\<fecha>\`:
  - `resumen_<fecha>.mp4`: el video de 10–20 min.
  - `resumen_<fecha>.md` / `.html`: mejores momentos, por qué importan y captions.
  - `clips_tiktok\*.mp4`: clips verticales de los mejores momentos.
  - `grabaciones\<fecha>_<streamer>.mp4`: un MP4 por streamer.

## 8. Comandos útiles

| Comando | Qué hace |
|---|---|
| `python -m clipmax web` | Interfaz + horario automático (lo que hace `iniciar.bat`). |
| `python -m clipmax grabar --horas 2` | Graba ya, sin interfaz. |
| `python -m clipmax procesar --fecha 2026-09-23` | Re-procesa un día completo. |
| `python -m clipmax procesar --desde editar` | Re-edita sin volver a llamar a Claude. |
| `python -m clipmax exportar-paquete` | Paquete para pegar en claude.ai. |
| `python -m clipmax importar-respuesta respuesta.json` | Importa la respuesta de claude.ai y renderiza. |
| `python -m clipmax prompt-maestro` | Muestra el prompt maestro con tu configuración. |
| `python -m clipmax snapshot westcol` | Captura del directo para calibrar la cámara. |
| `pip install -U yt-dlp` | Actualiza yt-dlp si Kick cambia algo. |

## 9. Espacio en disco

| Calidad | GB por hora y streamer | Día de 8 h, 2 streamers | Día de 8 h, 20 streamers |
|---|---|---|---|
| 1080p | ≈ 3.6 | ≈ 58 GB | ≈ 580 GB |
| 720p | ≈ 1.8 | ≈ 29 GB | ≈ 290 GB |
| 480p | ≈ 0.9 | ≈ 15 GB | ≈ 145 GB |

Con decenas de streamers, graba a la pareja principal en 720p y desactiva a los secundarios o baja la calidad. Borra o mueve las grabaciones de días anteriores cuando ya tengas el resumen.

## 10. Problemas frecuentes

| Síntoma | Solución |
|---|---|
| `No module named clipmax` | La carpeta del código debe llamarse `clipmax` **en minúsculas** y estar en `C:\ClipMax\clipmax\__main__.py`. Windows no distingue mayúsculas, pero Python sí. `instalar.bat` e `iniciar.bat` (vía `arrancar.py`) la renombran solos; a mano: `ren ClipMax clipmax_tmp` y luego `ren clipmax_tmp clipmax`. |
| `CERTIFICATE_VERIFY_FAILED` / `unable to get local issuer certificate` | Windows (sobre todo servidores recién instalados) no tiene aún el certificado raíz del sitio y Python no lo descarga solo. ClipMax usa `truststore` (valida como el navegador) y, si no alcanza, reintenta con `certifi`. Si una descarga sigue fallando, el instalador muestra el enlace y la ruta exacta para bajarla con el navegador. |
| `Kick respondió 403` | Actualiza: `pip install -U "yt-dlp[default,curl-cffi]"`. Revisa la VPN o el firewall. |
| El chat no conecta o marca 0 msg/min | Kick pudo cambiar la clave de Pusher: busca la nueva y agrégala en `chat.pusher_keys` (Configuración → YAML). También puedes fijar `chatroom_id` a mano. |
| "No encuentro whisper-cli" | `python -m clipmax descargar` o descomprime el zip en `bin\whisper`. |
| La transcripción en vivo se atrasa | Usa `models/ggml-tiny.bin` como `modelo_vivo`, sube `hilos` o usa la versión CUDA. |
| El resumen queda corto | Es normal los días con poco chipeo: Claude no rellena con gameplay. Sube `candidatos_max` o baja `umbral_z`. |
| "Presupuesto insuficiente" | Se alcanzó el tope del mes: usa el paquete manual (Sesión → Claude) o sube el tope. |
