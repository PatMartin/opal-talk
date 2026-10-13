"""Engine-neutral audio post-processing: resampling, gain, S16LE PCM and WAV encoding."""

from __future__ import annotations

import io
import wave
from math import gcd

import numpy as np
from scipy.signal import resample_poly

SAMPLE_WIDTH = 2
CHANNELS = 1


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or samples.size == 0:
        return samples
    g = gcd(src_rate, dst_rate)
    return resample_poly(samples, dst_rate // g, src_rate // g).astype(np.float32)


def to_pcm16(samples: np.ndarray, volume: float = 1.0) -> bytes:
    """Apply linear gain to float samples in [-1, 1], clip, and encode as signed 16-bit little-endian."""
    scaled = np.clip(np.asarray(samples, dtype=np.float32) * volume, -1.0, 1.0)
    return (scaled * 32767.0).round().astype("<i2").tobytes()


def wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()
