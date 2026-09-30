"""Self-written protocol audio boxes/answers; not decoded media or live STT."""

from bili_comment_bot.config import Settings


def box(kind: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def audio_bytes(size=32):
    return (
        box(b"ftyp", b"M4A \0\0\0\0isom") + box(b"moov", b"synthetic") + box(b"mdat", b"x" * size)
    )


def transcription_settings(**overrides):
    raw = {
        "evidence": {"transcription_enabled": True},
        "transcription": {"base_url": "https://speech.example/v1", "api_key": "speech-fixture-key"},
    }
    for key, values in overrides.items():
        raw.setdefault(key, {}).update(values)
    return Settings.model_validate(raw)


def transcript_response(text="早晨散步，晚上看星星。", duration=30.0):
    return {
        "task": "transcribe",
        "language": "chinese",
        "duration": duration,
        "text": text,
        "segments": [{"start": 0.0, "end": 10.0, "text": text, "no_speech_prob": 0.1}],
    }


def audio_response(**overrides):
    raw = {
        "timelength": 30000,
        "dash": {
            "duration": 30,
            "audio": [
                {
                    "id": 30216,
                    "bandwidth": 64000,
                    "codecs": "mp4a.40.2",
                    "base_url": "https://upos-sz-mirrorcos.bilivideo.com/audio.m4s?signature=private",
                    "backup_url": [],
                }
            ],
        },
    }
    raw.update(overrides)
    return raw
