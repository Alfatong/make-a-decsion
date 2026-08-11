"""内容生成服务（M1 核心）
串联 LLM Adapter + 记忆层：题材模板 → 全书大纲 → 逐章生成（含一致性校验）。
所有生成遵循 S2 结论：大纲预置硬事实 + 双层校验 + 空章重试。
"""
from __future__ import annotations
import os, re, json, logging, tempfile
from typing import List, Dict, Optional
from sqlalchemy.orm import Session

from ...core.config import settings
from ...models import Book, Chapter, Theme
from ..llm.adapter import LLMAdapter
from ..memory.fact_store import FactStore
from ..memory.checker import ConsistencyChecker, RuleChecker
from ..memory.generator import ChapterGenerator
from ..tts.synthesizer import ChapterTTS, TTSError

logger = logging.getLogger(__name__)

OUTLINE_SYS = "你是长篇小说策划编辑，擅长老年年代题材，输出结构化大纲。"
INTRO_SYS = "你是小说编辑，擅长写给老年读者看的作品简介，朴实有吸引力。"
INTRO_TMPL = """为长篇小说《{title}》写一段 120-180 字的作品简介。

【全书大纲】
{outline}

要求：口语化、有画面感，突出主要人物、核心冲突和年代烟火气，让运营人员快速把握全书主题，也让老年读者一看就想点进去读。不要剧透结局，不要用"本书讲述了"开头。直接输出简介正文。"""
OUTLINE_TMPL = """基于以下题材模板，为长篇小说《{title}》创作全书大纲（共{n}章）。

【题材模板】
{theme_prompt}

要求：
1. 先给出主要角色表（姓名/年龄/关系/初始住处/关键道具）
2. 再逐章列出章节标题与一句话情节（格式：第N章 标题 - 情节）
3. 标注关键状态变化点（角色生死/道具归属/住处变动）所在章节
直接输出大纲文本。"""


POLISH_SYS = "你是资深年代小说编辑，专治 AI 腔，改稿不动剧情。"
POLISH_TMPL = """下面是长篇小说《{title}》第{no}章的初稿。请润色改写。

【文风示范（学习这种感觉，不要照抄内容）】
{style_sample}

【润色要求】
1. 剧情、人物、对话走向、事实细节一律不变，只做文字层面的打磨
2. 删 AI 腔：慎用"仿佛/宛如/不禁/顿时/一股暖流"，能删就删；形容词堆叠处砍到只剩一个
3. 长句拆短，一句话只说一件事；对话要短、要脆、要带人物脾气
4. 每 300 字内补一处可感的细节：手上的动作、物件的质感、声响、气味、光线，从本章已有物件里选，不新造情节
5. 段落疏密有致，情绪重的地方段落要短
6. 章末必须留钩子：一个未落地的念头、一个反常的动静、一句没说完的话，让读者想翻下一章
7. 总字数保持在原稿的 85%-115%
直接输出润色后的全文，不要标题，不要解释。

【初稿】
{draft}"""

STYLE_SAMPLE = """外头北风刮了一夜，窗户纸噗噗地响。老周头摸黑起来，往灶膛里塞了把松明子，火苗子哄地窜上来，把他的脸映得红一阵白一阵。
"爹，水缸冻了。"大林子揉着眼睛站在门口，鼻尖通红。
"冻了就砸。"老周头把火钳子往灶边一搁，"人还能让尿憋死。"
他抄起门后的镐头，三两下凿开缸沿的冰碴子，舀了半瓢水，仰头灌了一口。水凉得扎牙，他咂咂嘴，倒是笑了。"""

BRIEF_SYS = "你是小说编辑，用一句话概括章节内容。"
BRIEF_TMPL = """用一句话（30-50字）概括这一章讲了什么，突出具体事件，让没读过的人知道这章的看点。直接输出这句话，不要"本章讲述了"。

【章节正文】
{content}"""


def _parse_outline_facts(outline: str) -> List[Dict]:
    """从大纲提取结构化事实计划（角色/道具/住处初始态 + 关键变化）。
    生产可用更精细的解析或人工录入，这里提供基础实现。"""
    facts = []
    # 这里简化为返回空，实际由大纲结构化解析或运营预置
    return facts


class ContentPipeline:
    def __init__(self, db: Session):
        self.db = db
        self.adapter = LLMAdapter.from_env()
        self.checker = ConsistencyChecker(adapter=self.adapter, enable_semantic=True)

    def _fact_store(self, book_id: int) -> FactStore:
        # 每本书独立事实库文件（生产可改 PostgreSQL 实现同接口）
        path = os.path.join(tempfile.gettempdir(), f"book_{book_id}_facts.db")
        return FactStore(path, str(book_id))

    def create_book(self, theme_id: int, title: str, chapters: Optional[int] = None) -> Book:
        theme = self.db.get(Theme, theme_id)
        if not theme:
            raise ValueError(f"题材 {theme_id} 不存在")
        n = chapters or theme.target_chapters
        # 生成全书大纲
        prompt = OUTLINE_TMPL.format(title=title, n=n, theme_prompt=theme.prompt_template)
        r = self.adapter.generate(prompt, model=settings.LLM_MODEL_OUTLINE,
                                  system=OUTLINE_SYS, max_tokens=6000, temperature=0.7)
        # 生成作品简介
        intro = ""
        try:
            ri = self.adapter.generate(
                INTRO_TMPL.format(title=title, outline=r.text[:2000]),
                model=settings.LLM_MODEL_CHAPTER, system=INTRO_SYS,
                max_tokens=300, temperature=0.7)
            intro = ri.text.strip()
        except Exception as e:  # noqa
            logger.warning("简介生成失败: %s", e)
        book = Book(theme_id=theme_id, title=title, intro=intro, outline=r.text,
                    status="draft", total_chapters=n, ai_label=True)
        self.db.add(book); self.db.commit(); self.db.refresh(book)
        logger.info("创建书籍 id=%s 大纲 %d 字", book.id, len(r.text))
        return book

    def generate_chapter(self, book_id: int, no: int, polish: bool = True) -> Chapter:
        book = self.db.get(Book, book_id)
        if not book:
            raise ValueError(f"书 {book_id} 不存在")
        store = self._fact_store(book_id)
        gen = ChapterGenerator(self.adapter, store, self.checker,
                               model=settings.LLM_MODEL_CHAPTER)
        brief = self._chapter_brief(book.outline, no)
        theme_prompt = book.theme.prompt_template if book.theme else ""
        result = gen.generate(no, theme_prompt, brief, preset=None)
        content = result["content"]
        # 润色 pass：去 AI 腔、强化文风与章末钩子（失败降级用初稿）
        if polish:
            try:
                content = self.polish_text(book.title, no, content)
            except Exception as e:  # noqa
                logger.warning("第%d章润色失败，用初稿: %s", no, e)

        ch = self.db.query(Chapter).filter_by(book_id=book_id, no=no).first()
        if not ch:
            ch = Chapter(book_id=book_id, no=no)
            self.db.add(ch)
        ch.content = content
        ch.word_count = len(re.sub(r"\s", "", content))
        ch.consistency_conflicts = result["conflicts"]
        ch.review_status = "pending"
        self.db.commit(); self.db.refresh(ch)
        # 章节一句话提要
        try:
            ch.brief = self.gen_brief(content)
            self.db.commit()
        except Exception as e:  # noqa
            logger.warning("第%d章提要生成失败: %s", no, e)
        return ch

    def polish_text(self, title: str, no: int, draft: str) -> str:
        """润色一段正文（供生成 pass 和既有章节翻新复用）。"""
        r = self.adapter.generate(
            POLISH_TMPL.format(title=title, no=no,
                               style_sample=STYLE_SAMPLE, draft=draft),
            model=settings.LLM_MODEL_OUTLINE,  # 润色用 Pro，质感优先
            system=POLISH_SYS, max_tokens=6000, temperature=0.6)
        text = r.text.strip()
        if len(text) < len(draft) * 0.5:
            raise RuntimeError("润色结果异常短，丢弃")
        return text

    def gen_brief(self, content: str) -> str:
        """章节一句话提要（30-50字）。"""
        r = self.adapter.generate(BRIEF_TMPL.format(content=content[:2500]),
                                  model=settings.LLM_MODEL_CHAPTER,
                                  system=BRIEF_SYS, max_tokens=120, temperature=0.5)
        return r.text.strip().strip('"').strip()[:80]

    def generate_book(self, book_id: int, max_chapters: Optional[int] = None) -> Dict:
        """逐章生成全书，返回统计。"""
        book = self.db.get(Book, book_id)
        book.status = "generating"; self.db.commit()
        n = max_chapters or book.total_chapters
        done, conflicts = 0, 0
        for no in range(1, n + 1):
            try:
                ch = self.generate_chapter(book_id, no)
                done += 1
                if ch.consistency_conflicts:
                    conflicts += 1
            except Exception as e:  # noqa
                logger.error("第%d章生成失败: %s", no, e)
        book.status = "reviewing"; self.db.commit()
        return {"book_id": book_id, "chapters_done": done, "conflict_chapters": conflicts}

    @staticmethod
    def _chapter_brief(outline: str, no: int) -> str:
        for line in outline.splitlines():
            s = line.strip()
            if s.startswith(f"第{no}章") or s.startswith(f"{no}."):
                return s
        return f"第{no}章"

    def synthesize_chapter_audio(self, chapter_id: int,
                                 voice: int = 101002,
                                 upload: Optional[callable] = None) -> Dict:
        """章节 TTS 合成：分段合成 + 时间轴入库。
        upload: 可选回调 (audio_bytes, ext)->url，用于上传 COS 并返回音频地址。
        故障降级：TTS 失败不阻塞阅读，仅记录空时间轴。"""
        ch = self.db.get(Chapter, chapter_id)
        if not ch:
            raise ValueError(f"章节 {chapter_id} 不存在")
        try:
            tts = ChapterTTS.from_env()
            result = tts.synthesize_chapter(ch.content, voice=voice)
        except TTSError as e:
            logger.error("章节%d TTS 失败（降级仅阅读）: %s", chapter_id, e)
            ch.tts_segments = []
            self.db.commit()
            return {"chapter_id": chapter_id, "tts_ok": False, "error": str(e)}
        segments = [{"text": s.text, "start_ms": s.start_ms, "end_ms": s.end_ms}
                    for s in result.segments]
        ch.tts_segments = segments
        audio_url = upload(result.audio_bytes, "mp3") if upload else None
        self.db.commit()
        return {"chapter_id": chapter_id, "tts_ok": True,
                "segments": len(segments), "duration_ms": result.duration_ms,
                "failed_paragraphs": result.failed_paragraphs,
                "audio_url": audio_url}
