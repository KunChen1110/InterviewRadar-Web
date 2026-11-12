"""One-sentence Alibaba Cloud ASR adapter with request-scoped audio."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

MAX_AUDIO_BYTES = 2 * 1024 * 1024
ALLOWED_SUFFIXES = {".ogg", ".webm", ".wav", ".mp3", ".aac", ".amr"}


class AudioValidationError(ValueError):
    pass


def transcribe_once(path: str | Path) -> str:
    path = Path(path)
    normalized: Path | None = None
    try:
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise AudioValidationError("仅支持 OGG/WebM/WAV/MP3/AAC/AMR 音频")
        if path.stat().st_size > MAX_AUDIO_BYTES:
            raise AudioValidationError("音频文件不能超过 2 MB")
        # 浏览器 MediaRecorder 常输出 Ogg/WebM Opus；统一转为 16 kHz 单声道 PCM，
        # 避免把容器格式或不匹配的采样率直接传给 ASR 影响识别率。
        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as output:
            normalized = Path(output.name)
        _convert_to_pcm(path, normalized)
        transcript = _transcribe(normalized).strip()
        if not transcript:
            raise AudioValidationError("未识别到有效语音，请靠近麦克风后重试或改用打字回答")
        return transcript
    except AudioValidationError:
        raise
    except RuntimeError as exc:
        # `_transcribe` may surface a provider response containing credentials,
        # request IDs or token state. Keep that diagnostic on the server side
        # and give the candidate one actionable, safe fallback.
        raise RuntimeError("语音转写暂不可用，请直接输入回答") from exc
    except Exception as exc:
        # Provider/SDK internals can contain credentials or opaque request
        # details. The browser only needs a safe fallback instruction.
        raise RuntimeError("语音转写暂不可用，请直接输入回答") from exc
    finally:
        for temp_path in (path, normalized):
            if temp_path:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _convert_to_pcm(source: Path, target: Path) -> None:
    """Convert user audio into NLS-compatible raw PCM without persisting it."""
    try:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-i", str(source), "-t", "60",
                "-ac", "1", "-ar", "16000", "-f", "s16le", str(target),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("服务端缺少音频转换组件") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise AudioValidationError("音频无法解析，请重新录制后再试") from exc


def _transcribe(path: Path) -> str:
    token = os.environ.get("ALIYUN_NLS_TOKEN", "")
    appkey = os.environ.get("ALIYUN_NLS_APP_KEY", "")
    if not token or not appkey:
        raise RuntimeError("阿里云 ASR 未配置 ALIYUN_NLS_TOKEN 或 ALIYUN_NLS_APP_KEY")
    try:
        import nls
    except ImportError as exc:
        raise RuntimeError("未安装阿里云 NLS SDK") from exc

    results: list[str] = []
    failed = False
    url = os.environ.get("ALIYUN_NLS_URL", "wss://nls-gateway-cn-shanghai.aliyuncs.com/ws/v1")

    def on_completed(message, *args):
        try:
            text = json.loads(message).get("payload", {}).get("result", "")
            if text:
                results.append(text)
        except (TypeError, json.JSONDecodeError):
            return

    def on_error(message, *args):
        nonlocal failed
        failed = True

    recognizer = nls.NlsSpeechRecognizer(
        url=url, token=token, appkey=appkey, on_completed=on_completed, on_error=on_error
    )
    recognizer.start(
        aformat="pcm", sample_rate=16000, ch=1, enable_intermediate_result=False,
        enable_punctuation_prediction=True, enable_inverse_text_normalization=True,
    )
    with path.open("rb") as audio:
        while chunk := audio.read(64 * 1024):
            recognizer.send_audio(chunk)
    recognizer.stop()
    if failed:
        raise RuntimeError("语音识别服务返回错误")
    return "".join(results)
