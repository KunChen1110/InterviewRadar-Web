import asr


def test_transcription_deletes_temporary_audio_after_provider_call(tmp_path, monkeypatch):
    path = tmp_path / "answer.ogg"
    path.write_bytes(b"audio")
    monkeypatch.setattr(asr, "_convert_to_pcm", lambda _source, target: target.write_bytes(b"pcm"))
    monkeypatch.setattr(asr, "_transcribe", lambda _: "可编辑的转写")

    assert asr.transcribe_once(path) == "可编辑的转写"
    assert not path.exists()


def test_audio_size_is_rejected_before_provider_call(tmp_path, monkeypatch):
    path = tmp_path / "answer.ogg"
    path.write_bytes(b"x" * (asr.MAX_AUDIO_BYTES + 1))
    monkeypatch.setattr(asr, "_transcribe", lambda _: "should not run")

    try:
        asr.transcribe_once(path)
    except asr.AudioValidationError as exc:
        assert "2 MB" in str(exc)
    else:
        raise AssertionError("oversized audio must be rejected")
    assert not path.exists()


def test_transcription_rejects_an_empty_provider_result_and_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "answer.ogg"
    path.write_bytes(b"audio")
    monkeypatch.setattr(asr, "_convert_to_pcm", lambda _source, target: target.write_bytes(b"pcm"))
    monkeypatch.setattr(asr, "_transcribe", lambda _: "   ")

    try:
        asr.transcribe_once(path)
    except asr.AudioValidationError as exc:
        assert "未识别到有效语音" in str(exc)
    else:
        raise AssertionError("empty provider output must not be treated as a valid transcript")
    assert not path.exists()


def test_transcription_hides_unexpected_provider_details_from_the_user(tmp_path, monkeypatch):
    path = tmp_path / "answer.ogg"
    path.write_bytes(b"audio")
    monkeypatch.setattr(asr, "_convert_to_pcm", lambda _source, target: target.write_bytes(b"pcm"))
    monkeypatch.setattr(asr, "_transcribe", lambda _: (_ for _ in ()).throw(ValueError("provider internal detail")))

    try:
        asr.transcribe_once(path)
    except RuntimeError as exc:
        assert "暂不可用" in str(exc)
        assert "provider internal detail" not in str(exc)
    else:
        raise AssertionError("unexpected provider failures must become a safe fallback error")
    assert not path.exists()


def test_transcription_hides_provider_runtime_details_from_the_user(tmp_path, monkeypatch):
    path = tmp_path / "answer.ogg"
    path.write_bytes(b"audio")
    monkeypatch.setattr(asr, "_convert_to_pcm", lambda _source, target: target.write_bytes(b"pcm"))
    monkeypatch.setattr(asr, "_transcribe", lambda _: (_ for _ in ()).throw(RuntimeError("NLS invalid token detail")))

    try:
        asr.transcribe_once(path)
    except RuntimeError as exc:
        assert "暂不可用" in str(exc)
        assert "invalid token" not in str(exc)
    else:
        raise AssertionError("provider runtime failures must become a safe fallback error")
    assert not path.exists()
