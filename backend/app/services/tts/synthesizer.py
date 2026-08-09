"""TTS 分段合成 + 时间戳拼装（生产版）
S3 验证结论：腾讯云 TTS 原生返回句级时间戳（Subtitles），
但基础合成接口单次限约150字，必须按自然段分段合成 + 累加偏移得到全章时间轴。
用法：
    from app.services.tts.synthesizer import ChapterTTS
    tts = ChapterTTS.from_env()
    result = tts.synthesize_chapter(text, voice=101002)
    # result.segments -> [{text,start_ms,end_ms}]，result.audio_bytes
"""
from __future__ import annotations
import os, re, json, base64, logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)

try:
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.tts.v20190823 import tts_client
except ImportError:
    tts_client = None


@dataclass
class Segment:
    text: str
    start_ms: int
    end_ms: int


@dataclass
class ChapterAudio:
    audio_bytes: bytes
    segments: List[Segment]
    duration_ms: int
    failed_paragraphs: List[int] = field(default_factory=list)


# 常用音色：101001 智瑜(女) / 101002 智聆(男) —— 适老推荐男中音
DEFAULT_VOICE = 101002
MAX_TTS_CHARS = 140  # 基础接口安全阈值（实测约150字上限）


class TTSError(Exception):
    pass


class ChapterTTS:
    def __init__(self, secret_id: str, secret_key: str, region: str = "ap-beijing"):
        if tts_client is None:
            raise TTSError("请先 pip install tencentcloud-sdk-python-tts")
        cred = credential.Credential(secret_id, secret_key)
        hp = HttpProfile(); hp.endpoint = "tts.tencentcloudapi.com"
        cp = ClientProfile(); cp.httpProfile = hp
        self._client = tts_client.TtsClient(cred, region, cp)

    @classmethod
    def from_env(cls, region: str = None):
        return cls(os.environ["TENCENT_SECRET_ID"], os.environ["TENCENT_SECRET_KEY"],
                   region or os.environ.get("TENCENT_REGION", "ap-beijing"))

    @staticmethod
    def split_paragraphs(text: str) -> List[str]:
        return [p.strip() for p in re.split(r"\n+", text) if p.strip()]

    def _synth_paragraph(self, text: str, voice: int) -> tuple[bytes, list]:
        """合成单段，返回 (audio_bytes, subtitles)。"""
        from tencentcloud.tts.v20190823 import models
        req = models.TextToVoiceRequest()
        req.Text = text
        req.SessionId = "chapter"
        req.VoiceType = voice
        req.Codec = "mp3"
        req.SampleRate = 16000
        try:
            req.EnableSubtitle = True
        except Exception:
            pass
        resp = self._client.TextToVoice(req)
        js = json.loads(resp.to_json_string())
        audio = base64.b64decode(js["Audio"]) if js.get("Audio") else b""
        return audio, js.get("Subtitles", [])

    def synthesize_chapter(self, text: str, voice: int = DEFAULT_VOICE) -> ChapterAudio:
        """分段合成 + 时间戳拼装。段间留 GAP_MS 间隔。"""
        GAP_MS = 250
        paragraphs = self.split_paragraphs(text)
        segments: List[Segment] = []
        full_audio = b""
        offset = 0
        failed = []
        for i, para in enumerate(paragraphs):
            if len(para) > MAX_TTS_CHARS:
                # 超长段按句二次切分（句号/问号/叹号）
                sub = self._split_long(para)
            else:
                sub = [para]
            para_start = offset
            for piece in sub:
                try:
                    audio, subs = self._synth_paragraph(piece, voice)
                except Exception as e:  # noqa
                    logger.warning("段%d合成失败: %s", i+1, e)
                    failed.append(i+1)
                    continue
                if not audio:
                    failed.append(i+1); continue
                full_audio += audio
                if subs:
                    offset = offset + subs[-1]["EndTime"]
                else:
                    offset += 3000
            para_end = offset
            segments.append(Segment(text=para, start_ms=para_start, end_ms=para_end))
            offset = para_end + GAP_MS
        if not segments:
            raise TTSError("整章合成失败：无有效段落")
        return ChapterAudio(audio_bytes=full_audio, segments=segments,
                            duration_ms=offset, failed_paragraphs=failed)

    @staticmethod
    def _split_long(text: str) -> List[str]:
        parts = re.split(r"(?<=[。！？!?])", text)
        out, buf = [], ""
        for p in parts:
            if len(buf) + len(p) <= MAX_TTS_CHARS:
                buf += p
            else:
                if buf: out.append(buf)
                buf = p
        if buf: out.append(buf)
        return [x for x in out if x.strip()]

    @staticmethod
    def segments_to_json(result: ChapterAudio) -> str:
        return json.dumps([{"text": s.text, "start_ms": s.start_ms, "end_ms": s.end_ms}
                           for s in result.segments], ensure_ascii=False, indent=2)
