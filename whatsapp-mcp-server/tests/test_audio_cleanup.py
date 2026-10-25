"""send_audio_message removes the .ogg it converts, and nothing else.

Converted files go in TMPDIR, which a hosted deployment points at persistent
storage so the bridge service can read them. Anything not cleaned up there
would accumulate for good.
"""

import pytest
import requests

import whatsapp


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture
def source_mp3(tmp_path):
    path = tmp_path / "note.mp3"
    path.write_bytes(b"ID3 fake mp3")
    return path


@pytest.fixture
def fake_convert(monkeypatch, tmp_path):
    """Stand in for ffmpeg: write a converted file and remember its path."""
    created = []

    def convert(input_file):
        out = tmp_path / f"converted-{len(created)}.ogg"
        out.write_bytes(b"OggS")
        created.append(out)
        return str(out)

    monkeypatch.setattr(whatsapp.audio, "convert_to_opus_ogg_temp", convert)
    return created


def post_returning(response):
    def post(url, json):
        # The bridge must still be able to read the file while the request is
        # in flight, so it has to exist at this point.
        assert open(json["media_path"], "rb").read()
        return response
    return post


def test_converted_file_removed_after_successful_send(monkeypatch, source_mp3, fake_convert):
    monkeypatch.setattr(whatsapp.requests, "post",
                        post_returning(FakeResponse(200, {"success": True, "message": "sent"})))

    assert whatsapp.send_audio_message("123", str(source_mp3)) == (True, "sent")
    assert not fake_convert[0].exists()
    assert source_mp3.exists()


def test_converted_file_removed_when_bridge_errors(monkeypatch, source_mp3, fake_convert):
    monkeypatch.setattr(whatsapp.requests, "post",
                        post_returning(FakeResponse(500, text="boom")))

    ok, _ = whatsapp.send_audio_message("123", str(source_mp3))
    assert not ok
    assert not fake_convert[0].exists()


def test_converted_file_removed_when_request_fails(monkeypatch, source_mp3, fake_convert):
    def post(url, json):
        raise requests.ConnectionError("bridge down")

    monkeypatch.setattr(whatsapp.requests, "post", post)

    ok, message = whatsapp.send_audio_message("123", str(source_mp3))
    assert not ok and "bridge down" in message
    assert not fake_convert[0].exists()


def test_callers_own_ogg_is_never_deleted(monkeypatch, tmp_path, fake_convert):
    original = tmp_path / "already.ogg"
    original.write_bytes(b"OggS")
    monkeypatch.setattr(whatsapp.requests, "post",
                        post_returning(FakeResponse(200, {"success": True, "message": "sent"})))

    assert whatsapp.send_audio_message("123", str(original)) == (True, "sent")
    assert original.exists()
    assert fake_convert == []  # no conversion happened
