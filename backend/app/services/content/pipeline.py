"""内容生成服务（M1 核心）
串联 LLM Adapter + 记忆层：题材模板 → 全书大纲 → 逐章生成（含一致性校验）。
所有生成遵循 S2 结论：大纲预置硬事实 + 双层校验 + 空章重试。
"""
from __future__ import annotations
import os, re, json, logging, tempfile, time, threading
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

OUTLINE_SYS = "你是长篇小说策划编辑，擅长老年年代题材，懂节拍设计，输出结构化大纲。"
INTRO_SYS = "你是小说编辑，擅长写给老年读者看的作品简介，朴实有吸引力。"
INTRO_TMPL = """为长篇小说《{title}》写一段 120-180 字的作品简介。

【全书大纲】
{outline}

要求：口语化、有画面感，突出主要人物、核心冲突和年代烟火气，让运营人员快速把握全书主题，也让老年读者一看就想点进去读。不要剧透结局，不要用"本书讲述了"开头。直接输出简介正文。"""
OUTLINE_TMPL = """基于以下题材模板，为长篇小说《{title}》创作全书大纲（共{n}章）。

【题材模板】
{theme_prompt}

要求：
1. 先给"## 主要角色表"，每个角色严格用此格式一行一个：
   - **姓名**｜年龄｜身份/学历｜关系｜初始住处｜关键道具
2. 再给"## 称谓约定"：写明主要人物之间如何互相称呼（尤其同辈亲属对长辈的统一叫法，如兄弟间谈论母亲一律用"咱妈"；谁叫谁的小名/外号），全书所有对话必须遵守
3. 再给"## 章节大纲"，逐章列出（格式：第N章 标题 - 情节 - 本章冲突点 - 章末钩子）
4. 节拍要求（这是重点）：
   - 每章必须有明确的冲突点或情感张力（误会、分歧、难处、反常迹象），不允许"纯过日子"的平章
   - 每章结尾必须有钩子：悬念（发现秘密的一半）、情感爆发前夜、两难抉择留白，三选一
   - 每3-5章安排一次小高潮（矛盾激化/真相揭露一角/关系破裂或和解）
   - 全书安排2-3次大高潮，高潮前3-5章埋伏笔线索
   - 情绪节奏遵循"压抑-释放"循环：憋屈的戏不能连压超过3章，之后必须给读者一口气顺出来的释放（和解、澄清、撑腰、团聚）
5. 标注关键状态变化点（角色生死/道具归属/住处变动）所在章节
6. 主要角色 6-10 个，姓名符合年代感和地域特色，全书不得超表新增有名人物
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


def _extract_cast(outline: str) -> str:
    """从大纲提取角色表段落（兼容带编号的标题：一、主要角色表 / 主要角色表 / 角色表）。"""
    m = re.search(r"#{1,4}\s*(?:[一二三四五六\d]+[、.．]\s*)?(?:主要)?角色表(.+?)(?:\n\s*---|\n#{1,4}\s|\Z)",
                  outline, re.S)
    return m.group(1).strip() if m else ""


def _extract_appellations(outline: str) -> str:
    """从大纲提取称谓约定段落。"""
    m = re.search(r"#{1,4}\s*(?:[一二三四五六\d]+[、.．]\s*)?称谓约定(.+?)(?:\n\s*---|\n#{1,4}\s|\Z)",
                  outline, re.S)
    return m.group(1).strip() if m else ""


def _extract_cast_names(cast: str) -> List[str]:
    """从角色表提取人物姓名。兼容两种格式：
    - markdown 表格：| **赵长山** | 52岁 | ...
    - 列表全角竖线：- **赵长山**｜55岁｜...
    """
    names = []
    for m in re.finditer(r"\*\*([一-龥]{2,4})\*\*\s*[|｜]", cast):
        if m.group(1) not in ("姓名", "名称") and m.group(1) not in names:
            names.append(m.group(1))
    return names


class ContentPipeline:
    def __init__(self, db: Session):
        self.db = db
        self.adapter = LLMAdapter.from_env()
        self.checker = ConsistencyChecker(adapter=self.adapter, enable_semantic=True)
        from ..moderation.review import ReviewService
        self.review = ReviewService()

    def _fact_store(self, book_id: int) -> FactStore:
        # 每本书独立事实库文件（生产可改 PostgreSQL 实现同接口）
        path = os.path.join(tempfile.gettempdir(), f"book_{book_id}_facts.db")
        return FactStore(path, str(book_id))

    def create_book(self, theme_id: int, title: str, chapters: Optional[int] = None) -> Book:
        theme = self.db.get(Theme, theme_id)
        if not theme:
            raise ValueError(f"题材 {theme_id} 不存在")
        n = chapters or theme.target_chapters
        # 生成全书大纲（pro 优先，pro 抖动不可用时降级 flash，保证建书不阻塞）
        prompt = OUTLINE_TMPL.format(title=title, n=n, theme_prompt=theme.prompt_template)
        r = None
        for model in (settings.LLM_MODEL_OUTLINE, settings.LLM_MODEL_CHAPTER):
            try:
                r = self.adapter.generate(prompt, model=model,
                                          system=OUTLINE_SYS, max_tokens=6000, temperature=0.7)
                if model != settings.LLM_MODEL_OUTLINE:
                    logger.warning("大纲生成降级到 %s（pro 不可用）", model)
                break
            except Exception as e:  # noqa
                logger.warning("大纲生成 %s 失败: %s", model, e)
        if r is None:
            raise RuntimeError("大纲生成失败（双模型均不可用）")
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

    def _gen_chapter_core(self, book_id: int, no: int) -> Chapter:
        """章节核心生成（串行环节）：生成 + 人名/衔接校验 + 入库。
        不润色、不提要、不机审——这些走 _post_chapter 异步。"""
        book = self.db.get(Book, book_id)
        if not book:
            raise ValueError(f"书 {book_id} 不存在")
        store = self._fact_store(book_id)
        gen = ChapterGenerator(self.adapter, store, self.checker,
                               model=settings.LLM_MODEL_CHAPTER)
        brief = self._chapter_brief(book.outline, no)
        theme_prompt = book.theme.prompt_template if book.theme else ""
        # 一致性硬约束：角色表 + 上一章结尾 + 下一章前瞻
        cast = _extract_cast(book.outline)
        cast_names = _extract_cast_names(cast)
        appellations = _extract_appellations(book.outline)
        prev = self.db.query(Chapter).filter_by(book_id=book_id, no=no - 1).first()
        prev_tail = prev.content[-800:] if prev and prev.content else ""
        next_brief = self._chapter_brief(book.outline, no + 1) if no < book.total_chapters else ""
        result = gen.generate(no, theme_prompt, brief,
                              cast=cast, cast_names=cast_names,
                              prev_tail=prev_tail, next_brief=next_brief,
                              appellations=appellations, preset=None)
        content = result["content"]
        ch = self.db.query(Chapter).filter_by(book_id=book_id, no=no).first()
        if not ch:
            ch = Chapter(book_id=book_id, no=no)
            self.db.add(ch)
        ch.content = content
        ch.word_count = len(re.sub(r"\s", "", content))
        ch.consistency_conflicts = result["conflicts"]
        ch.review_status = "pending"
        self.db.commit(); self.db.refresh(ch)
        return ch

    def _post_chapter(self, chapter_id: int, title: str, no: int,
                      polish: bool = True):
        """章节后处理（可异步并行）：润色 → 提要 → 机审。失败均降级不阻塞。"""
        ch = self.db.get(Chapter, chapter_id)
        if not ch or not ch.content:
            return
        # 润色 pass（pro 抖动时登记，等补跑）
        if polish:
            try:
                polished = self.polish_text(title, no, ch.content)
                ch.content = polished
                ch.word_count = len(re.sub(r"\s", "", polished))
                self.db.commit()
            except Exception as e:  # noqa
                logger.warning("第%d章润色失败，用初稿: %s", no, e)
        # 章节一句话提要
        try:
            ch.brief = self.gen_brief(ch.content)
            self.db.commit()
        except Exception as e:  # noqa
            logger.warning("第%d章提要生成失败: %s", no, e)
        # 机审
        try:
            res = self.review.review_text(ch.content[:3000])
            ch.review_label = res.get("label", "")
            ch.review_status = "machine_hit" if res.get("hit") else "machine_pass"
            self.db.commit()
        except Exception as e:  # noqa
            logger.warning("第%d章机审失败: %s", no, e)

    def generate_chapter(self, book_id: int, no: int, polish: bool = True) -> Chapter:
        """单章完整生成（同步版，人工单章场景用）。"""
        ch = self._gen_chapter_core(book_id, no)
        book = self.db.get(Book, book_id)
        self._post_chapter(ch.id, book.title, no, polish=polish)
        self.db.refresh(ch)
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
        """流水线生成全书：
        - 章节正文串行（前章衔接依赖）
        - 润色/提要/机审 线程池并行（每章生成完即提交）
        - 失败章节指数退避自愈重试（30/60/120/300s 四轮）
        - 收尾：审计（同步）+ TTS/互动（后台守护线程）
        """
        from concurrent.futures import ThreadPoolExecutor
        book = self.db.get(Book, book_id)
        book.status = "generating"; self.db.commit()
        n = max_chapters or book.total_chapters
        title = book.title
        pool = ThreadPoolExecutor(max_workers=4)
        done, conflicts, failed = 0, 0, []
        # 断点续跑：已有正文的章节跳过生成，但仍补齐后处理
        existing = {c.no: c for c in self.db.query(Chapter).filter_by(book_id=book_id).all()
                    if c.content and len(c.content) >= 800}
        if existing:
            logger.info("书%d 断点续跑：跳过已生成章节 %s", book_id, sorted(existing))
        done = len(existing)
        for no, c in existing.items():
            pool.submit(self._post_safe, c.id, title, no)

        def _gen_one(no: int):
            ch = self._gen_chapter_core(book_id, no)
            pool.submit(self._post_safe, ch.id, title, no)
            return ch

        for no in range(1, n + 1):
            if no in existing:
                continue  # 断点续跑：跳过
            try:
                ch = _gen_one(no)
                done += 1
                if ch.consistency_conflicts:
                    conflicts += 1
            except Exception as e:  # noqa
                logger.error("第%d章生成失败（待自愈重试）: %s", no, e)
                failed.append(no)

        # 自愈重试：指数退避，扛模型长时抖动
        for wait in (30, 60, 120, 300):
            if not failed:
                break
            logger.info("自愈重试：%d 秒后重试章节 %s", wait, failed)
            time.sleep(wait)
            retry, failed = failed, []
            for no in retry:
                try:
                    ch = _gen_one(no)
                    done += 1
                    if ch.consistency_conflicts:
                        conflicts += 1
                except Exception as e:  # noqa
                    logger.error("第%d章自愈重试仍失败: %s", no, e)
                    failed.append(no)
        if failed:
            logger.error("书%d 最终失败章节: %s", book_id, failed)

        pool.shutdown(wait=True)  # 等润色/提要/机审全部落地
        book = self.db.get(Book, book_id)  # refresh（后处理线程改过章节）
        book.status = "reviewing"; self.db.commit()
        # 编辑通读 pass：抓跨章细节问题并自动修复，节奏问题记入审计报告
        try:
            from .editor import EditorPass
            editor_report = EditorPass(self.db, self.adapter).run(book_id)
            logger.info("书%d编辑通读: 细节问题%d处 修复%d章 节奏问题%d处",
                        book_id, editor_report["detail_issues_found"],
                        editor_report["detail_fixed"], len(editor_report["pace_issues"]))
        except Exception as e:  # noqa
            logger.error("编辑通读失败: %s", e)
            editor_report = None
        # 全书一致性审计（生成后自动检验，报告入库，上架门槛依据）
        try:
            report = self.consistency_audit(book_id)
            if editor_report:
                report["editor"] = editor_report
                book = self.db.get(Book, book_id)
                book.audit_report = report
                self.db.commit()
            logger.info("书%d一致性审计: %s 违规%d 低覆盖%d",
                        book_id, "PASS" if report["passed"] else "FAIL",
                        len(report["violations"]), len(report["low_coverage"]))
        except Exception as e:  # noqa
            logger.error("一致性审计失败: %s", e)
        # TTS + 互动：后台守护线程自动补齐（不阻塞任务返回）
        try:
            t = threading.Thread(target=self._enrich_book, args=(book_id,), daemon=True)
            t.start()
        except Exception as e:  # noqa
            logger.error("TTS/互动收尾线程启动失败: %s", e)
        return {"book_id": book_id, "chapters_done": done,
                "conflict_chapters": conflicts, "failed": failed}

    def _post_safe(self, chapter_id: int, title: str, no: int):
        """线程池安全的后处理：独立 DB session。"""
        from ...db import SessionLocal
        db = SessionLocal()
        try:
            ContentPipeline(db)._post_chapter(chapter_id, title, no)
        except Exception as e:  # noqa
            logger.error("第%d章后处理异常: %s", no, e)
        finally:
            db.close()

    def _enrich_book(self, book_id: int):
        """全书 TTS + 互动节点后台补齐（独立 session，失败降级）。"""
        from ...db import SessionLocal
        db = SessionLocal()
        try:
            from ..interact.service import InteractService
            svc = InteractService(db)
            pipe = ContentPipeline(db)
            chapters = db.query(Chapter).filter_by(book_id=book_id).order_by(Chapter.no).all()
            for ch in chapters:
                try:
                    pipe.synthesize_chapter_audio(ch.id)
                except Exception as e:  # noqa
                    logger.warning("TTS ch%s 失败: %s", ch.id, e)
                for attempt in range(3):
                    try:
                        svc.get_or_create_node(ch.id)
                        break
                    except Exception as e:  # noqa
                        logger.warning("互动 ch%s 试%d失败: %s", ch.id, attempt+1, e)
                        time.sleep(10)
            logger.info("书%d TTS/互动收尾完成", book_id)
        except Exception as e:  # noqa
            logger.error("书%d TTS/互动收尾异常: %s", book_id, e)
        finally:
            db.close()

    @staticmethod
    def _chapter_brief(outline: str, no: int) -> str:
        prefix = f"第{no}章"
        for line in outline.splitlines():
            s = line.strip().lstrip("*- ").strip()
            if s.startswith(prefix):
                # 防前缀误匹配：第1章 ≠ 第10章
                rest = s[len(prefix):]
                if not rest or not rest[0].isdigit():
                    return s.rstrip("*").strip()
            if s.startswith(f"{no}."):
                return s.rstrip("*").strip()
        return f"第{no}章"

    def consistency_audit(self, book_id: int) -> Dict:
        """全书一致性审计：人物出场矩阵 + 表外人物 + 主线人物覆盖率 + 门槛判定。

        检验项：
        1. 表外人物：每章 strict 人名校验（老X头类称呼必须在角色表内）
        2. 主线人物覆盖率：第1章出现的主角，后续章节出场率必须 >= 50%（防中途换人）
        3. 章节完整度：每章字数 >= 1500
        """
        from ..memory.generator import check_characters
        book = self.db.get(Book, book_id)
        cast = _extract_cast(book.outline or "")
        cast_names = _extract_cast_names(cast)
        chapters = sorted(book.chapters, key=lambda c: c.no)

        per_chapter, violations = [], []
        appear_count = {n: 0 for n in cast_names}
        for ch in chapters:
            bad = check_characters(ch.content, cast_names, strict=True)
            # 出场识别：全名或简称（末两字/老+姓）命中
            appeared = []
            for n in cast_names:
                aliases = {n, n[-2:], "老" + n[0]}
                if any(a and a in ch.content for a in aliases):
                    appeared.append(n)
                    appear_count[n] += 1
            per_chapter.append({"no": ch.no, "words": ch.word_count,
                                "appeared": appeared, "violations": bad})
            violations += [{"chapter": ch.no, "v": v} for v in bad]

        n_ch = len(chapters)
        # 主线人物：第1章出场的人物
        main_chars = per_chapter[0]["appeared"] if per_chapter else []
        coverage = {}
        for n in main_chars:
            coverage[n] = round(appear_count[n] / n_ch, 2) if n_ch else 0
        low_coverage = {n: c for n, c in coverage.items() if c < 0.5}
        short_chapters = [p["no"] for p in per_chapter if p["words"] < 1500]

        passed = (not violations) and (not low_coverage) and (not short_chapters)
        report = {
            "book_id": book_id, "passed": passed,
            "cast": cast_names, "chapters": n_ch,
            "violations": violations,
            "main_char_coverage": coverage,
            "low_coverage": low_coverage,
            "short_chapters": short_chapters,
            "per_chapter": per_chapter,
        }
        book.audit_report = report
        self.db.commit()
        return report

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
        if upload:
            audio_url = upload(result.audio_bytes, "mp3")
        else:
            audio_url = self._save_audio_local(chapter_id, result.audio_bytes, "mp3")
        self.db.commit()
        return {"chapter_id": chapter_id, "tts_ok": True,
                "segments": len(segments), "duration_ms": result.duration_ms,
                "failed_paragraphs": result.failed_paragraphs,
                "audio_url": audio_url}

    @staticmethod
    def _save_audio_local(chapter_id: int, audio_bytes: bytes, ext: str) -> str:
        """默认落盘：写入 web/audio（nginx 托管），返回 C 端可访问 URL。"""
        audio_dir = os.environ.get("AUDIO_DIR", "/opt/novel-app/deploy/web/audio")
        os.makedirs(audio_dir, exist_ok=True)
        path = os.path.join(audio_dir, f"ch{chapter_id}.{ext}")
        with open(path, "wb") as f:
            f.write(audio_bytes)
        return f"/audio/ch{chapter_id}.{ext}"
