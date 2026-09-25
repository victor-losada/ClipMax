"""Detalles específicos de Windows.

- Evitar que el PC entre en suspensión mientras se graba (8 horas desatendidas).
- Lanzar procesos sin abrir ventanas de consola.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# Flags para subprocess: sin ventana de consola y en su propio grupo de procesos
# (así un Ctrl+C en la consola de ClipMax no mata a ffmpeg antes de cerrarlo bien).
if IS_WINDOWS:
    POPEN_FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
else:
    POPEN_FLAGS = 0

_ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake(threading.Thread):
    """Mientras esté activo, reinicia cada 30 s el temporizador de suspensión de Windows.

    SetThreadExecutionState está ligado al hilo que lo llama, por eso vive en su
    propio hilo de larga duración en lugar de llamarse desde hilos de la web.
    La pantalla sí puede apagarse; lo que no se suspende es el equipo.
    """

    def __init__(self):
        super().__init__(name="keep-awake", daemon=True)
        # Ojo: no llamar a estos atributos _stop/_started (pisan internos de threading.Thread).
        self._on = threading.Event()
        self._halt = threading.Event()

    def set_active(self, active: bool) -> None:
        if active and not self._on.is_set():
            log.info("Suspensión de Windows bloqueada mientras dure la grabación")
        if not active and self._on.is_set():
            log.info("Suspensión de Windows restaurada")
        (self._on.set if active else self._on.clear)()

    def run(self) -> None:
        if not IS_WINDOWS:
            return
        import ctypes

        while not self._halt.wait(30):
            if self._on.is_set():
                try:
                    ctypes.windll.kernel32.SetThreadExecutionState(_ES_SYSTEM_REQUIRED)
                except Exception as exc:  # noqa: BLE001
                    log.warning("SetThreadExecutionState falló: %s", exc)

    def stop(self) -> None:
        self._halt.set()


KEEP_AWAKE = KeepAwake()
