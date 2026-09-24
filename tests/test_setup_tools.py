"""Descargador: reintento ante CERTIFICATE_VERIFY_FAILED, resumen de fallos y no repetir descargas."""

import ssl
import urllib.error

from clipmax import netssl, setup_tools


def _cert_error():
    return urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED unable to get local issuer"))


def test_cert_error_retries_with_certifi(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(url, dest, context):
        calls.append(context)
        if context is None:
            raise _cert_error()
        dest.write_bytes(b"ok")
        return dest

    monkeypatch.setattr(setup_tools, "_fetch", fake_fetch)
    out = setup_tools._download("https://huggingface.co/x.bin", tmp_path / "x.bin")
    assert out.read_bytes() == b"ok"
    assert calls[0] is None and isinstance(calls[1], ssl.SSLContext)


def test_failures_do_not_stop_other_downloads(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_tools, "MODELS", tmp_path)

    def fake_fetch(url, dest, context):
        if "base" in url:
            raise _cert_error()
        dest.write_bytes(b"model")
        return dest

    monkeypatch.setattr(setup_tools, "_fetch", fake_fetch)
    ok = setup_tools.run(["base", "small"], ffmpeg=False, cuda=False, vad=False, skip_whisper=True)
    out = capsys.readouterr().out
    assert ok is False
    assert (tmp_path / "ggml-small.bin").exists() and not (tmp_path / "ggml-base.bin").exists()
    assert "1 descarga(s) fallaron" in out and "ggml-base.bin" in out and "guardar como" in out
    assert "Traceback" not in out


def test_existing_files_are_not_downloaded_again(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_tools, "MODELS", tmp_path / "models")
    monkeypatch.setattr(setup_tools, "BIN", tmp_path / "bin")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "ggml-base.bin").write_bytes(b"x")
    exe = tmp_path / "bin" / "whisper" / "Release" / "whisper-cli.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    monkeypatch.setattr(setup_tools, "_fetch", lambda *a: (_ for _ in ()).throw(AssertionError("no debería descargar")))
    assert setup_tools.run(["base"], ffmpeg=False, cuda=False, vad=False, skip_whisper=False) is True


def test_configure_ssl_uses_truststore():
    assert netssl.configure_ssl() in ("truststore", "certifi")
    ctx = ssl.create_default_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
