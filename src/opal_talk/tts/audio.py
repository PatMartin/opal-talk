"""Engine-neutral audio post-processing: resampling, gain, S16LE PCM and WAV encoding."""

from __future__ import annotations

import io
import wave
from math import gcd

import numpy as np
from scipy.signal import resample_poly

SAMPLE_WIDTH = 2
CHANNELS = 1
# Fixed container overhead of the WAV encoding produced by wav_bytes():
# RIFF header (12) + fmt chunk (24) + data chunk header (8) for mono S16LE.
WAV_HEADER_BYTES = 44


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or samples.size == 0:
        return samples
    g = gcd(src_rate, dst_rate)
    return resample_poly(samples, dst_rate // g, src_rate // g).astype(np.float32)


def to_pcm16(samples: np.ndarray, volume: float = 1.0) -> bytes:
    """Apply linear gain to float samples in [-1, 1], clip, and encode as signed 16-bit little-endian."""
    scaled = np.asarray(samples, dtype=np.float32) * volume * 32767.0
    np.clip(scaled, -32767.0, 32767.0, out=scaled)  # fewer temporaries, same output
    return scaled.round().astype("<i2").tobytes()


def wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()
