"""cleanup error contracts — no Ollama needed (transport is monkeypatched).

Regressions for the Етап-1 findings:
- warm_up used to swallow _call_ollama errors, so a stopped Ollama server
  still booted the app into "ready".
- response-phase transport failures (Ollama restarted mid-generation) leaked
  unwrapped out of _call_ollama and broke run_pipeline's never-raises contract
  instead of falling back to the raw transcript.
"""

from __future__ import annotations

import http.client

import pytest

from flow import cleanup
from flow.config import FlowConfig
from flow.dictionary import EMPTY


# --- warm_up contract -------------------------------------------------------

def test_warm_up_returns_latency_on_success(monkeypatch):
    monkeypatch.setattr(
        cleanup, "_call_ollama",
        lambda transcript, config, dictionary=None: ("Здравей.", None, 0.01),
    )
    latency = cleanup.warm_up(FlowConfig())
    assert isinstance(latency, float) and latency >= 0.0


def test_warm_up_raises_with_reason_on_error(monkeypatch):
    monkeypatch.setattr(
        cleanup, "_call_ollama",
        lambda transcript, config, dictionary=None:
            (None, "URLError: [Errno 61] Connection refused", 0.0),
    )
    with pytest.raises(cleanup.CleanupWarmupError, match="Connection refused"):
        cleanup.warm_up(FlowConfig())


def test_warm_up_error_is_a_runtimeerror():
    # callers that only know RuntimeError still catch it
    assert issubclass(cleanup.CleanupWarmupError, RuntimeError)


# --- transport failures never escape clean_transcript ------------------------

@pytest.mark.parametrize("exc", [
    ConnectionResetError(54, "Connection reset by peer"),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    http.client.IncompleteRead(b"partial"),
    TimeoutError("timed out"),
], ids=lambda e: type(e).__name__)
def test_clean_transcript_survives_transport_failures(monkeypatch, exc):
    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr("urllib.request.urlopen", boom)
    result = cleanup.clean_transcript("здравей свят", FlowConfig(), dictionary=EMPTY)
    assert result.used_fallback
    assert result.text == "здравей свят"
    assert result.fallback_reason
