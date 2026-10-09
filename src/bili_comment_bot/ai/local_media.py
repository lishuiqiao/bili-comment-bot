"""Decode local MP4 only; PyAV bundles codecs so ffmpeg CLI is unnecessary."""

import math


def open_media(path):
    import av

    return av.open(
        str(path), format="mov", options={"protocol_whitelist": "file", "enable_drefs": "0"}
    )


def sample_frames(path, duration, count, long_edge):
    if not 0 < count <= 64 or not 0 < duration <= 14400 or not 224 <= long_edge <= 1024:
        raise ValueError("invalid media budget")
    frames, times = [], []
    with open_media(path) as container:
        if len(container.streams.video) != 1:
            raise ValueError("video stream required")
        stream = container.streams.video[0]
        stream.thread_count = 1
        if (
            stream.duration is None
            or stream.start_time not in (None, 0)
            or abs(float(stream.duration * stream.time_base) - duration) > 2
            or not 0 < stream.width <= 4096
            or not 0 < stream.height <= 4096
        ):
            raise ValueError("invalid video geometry or duration")
        for index in range(count):
            target = duration * (index + 0.5) / count
            container.seek(int(target / stream.time_base), stream=stream, backward=True)
            candidate = None
            for frame in container.decode(stream):
                if frame.pts is None:
                    raise ValueError("missing frame timestamp")
                stamp = float(frame.pts * stream.time_base)
                if not math.isfinite(stamp) or not 0 <= stamp <= duration + 0.1:
                    raise ValueError("invalid frame timestamp")
                if not 0 < frame.width <= 4096 or not 0 < frame.height <= 4096:
                    raise ValueError("invalid decoded geometry")
                candidate = (frame, stamp)
                if stamp >= target:
                    break
            # Low-FPS clips may end before every target, but their final frame is valid.
            if candidate is not None:
                frame, stamp = candidate
                if not times or stamp > times[-1]:
                    scale = min(1, long_edge / max(frame.width, frame.height))
                    image = frame.reformat(
                        width=max(1, int(frame.width * scale)),
                        height=max(1, int(frame.height * scale)),
                        format="rgb24",
                    ).to_image()
                    frames.append(image)
                    times.append(stamp)
        if not frames:
            raise ValueError("no decodable samples")
    return frames, times


def decode_audio(path, expected_duration):
    import av
    import numpy as np

    chunks, samples = [], 0
    with open_media(path) as container:
        if len(container.streams.audio) != 1:
            raise ValueError("audio stream required")
        stream = container.streams.audio[0]
        stream.thread_count = 1
        if stream.start_time not in (None, 0):
            raise ValueError("nonzero audio offset")
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                chunk = converted.to_ndarray().reshape(-1)
                samples += len(chunk)
                if samples > (expected_duration + 2) * 16000:
                    raise ValueError("audio duration budget")
                chunks.append(chunk)
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray().reshape(-1))
    if not chunks:
        raise ValueError("empty audio")
    waveform = np.concatenate(chunks).astype(np.float32)
    duration = len(waveform) / 16000
    if abs(duration - expected_duration) > 2 or not np.isfinite(waveform).all():
        raise ValueError("invalid decoded audio")
    return waveform, duration
