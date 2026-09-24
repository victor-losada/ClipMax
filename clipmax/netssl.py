"""Certificados HTTPS confiables en Windows.

Python en Windows solo confía en los certificados raíz que ya están guardados en
el almacén de Windows. En equipos o servidores recién instalados ese almacén
viene casi vacío (el navegador descarga las raíces que faltan sobre la marcha,
Python no), y aparecen errores como:

    SSL: CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate

Afecta a todo lo que use el módulo ssl de Python: descargas de modelos, el chat
de Kick (websocket), la API de X. Solución, en este orden:

1. truststore: verifica con el mismo sistema que usa el navegador (CryptoAPI de
   Windows), que sí descarga las raíces que faltan. Viene con el SDK de Anthropic.
2. certifi: si truststore no está, se agrega el paquete de raíces de Mozilla.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_configured: str | None = None


def configure_ssl() -> str:
    """Se llama una vez al arrancar. Devuelve el método usado."""
    global _configured
    if _configured:
        return _configured
    method = "sistema"
    if not os.environ.get("CLIPMAX_SIN_TRUSTSTORE"):
        try:
            import truststore

            truststore.inject_into_ssl()
            method = "truststore"
        except Exception as exc:  # noqa: BLE001 - nunca impedir el arranque por esto
            log.debug("truststore no disponible: %s", exc)
    try:
        import certifi

        # Complementa (no reemplaza) el almacén de Windows con las raíces de Mozilla.
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        if method == "sistema":
            method = "certifi"
    except ImportError:
        pass
    _configured = method
    return method


def certifi_context():
    """Contexto SSL con las raíces de Mozilla (plan B explícito para descargas)."""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()
