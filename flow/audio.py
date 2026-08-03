"""Microphone recording (sounddevice) and energy helpers."""

from __future__ import annotations

import threading

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
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def truncated(self) -> bool:
        """True if the last recording hit the max-duration cap."""
        return self._truncated

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
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
        self._stream = sd.InputStream(
            samplerate=self.config.sample_rate,
            channels=self.config.channels,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        """Stop recording and return the captured audio (may be empty)."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
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
