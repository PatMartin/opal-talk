import numpy as np
import pytest

from opal_talk.tts.audio import resample, to_pcm16, wav_bytes
from opal_talk.tts.base import OptionSpec, check_options
from opal_talk.tts.chunking import chunk_text


class TestChunking:
    def test_short_text_is_one_chunk(self):
        assert chunk_text("Hello world.") == ["Hello world."]

    def test_preserves_order_and_content(self):
        text = "First sentence. Second one! Third? Fourth; fifth: sixth."
        chunks = chunk_text(text, max_chars=20)
        assert " ".join(chunks).split() == text.split()
        assert all(len(c) <= 20 for c in chunks)

    def test_packs_sentences_up_to_limit(self):
        assert chunk_text("A b. C d. E f.", max_chars=100) == ["A b. C d. E f."]

    def test_splits_long_sentence_on_clauses_then_words(self):
        text = "alpha, " * 30 + "omega"
        chunks = chunk_text(text, max_chars=25)
        assert all(len(c) <= 25 for c in chunks)
        assert " ".join(chunks).split() == text.split()

    def test_hard_splits_very_long_words(self):
        chunks = chunk_text("x" * 95, max_chars=40)
        assert chunks == ["x" * 40, "x" * 40, "x" * 15]

    def test_whitespace_only_yields_nothing(self):
        assert chunk_text(" \n\t ") == []

    def test_paragraphs_and_newlines(self):
        assert chunk_text("line one\nline two\n\nline three", max_chars=10) == ["line one", "line two", "line three"]

    def test_invalid_limit(self):
        with pytest.raises(ValueError):
            chunk_text("x", 0)


class TestAudio:
    def test_pcm16_encoding_and_clipping(self):
        pcm = to_pcm16(np.array([0.0, 1.0, -1.0, 2.0], dtype=np.float32))
        assert np.frombuffer(pcm, "<i2").tolist() == [0, 32767, -32767, 32767]

    def test_gain(self):
        pcm = to_pcm16(np.array([0.5], dtype=np.float32), volume=0.5)
        assert np.frombuffer(pcm, "<i2")[0] == round(0.25 * 32767)
        assert np.frombuffer(to_pcm16(np.array([0.5], dtype=np.float32), 0.0), "<i2")[0] == 0

    def test_resample_changes_length(self):
        x = np.zeros(22050, dtype=np.float32)
        assert len(resample(x, 22050, 16000)) == 16000
        assert resample(x, 22050, 22050) is x

    def test_wav_header(self):
        data = wav_bytes(b"\x00\x00" * 10, 8000)
        assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
        assert int.from_bytes(data[24:28], "little") == 8000
        assert int.from_bytes(data[22:24], "little") == 1
        assert int.from_bytes(data[34:36], "little") == 16


class TestOptionChecking:
    SPECS = {
        "a": OptionSpec((int,), ("generation",), lambda v: None if v > 0 else "must be positive"),
        "b": OptionSpec((str,), ("engine",), nullable=True),
        "c": OptionSpec((bool,), ("voice",)),
    }

    def test_valid(self):
        assert check_options({"a": 1}, self.SPECS, ("generation",)) == []
        assert check_options({"b": None}, self.SPECS, ("engine",)) == []

    def test_unknown_and_wrong_scope(self):
        issues = check_options({"zzz": 1, "b": "x"}, self.SPECS, ("generation",))
        assert [i.field for i in issues] == ["zzz", "b"]
        assert all(i.code == "unsupported_option" for i in issues)

    def test_type_and_range(self):
        issues = check_options({"a": "1"}, self.SPECS, ("generation",))
        assert issues[0].code == "invalid_option"
        assert "positive" in check_options({"a": -1}, self.SPECS, ("generation",))[0].message

    def test_bool_is_not_int(self):
        assert check_options({"a": True}, self.SPECS, ("generation",))[0].code == "invalid_option"
