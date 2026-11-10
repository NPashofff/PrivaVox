"""Microphone recording (sounddevice) and energy helpers."""

from __future__ import annotations

import threading
import time

import numpy as np
import sounddevice as sd

from .config import DEFAULT, FlowConfig


def rms(audio: np.ndarray) -> float:
    """Root-mean-square energy of a float32 audio buffer (full scale = 1.0)."""
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def is_silence(audio: np.ndarray, threshold: float = DEFAULT.energy_threshold) -> bool:
    """True if the buffer's overall RMS energy is below the gate threshold."""
    return rms(audio) < threshold


# Recorder.stop(): how long to wait for the stream to stop ITSELF before
# falling back to an outside stop.
_STOP_WAIT_S = 1.0


class Recorder:
    """Push-to-talk microphone recorder.

    start() opens a 16 kHz mono float32 InputStream and buffers audio;
    stop() closes the stream and returns everything captured as a 1-D
    numpy array. The buffer is capped at max_recording_s (extra audio is
    dropped, not an error). Opening the stream is the point where macOS
    asks for / enforces Microphone permission.
    """

    def __init__(self, config: FlowConfig = DEFAULT) -> None:
        self.config = config
        self._stream: sd.InputStream | None = None
        self._chunks: list[np.ndarray] = []
        self._samples = 0
        self._max_samples = int(config.max_recording_s * config.sample_rate)
        self._truncated = False
        self._overflows = 0
        self.start_delay_s = 0.0
        self._stop_req = threading.Event()
        self._finished = threading.Event()
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def truncated(self) -> bool:
        """True if the last recording hit the max-duration cap."""
        return self._truncated

    @property
    def overflows(self) -> int:
        """Callbacks that reported a status flag (input overflow = PortAudio
        DROPPED audio between callbacks). Non-zero = the transcript will have
        eaten syllables no ASR can restore — that's a capture problem, not a
        Whisper problem, and it must be visible in the log."""
        return self._overflows

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:  # CallbackFlags: input_overflow etc. — count, never ignore
            self._overflows += 1
        if self._stop_req.is_set():
            raise sd.CallbackStop  # see stop(): the stream ends itself
        with self._lock:
            if self._samples >= self._max_samples:
                self._truncated = True
                return
            chunk = indata[:, 0].copy()  # mono channel, own the memory
            self._chunks.append(chunk)
            self._samples += len(chunk)

    def start(self) -> None:
        if self._stream is not None:
            return
        with self._lock:
            self._chunks = []
            self._samples = 0
            self._truncated = False
        self._overflows = 0
        # Fresh per stream: a late callback of an old stream must not see
        # (or trip) the new one's flags.
        self._stop_req = threading.Event()
        self._finished = finished = threading.Event()
        t0 = time.perf_counter()
        self._stream = sd.InputStream(
            samplerate=self.config.sample_rate,
            channels=self.config.channels,
            dtype="float32",
            callback=self._callback,
            finished_callback=finished.set,
        )
        self._stream.start()
        # Speech before the stream is live is lost (CoreAudio spin-up) — the
        # push-to-talk user starts speaking the instant they press the key,
        # so a slow open clips the first syllable. Measured, logged per take.
        self.start_delay_s = time.perf_counter() - t0

    def stop(self) -> np.ndarray:
        """Stop recording and return the captured audio (may be empty)."""
        stream, self._stream = self._stream, None
        if stream is not None:
            # NEVER stream.stop() from here while audio is flowing.
            # PortAudio/CoreAudio DEADLOCK (macOS 27; reproduced standalone:
            # a plain start/stop loop hung on the 33rd cycle): an outside
            # stop takes the AudioUnit mutex and then wants the HAL one,
            # while the HAL IO thread — holding the HAL mutex — is inside
            # PortAudio's startStopCallback wanting the AudioUnit one. Both
            # hang forever and the app sits on "Записва…" until killed.
            # Instead the callback stops the stream from the IO thread itself
            # (CallbackStop → no cross-thread lock order at all; 150 cycles
            # clean), and we only close it once it reports "finished".
            self._stop_req.set()
            if not self._finished.wait(_STOP_WAIT_S):
                # No callback ran (IO never started / device vanished):
                # close() below stops from outside — the risky path, but
                # there is no IO thread racing us if nothing is flowing.
                print("[flow.audio] stream did not finish on its own — closing from outside")
            stream.close()
        with self._lock:
            chunks, self._chunks = self._chunks, []
            self._samples = 0
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks)
        return audio[: self._max_samples]

    def cancel(self) -> None:
        """Stop recording and discard everything captured."""
        self.stop()


def duration_s(audio: np.ndarray, sample_rate: int = DEFAULT.sample_rate) -> float:
    return audio.size / float(sample_rate)
