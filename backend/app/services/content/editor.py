"""编辑通读 pass（第八道防线）
全书生成后，编辑角色分块通读全书：
1. 抓跨章细节问题（称谓漂移/道具跨章漂移/人物状态矛盾/时间线错误）
2. 评估节奏（哪几章太平、钩子缺失）——只报告不自动改
3. 细节类问题自动修复：带着问题清单局部重写问题章节（保持前后衔接）
"""
from __future__ import annotations
import json, logging, re
from typing import Dict, List

from ...core.config import settings
from ...models import Book, Chapter
from ..llm.adapter import LLMAdapter

logger = logging.getLogger(__name__)

EDITOR_SYS = "你是长篇小说的责任主编，通读稿件抓错：细节矛盾一处不放过，评价节奏一针见血。"
EDITOR_TMPL = """通读长篇小说《{title}》的第{s}-{e}章（连续章节），以主编身份抓问题。

【全书角色表】
{cast}

【称谓约定】
{appellations}

【全书情节线（校验基准：每条线的相邻节点章之间必须有承接）】
{storylines}

【第{s}-{e}章正文】
{block}

抓两类问题：
A. 细节错误（可自动修复）：
   - 称谓漂移：同一人物对同一对象的称呼前后不一（与称谓约定不符）
   - 道具矛盾：同一物件的位置/状态跨章或章内不一致（怀里变包里、已送出又出现）
   - 人物状态矛盾：行为与身份/学历/年龄/伤病状况冲突
   - 时间线错误：季节/时辰/日期前后冲突
   - 事件矛盾：同一事件/悬念在不同章节的指称或结果不一致（如前一章说"分房名额被压下"，后一章说成"夜大名额被挡"；前一章说某人不知情，后一章写他早就知道）
   - 悬念失联：前文已抛出的事件/悬念（名额被压、秘密发现一半、未赴的约）在本块后续章节中被丢弃——既不推进也不呼应，各说各话（注意：两个独立事件本身合理，缺的是它们之间的呼应句；修复方向是补呼应，不是删事件）
   - 线索断裂：对照【全书情节线】，某条线上相邻节点章之间没有承接（线上前一章的事件/情绪在本章毫无延续），或交汇点章节没写出线之间的呼应
B. 节奏问题（只报告）：哪一章平淡无冲突、章末无钩子、连压3章以上无释放

输出 JSON（严格遵守，不要输出任何其他文字）：
{{"detail_issues":[{{"chapter":章号,"desc":"问题描述(30字内)","fix_hint":"怎么改(30字内)"}}],
  "pace_issues":[{{"chapter":章号,"desc":"节奏问题(30字内)"}}]}}
没有问题就输出空数组。"""

REVISE_SYS = "你是小说改稿编辑，按修改意见局部修订章节，不动剧情主线，保持与前后章衔接。"
REVISE_TMPL = """修订长篇小说《{title}》第{no}章。编辑通读发现以下问题，逐条修正：

【本章问题清单】
{issues}

【上一章结尾（衔接约束，不得矛盾）】
…{prev_tail}

【下一章开头（衔接约束，不得矛盾）】
{next_head}

【称谓约定（必须遵守）】
{appellations}

【本章正文（待修订）】
{content}

要求：
1. 只改问题清单涉及的细节（称谓/道具/状态/时间线），剧情走向、场景、对话走向一律不动
2. 与上一章结尾、下一章开头保持衔接
3. 字数保持原稿的 90%-110%
直接输出修订后的全文，不要标题，不要解释。"""


# ---------- 海外分支（market="overseas"）：英文通读/修订 prompt ----------
EDITOR_SYS_EN = ("You are the editor-in-chief of an English web novel. You read the full "
                 "manuscript and catch every detail slip; your pacing verdicts are sharp.")
EDITOR_TMPL_EN = """Read Chapters {s}-{e} (consecutive) of the English web novel "{title}" and find problems as editor-in-chief.

[Main Cast]
{cast}

[Naming Conventions]
{appellations}

[Plot Lines (baseline: adjacent node chapters on a line must carry over)]
{storylines}

[Chapters {s}-{e} Text]
{block}

Find two kinds of problems:
A. Detail errors (auto-fixable):
   - Naming drift: the same character addresses the same person inconsistently (against the naming conventions)
   - Prop contradiction: an object's location/state conflicts across or within chapters
   - Character state contradiction: behavior conflicting with identity/age/injury status
   - Timeline errors: season/time-of-day/date conflicts
   - Event contradiction: the same event/mystery is described or resolved inconsistently across chapters
   - Dropped suspense: a planted mystery (a secret half-found, an unkept appointment) is abandoned without any callback in later chapters of this block (fix direction: add a callback, not delete events)
   - Line break: per the plot lines, adjacent node chapters on a line show no carry-over, or a collision chapter misses the echo between lines
B. Pacing problems (report only): which chapter is flat with no conflict, which ends with no hook, 3+ consecutive suppressed chapters with no release

Output JSON strictly, nothing else:
{{"detail_issues":[{{"chapter":chapter number,"desc":"problem (under 30 words)","fix_hint":"how to fix (under 30 words)"}}],
  "pace_issues":[{{"chapter":chapter number,"desc":"pacing problem (under 30 words)"}}]}}
Empty arrays if no problems."""

REVISE_SYS_EN = ("You are a line editor revising a chapter per the fix list. Do not touch "
                 "the plot; keep continuity with neighboring chapters.")
REVISE_TMPL_EN = """Revise Chapter {no} of the English web novel "{title}". The editor pass found the problems below; fix them one by one:

[Fix List for This Chapter]
{issues}

[Previous Chapter Ending (continuity constraint, must not contradict)]
…{prev_tail}

[Next Chapter Opening (continuity constraint, must not contradict)]
{next_head}

[Naming Conventions (must obey)]
{appellations}

[Chapter Text (to revise)]
{content}

Requirements:
1. Only fix the listed details (naming/props/state/timeline); plot, scenes and dialogue direction stay untouched
2. Keep continuity with the previous chapter's ending and the next chapter's opening
3. Keep the length at 90%-110% of the original
Output the full revised text directly, no title, no commentary."""


class EditorPass:
    """全书编辑通读 + 自动修复。generate_book 收尾阶段调用。"""

    def __init__(self, db, adapter: LLMAdapter):
        self.db = db
        self.adapter = adapter

    def run(self, book_id: int, block_size: int = 4) -> Dict:
        book = self.db.get(Book, book_id)
        overseas = (getattr(book, "market", None) or "cn") == "overseas"
        chapters = [c for c in book.chapters if c.content]
        chapters.sort(key=lambda c: c.no)
        if not chapters:
            return {"detail_fixed": 0, "pace_issues": []}
        # 海外书：读修订稿优先（edited_content 无则 content）；国内书仍只读 content
        def _src(c) -> str:
            return (c.edited_content or c.content) if overseas else c.content
        from .pipeline import (_extract_cast, _extract_appellations, _extract_storylines,
                               _extract_cast_en, _extract_appellations_en, _extract_storylines_en)
        if overseas:
            cast = _extract_cast_en(book.outline)
            appellations = _extract_appellations_en(book.outline)
            storylines = _extract_storylines_en(book.outline)
            editor_tmpl, editor_sys = EDITOR_TMPL_EN, EDITOR_SYS_EN
            revise_tmpl, revise_sys = REVISE_TMPL_EN, REVISE_SYS_EN
            none_str = "(none)"
        else:
            cast = _extract_cast(book.outline)
            appellations = _extract_appellations(book.outline)
            storylines = _extract_storylines(book.outline)
            editor_tmpl, editor_sys = EDITOR_TMPL, EDITOR_SYS
            revise_tmpl, revise_sys = REVISE_TMPL, REVISE_SYS
            none_str = "（无）"

        all_detail: List[Dict] = []
        all_pace: List[Dict] = []
        # 1. 分块通读（pro 优先，长退避扛抖动，失败降级 flash）
        for i in range(0, len(chapters), block_size):
            block = chapters[i:i + block_size]
            if overseas:
                text = "\n\n".join(f"=== Chapter {c.no} ===\n{_src(c)}" for c in block)
            else:
                text = "\n\n".join(f"=== 第{c.no}章 ===\n{c.content}" for c in block)
            prompt = editor_tmpl.format(title=book.title, s=block[0].no, e=block[-1].no,
                                        cast=cast[:1500], appellations=appellations or none_str,
                                        storylines=storylines or none_str,
                                        block=text[:18000])
            done = False
            for model in (settings.LLM_MODEL_OUTLINE, settings.LLM_MODEL_CHAPTER):
                try:
                    r = self.adapter.generate(
                        prompt, model=model, system=editor_sys,
                        max_tokens=2000, temperature=0.2,
                        retry_waits=[15, 60, 120])
                    m = re.search(r"\{.*\}", r.text, re.S)
                    if m:
                        data = json.loads(m.group(0))
                        all_detail += data.get("detail_issues", [])
                        all_pace += data.get("pace_issues", [])
                    done = True
                    break
                except Exception as e:  # noqa
                    logger.warning("编辑通读第%d块 model=%s 失败: %s",
                                   i // block_size + 1, model, e)
            if not done:
                logger.error("编辑通读第%d块双模型均失败，跳过", i // block_size + 1)

        # 2. 细节问题按章聚合 → 自动修订
        by_chapter: Dict[int, List[Dict]] = {}
        for iss in all_detail:
            try:
                by_chapter.setdefault(int(iss.get("chapter", 0)), []).append(iss)
            except Exception:  # noqa
                continue
        fixed = 0
        ch_map = {c.no: c for c in chapters}
        for no, issues in by_chapter.items():
            ch = ch_map.get(no)
            if not ch:
                continue
            prev = ch_map.get(no - 1)
            nxt = ch_map.get(no + 1)
            issue_text = "\n".join(f"- {i.get('desc','')}（改法：{i.get('fix_hint','')}）" for i in issues)
            base = _src(ch)
            first_str, last_str = ("(first chapter)", "(last chapter)") if overseas \
                else ("（第一章）", "（最后一章）")
            for model in (settings.LLM_MODEL_OUTLINE, settings.LLM_MODEL_CHAPTER):
                try:
                    r = self.adapter.generate(
                        revise_tmpl.format(title=book.title, no=no, issues=issue_text,
                                           prev_tail=(_src(prev)[-400:] if prev else first_str),
                                           next_head=(_src(nxt)[:400] if nxt else last_str),
                                           appellations=appellations or none_str,
                                           content=base),
                        model=model, system=revise_sys,
                        max_tokens=6000, temperature=0.4,
                        retry_waits=[15, 60, 120])
                    text = r.text.strip()
                    if len(text) >= len(base) * 0.7:
                        if overseas:
                            # 海外书：首次修订前备份原稿，修订稿写 edited_content，不动 content
                            if not ch.raw_content:
                                ch.raw_content = ch.content
                            ch.edited_content = text
                            ch.word_count = len(text.split())
                        else:
                            ch.content = text
                            ch.word_count = len(re.sub(r"\s", "", text))
                        ch.tts_segments = []  # 内容变了，TTS 待重跑
                        fixed += 1
                        logger.info("第%d章编辑修订完成（%d处问题）", no, len(issues))
                    break
                except Exception as e:  # noqa
                    logger.warning("第%d章修订 model=%s 失败: %s", no, model, e)
        self.db.commit()
        return {"detail_issues_found": len(all_detail), "detail_fixed": fixed,
                "pace_issues": [f"第{p.get('chapter')}章: {p.get('desc')}" for p in all_pace]}
