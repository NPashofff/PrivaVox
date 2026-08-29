"""Localhost-only ASR sidecar exposing an OpenAI-compatible HTTP API.

Wraps flow.stt / flow.audio (mlx-whisper large-v3-turbo, en/bg-constrained
auto-detect, RMS silence gate, hallucination filter) behind the same HTTP
shape openless's WhisperBatchASR client speaks:

    POST /v1/audio/transcriptions   multipart/form-data: file, model,
                                     language, response_format
    GET  /health

This lets any OpenAI-audio-transcriptions-compatible client (openless,
curl, etc.) use our fully-local pipeline by pointing base_url at
http://127.0.0.1:<port>/v1 with any dummy api_key.

Implementation notes (see docs/phase2-sidecar.md for the full writeup):
  - stdlib only: http.server.ThreadingHTTPServer + a small hand-rolled
    multipart/form-data parser (the cgi module's FieldStorage is deprecated
    since 3.11/removed in 3.13, and the request shape here is simple enough
    — a handful of text fields plus one file part — that a ~40-line parser
    is easier to reason about than pulling in Flask).
  - Never write the uploaded audio to disk: it is piped directly into
    `ffmpeg -i pipe:0 -f f32le -ac 1 -ar 16000 pipe:1` via subprocess, and
    the stdout bytes are viewed as a float32 numpy array — no temp files.
  - Inference is single-flight: one global lock serializes calls into
    flow.stt.transcribe (mlx's model/cache state is not assumed thread-safe).
    Concurrent requests queue and are served in arrival order.
  - Gates match the daemon path exactly: RMS silence gate
    (flow.audio.is_silence) then flow.stt.is_hallucination() on the raw
    Whisper output. Gated audio returns {"text": ""} with HTTP 200 — it is
    not treated as an error, mirroring flow.__main__.run_pipeline. The LLM
    cleanup stage (flow.cleanup) is intentionally NOT applied here: OpenAI's
    /v1/audio/transcriptions contract is "raw transcription in, text out",
    and the sidecar is meant to be a drop-in ASR provider, not the whole
    Flow pipeline.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

from . import audio as audio_mod
from . import stt
from .config import DEFAULT, FlowConfig
from .dictionary import load_configured_dictionary

FFMPEG_TIMEOUT_S = 30.0


class ApiError(Exception):
    """Carries an OpenAI-style error payload + HTTP status to the handler."""

    def __init__(self, status: int, message: str, error_type: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_type = error_type


# --- Minimal multipart/form-data parsing ------------------------------------
#
# We only need what browsers/HTTP clients actually send for this endpoint:
# a handful of scalar text fields (model, language, response_format) and one
# file field. RFC 2046 / RFC 7578 in full is much bigger than that; this
# parser covers exactly the subset OpenAI-style transcription clients use.


class MultipartField:
    __slots__ = ("name", "filename", "content_type", "data")

    def __init__(self, name: str, filename: str | None, content_type: str | None, data: bytes) -> None:
        self.name = name
        self.filename = filename
        self.content_type = content_type
        self.data = data


def _parse_content_type(header: str) -> tuple[str, dict[str, str]]:
    parts = header.split(";")
    main = parts[0].strip()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, _, value = part.strip().partition("=")
        params[key.strip().lower()] = value.strip().strip('"')
    return main, params


def parse_multipart(body: bytes, boundary: str) -> list[MultipartField]:
    """Parse a multipart/form-data body into a list of fields, in order."""
    boundary_bytes = ("--" + boundary).encode("utf-8")
    # Split on the boundary marker; the first and last chunks are preamble/epilogue.
    chunks = body.split(boundary_bytes)
    fields: list[MultipartField] = []
    for chunk in chunks:
        if not chunk or chunk in (b"--\r\n", b"--"):
            continue
        # Each part: leading CRLF, headers, blank line, data, trailing CRLF.
        part = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        if part.endswith(b"\r\n"):
            part = part[:-2]
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        header_blob = part[:header_end].decode("utf-8", errors="replace")
        data = part[header_end + 4 :]

        name: str | None = None
        filename: str | None = None
        content_type: str | None = None
        for line in header_blob.split("\r\n"):
            if not line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "content-disposition":
                _, params = _parse_content_type(value)
                name = params.get("name")
                filename = params.get("filename")
            elif key == "content-type":
                content_type = value
        if name is None:
            continue
        fields.append(MultipartField(name, filename, content_type, data))
    return fields


# --- ffmpeg decode ------------------------------------------------------------


def decode_audio_bytes(raw: bytes, config: FlowConfig = DEFAULT) -> np.ndarray:
    """Decode an arbitrary audio container to 16 kHz mono float32 via ffmpeg.

    Pipes `raw` into ffmpeg's stdin and reads raw f32le samples from stdout —
    no temp files. Raises ApiError(415, ...) if ffmpeg can't decode it (bad
    upload, empty file, unsupported/corrupt container).
    """
    if not raw:
        raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "uploaded file is empty", "invalid_request_error")
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-i", "pipe:0",
        "-f", "f32le",
        "-ac", str(config.channels),
        "-ar", str(config.sample_rate),
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=raw,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "audio decode timed out", "invalid_request_error") from e
    except FileNotFoundError as e:
        raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "ffmpeg is not installed / not on PATH", "server_error") from e

    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", errors="replace")[-500:]
        raise ApiError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            f"could not decode uploaded audio: {detail.strip() or 'ffmpeg failed'}",
            "invalid_request_error",
        )
    return np.frombuffer(proc.stdout, dtype=np.float32)


# --- Inference (single-flight) ------------------------------------------------

_inference_lock = threading.Lock()
_model_warm = False
_warm_lock = threading.Lock()


def ensure_warm(config: FlowConfig = DEFAULT) -> None:
    global _model_warm
    if _model_warm:
        return
    with _warm_lock:
        if _model_warm:
            return
        with _inference_lock:
            stt.warm_up(config)
        _model_warm = True


def run_transcription(
    audio: np.ndarray, language: str | None, config: FlowConfig = DEFAULT
) -> dict[str, Any]:
    """Gate -> STT -> hallucination filter. Mirrors flow.__main__.run_pipeline
    minus the LLM cleanup stage (out of scope for a raw-transcription API).

    Returns a dict with text/language/detected_language/duration/gated.
    """
    duration = audio_mod.duration_s(audio, config.sample_rate)

    if audio_mod.is_silence(audio, config.energy_threshold):
        return {
            "text": "",
            "language": language or config.language_mode,
            "detected_language": None,
            "duration": duration,
            "gated": "silence",
        }

    with _inference_lock:
        result = stt.transcribe(audio, language=language, config=config)

    if stt.is_hallucination(result["text"]):
        return {
            "text": "",
            "language": result["language"],
            "detected_language": result["detected_language"],
            "duration": duration,
            "gated": "hallucination",
        }

    return {
        "text": result["text"],
        "language": result["language"],
        "detected_language": result["detected_language"],
        "duration": duration,
        "gated": None,
    }


def _normalize_language(language: str | None, config: FlowConfig) -> str | None:
    """Map an arbitrary incoming ISO code to something flow.stt understands.

    en/bg are honored and forced; anything else (unset, unknown code, "auto")
    falls back to the configured auto-detect mode.
    """
    if not language:
        return None
    lang = language.strip().lower()
    if lang in config.supported_languages:
        return lang
    return None  # unknown code -> auto (config.language_mode)


# --- HTTP handler --------------------------------------------------------------


class TranscriptionHandler(BaseHTTPRequestHandler):
    server_version = "FlowASRSidecar/0.1"
    config: FlowConfig = DEFAULT

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write(f"[flow.server] {self.address_string()} - {fmt % args}\n")

    # -- helpers ---------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, err: ApiError) -> None:
        self._send_json(err.status, {"error": {"message": err.message, "type": err.error_type}})

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path.rstrip("/") == "" or self.path == "/health":
                ensure_warm(self.config)
                self._send_json(
                    HTTPStatus.OK, {"status": "ok", "model": self.config.stt_model_repo}
                )
                return
            self._send_error(ApiError(HTTPStatus.NOT_FOUND, "not found", "invalid_request_error"))
        except Exception as e:  # keep the server alive on any per-request error
            self._send_error(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error: {e!r}", "server_error"))

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path.rstrip("/") == "/v1/audio/transcriptions":
                self._handle_transcription()
                return
            self._send_error(ApiError(HTTPStatus.NOT_FOUND, "not found", "invalid_request_error"))
        except ApiError as e:
            self._send_error(e)
        except Exception as e:  # keep the server alive on any per-request error
            self._send_error(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"internal error: {e!r}", "server_error"))

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing Content-Length", "invalid_request_error")
        try:
            n = int(length)
        except ValueError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length", "invalid_request_error") from e
        return self.rfile.read(n)

    def _handle_transcription(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        main_type, params = _parse_content_type(content_type)
        if main_type != "multipart/form-data" or "boundary" not in params:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "expected multipart/form-data with a 'file' field",
                "invalid_request_error",
            )
        body = self._read_body()
        fields = parse_multipart(body, params["boundary"])
        by_name = {f.name: f for f in fields}

        file_field = by_name.get("file")
        if file_field is None or not file_field.data:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing required field 'file'", "invalid_request_error")

        model = by_name.get("model")
        if model is not None:
            print(f"[flow.server] request model={model.data.decode('utf-8', errors='replace')!r} (ignored)")

        language_field = by_name.get("language")
        language_raw = language_field.data.decode("utf-8", errors="replace").strip() if language_field else None
        language = _normalize_language(language_raw, self.config)

        response_format_field = by_name.get("response_format")
        response_format = (
            response_format_field.data.decode("utf-8", errors="replace").strip().lower()
            if response_format_field
            else "json"
        )
        if response_format not in ("json", "text", "verbose_json"):
            response_format = "json"

        audio = decode_audio_bytes(file_field.data, self.config)
        ensure_warm(self.config)
        result = run_transcription(audio, language, self.config)

        if response_format == "text":
            self._send_text(HTTPStatus.OK, result["text"])
            return
        if response_format == "verbose_json":
            self._send_json(
                HTTPStatus.OK,
                {
                    "text": result["text"],
                    "language": result["language"],
                    "duration": round(result["duration"], 3),
                },
            )
            return
        self._send_json(HTTPStatus.OK, {"text": result["text"]})


def make_handler(config: FlowConfig) -> type[TranscriptionHandler]:
    return type("BoundTranscriptionHandler", (TranscriptionHandler,), {"config": config})


def serve(config: FlowConfig = DEFAULT, port: int | None = None) -> ThreadingHTTPServer:
    """Build and return a bound (not yet serving) ThreadingHTTPServer.

    Caller is responsible for calling serve_forever() / shutdown().
    Never binds anywhere but config.server_host (localhost only).
    """
    host = config.server_host
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"refusing to bind non-localhost host {host!r}")
    bind_port = port if port is not None else config.server_port
    handler_cls = make_handler(config)
    httpd = ThreadingHTTPServer((host, bind_port), handler_cls)
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flow.server", description="Localhost ASR sidecar (OpenAI-compatible /v1/audio/transcriptions)"
    )
    parser.add_argument("--port", type=int, default=None, help=f"port to bind (default: {DEFAULT.server_port})")
    parser.add_argument("--lang", choices=["auto", "en", "bg"], default=None, help="language mode (default: config, 'auto')")
    parser.add_argument("--no-warm", action="store_true", help="skip warming the model on startup (warms on first request instead)")
    args = parser.parse_args(argv)

    config = FlowConfig()
    if args.lang is not None:
        config.language_mode = args.lang

    httpd = serve(config, port=args.port)
    host, port = httpd.server_address[0], httpd.server_address[1]
    print(f"[flow.server] Flow ASR sidecar — binding http://{host}:{port}")
    print(f"[flow.server]   POST http://{host}:{port}/v1/audio/transcriptions")
    print(f"[flow.server]   GET  http://{host}:{port}/health")
    print(f"[flow.server] model: {config.stt_model_repo!r} (language_mode={config.language_mode})")
    dictionary = load_configured_dictionary(config)
    print(f"[flow.server] dictionary: {len(dictionary.terms)} term(s) loaded from {config.dictionary_path!r}")

    if not args.no_warm:
        print("[flow.server] warming model ...", flush=True)
        t0 = time.monotonic()
        ensure_warm(config)
        print(f"[flow.server]   ready in {time.monotonic() - t0:.2f}s")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        print("\n[flow.server] bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
