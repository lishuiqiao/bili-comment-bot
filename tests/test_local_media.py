"""Real codecs with synthetic media; no downloaded clips or model weights."""

from fractions import Fraction

import pytest

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")

from bili_comment_bot.ai.local_media import decode_audio, sample_frames  # noqa: E402


def video(path):
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = 320, 240, "yuv420p"
        for index in range(40):
            pixels = np.zeros((240, 320, 3), dtype=np.uint8)
            pixels[:, :, 0 if index < 20 else 2] = 220
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            frame.pts = index
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


def audio(path):
    with av.open(str(path), "w", format="mp4") as output:
        stream = output.add_stream("aac", rate=16000)
        stream.layout = "mono"
        for index in range(32):
            frame = av.AudioFrame.from_ndarray(
                np.zeros((1, 1000), dtype=np.float32), format="fltp", layout="mono"
            )
            frame.sample_rate = 16000
            frame.time_base = Fraction(1, 16000)
            frame.pts = index * 1000
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


def test_frame_sampling_tracks_actual_times_and_resizes(tmp_path):
    path = tmp_path / "clip.mp4"
    video(path)
    images, times = sample_frames(path, 4, 4, 224)
    assert times == [0.5, 1.5, 2.5, 3.5]
    assert all(max(image.size) <= 224 for image in images)
    assert images[0].getpixel((0, 0))[0] > 200
    assert images[-1].getpixel((0, 0))[2] > 200
    with pytest.raises(ValueError):
        sample_frames(path, 40, 4, 224)


def test_audio_is_mono_16khz_and_duration_comes_from_decoded_samples(tmp_path):
    path = tmp_path / "audio.m4a"
    audio(path)
    samples, duration = decode_audio(path, 2)
    assert samples.ndim == 1 and samples.dtype == np.float32
    assert 2 <= duration < 2.1 and len(samples) == duration * 16000
    assert not samples.any()
    with pytest.raises(ValueError):
        decode_audio(path, 30)


def test_decoder_rejects_non_mp4_and_wrong_stream_type(tmp_path):
    path = tmp_path / "bad.mp4"
    path.write_text("#EXTM3U\nhttps://example.invalid/private")
    with pytest.raises(av.error.InvalidDataError):
        sample_frames(path, 4, 4, 224)
    video(path)
    with pytest.raises(ValueError):
        decode_audio(path, 4)
