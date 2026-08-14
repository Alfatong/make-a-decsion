"""互动回响生成服务（M2）
读者在互动节点选择后，由 LLM 生成贴合剧情的"回响段"（实时反馈 + 剧情延展）。
回响段生成后缓存进 InteractNode.responses，重选直接命中缓存。
"""
from __future__ import annotations
import re, json, logging
from typing import Dict, List, Optional
from sqlalchemy.orm import Session

from ...core.config import settings
from ...models import Chapter, InteractNode, Book
from ..llm.adapter import LLMAdapter

logger = logging.getLogger(__name__)

ECHO_SYS = "你是老年年代题材长篇小说的作者，根据读者选择续写贴合剧情的回响段，语言口语化温暖。"
ECHO_TMPL = """长篇小说《{title}》第{no}章结尾，剧情如下：

【本章结尾】
{context}

【下一章开场（读者还没看到）】
{next_context}

【读者面临的抉择】
{question}

【读者的选择】
{choice}

请写一段 80-150 字的"回响段"：
1. 承接本章结尾剧情，让读者感觉这个选择"有分量"
2. 如果读者的选择接近下一章的实际走向，回响段要给出"选对了"的暗示和期待感；如果走向不同，回响段要写出"这个选择也有它的道理"的回味
3. 结尾自然勾起读者翻下一章的欲望
直接输出回响段正文，不要标题。"""

GEN_NODE_SYS = "你是长篇小说互动设计编辑，擅长在章节结尾设计让读者纠结又期待的抉择。只输出 JSON。"
GEN_NODE_TMPL = """长篇小说《{title}》第{no}章刚读完，读者要做一个影响心情的抉择。

【本章结尾剧情（读者刚读到这里）】
{context}

【下一章实际走向（读者不知道，你要利用它）】
{next_context}

设计一个互动节点，要求：
1. question：必须紧扣本章结尾的具体事件或悬念，并引用本章结尾的实际场景细节（人物、物件、原话），让读者觉得"这问题问到我心坎上了"
2. 【铁律】question 和 options 里只能出现本章已经写到的人物、事件、物件。下一章走向只用于暗中校准选项方向，【绝对禁止】把本章没有出现的新人物、新事件、新物件写进问题或选项（读者还没看到，会出戏）
3. options[0]：与下一章实际走向暗中呼应的选择，但表述必须完全用本章已有的信息（读者选了它，下一章会有"猜中了"的满足感）
4. options[1]：另一种符合人物性格、但剧情未采用的走向（让读者纠结）
5. 两个选项都要具体、有画面感，体现不同的处事态度，不能是"继续看/不看了"这种假选择
6. 【输出前必须自查】把问题、选项A、选项B 里出现的每个人名、物件、事件逐一核对，是否在本章结尾剧情里真实出现过；但凡有一个是本章没写过的（哪怕来自下一章走向），必须改写成本章已有的元素。自查通过后才输出。

严格按以下三行格式输出（不要输出任何其他内容）：
问题：<互动提问>
选项A：<选项一>
选项B：<选项二>"""


def _parse_node(text: str, default_q: str, default_opts: List[str]):
    """鲁棒解析：优先按行格式，回退 JSON，再回退兜底。"""
    q, opts = None, []
    for line in text.splitlines():
        s = line.strip().strip('"').rstrip('",')
        if s.startswith("问题：") or s.startswith("问题:"):
            q = s.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif s.startswith("选项A：") or s.startswith("选项A:"):
            opts.insert(0, s.split("：", 1)[-1].split(":", 1)[-1].strip())
        elif s.startswith("选项B：") or s.startswith("选项B:"):
            opts.append(s.split("：", 1)[-1].split(":", 1)[-1].strip())
    if q and len(opts) >= 2:
        return q, opts[:2]
    # 回退 JSON
    try:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            d = json.loads(m.group(0))
            q = d.get("question") or default_q
            o = d.get("options") or []
            if isinstance(o, list) and len(o) >= 2:
                return q, o[:2]
    except Exception:  # noqa
        pass
    return default_q, default_opts


class InteractService:
    def __init__(self, db: Session):
        self.db = db
        self.adapter = LLMAdapter.from_env()

    def _chapter_context(self, ch: Chapter, tail: bool = True, n: int = 1200) -> str:
        """取章节上下文：互动看结尾（tail=True），不是开头。"""
        return ch.content[-n:] if tail else ch.content[:n]

    def _next_chapter(self, ch: Chapter) -> Optional[Chapter]:
        return (self.db.query(Chapter)
                .filter(Chapter.book_id == ch.book_id, Chapter.no == ch.no + 1)
                .first())

    def get_or_create_node(self, chapter_id: int) -> InteractNode:
        node = self.db.query(InteractNode).filter_by(chapter_id=chapter_id).first()
        if node:
            return node
        ch = self.db.get(Chapter, chapter_id)
        book = self.db.get(Book, ch.book_id)
        next_ch = self._next_chapter(ch)
        next_ctx = self._chapter_context(next_ch, tail=False, n=500) if next_ch else "（全书最后一章，没有后续）"
        prompt = GEN_NODE_TMPL.format(
            title=book.title, no=ch.no,
            context=self._chapter_context(ch, tail=True, n=1500),
            next_context=next_ctx)
        question, options = None, None
        # flash 优先，失败或解析兜底则降级 pro 再试（flash 偶发空响应）
        # 长退避扛模型抖动，避免读者看到"互动加载失败"
        for model in (settings.LLM_MODEL_CHAPTER, settings.LLM_MODEL_OUTLINE):
            try:
                r = self.adapter.generate(prompt, model=model,
                                          system=GEN_NODE_SYS, max_tokens=900,
                                          temperature=0.7, retry_waits=[10, 30, 60])
                q, o = _parse_node(r.text, "", [])
                if q and len(o) >= 2:
                    question, options = q, o
                    break
                logger.warning("互动节点解析失败 model=%s ch=%s，尝试下一档", model, chapter_id)
            except Exception as e:  # noqa
                logger.warning("互动节点生成失败 model=%s ch=%s: %s", model, chapter_id, e)
        if not question:
            raise RuntimeError(f"互动节点生成失败 ch{chapter_id}（双模型均不可用）")
        node = InteractNode(chapter_id=chapter_id, question=question,
                            options=options, position=0, responses={})
        self.db.add(node); self.db.commit(); self.db.refresh(node)
        # 预生成两个选项的回响（读者选择时秒出，不现场调 LLM）
        for idx in range(len(options)):
            try:
                self._gen_echo(node, ch, book, next_ctx, options[idx], str(idx))
            except Exception as e:  # noqa
                logger.warning("回响预生成失败 ch%s opt%s: %s", chapter_id, idx, e)
        self.db.refresh(node)
        return node

    def _gen_echo(self, node: InteractNode, ch: Chapter, book: Book,
                  next_ctx: str, choice: str, key: str) -> str:
        """生成单个选项的回响并写入缓存（flash 失败降级 pro）。"""
        prompt = ECHO_TMPL.format(title=book.title, no=ch.no,
                                  context=self._chapter_context(ch, tail=True, n=1200),
                                  next_context=next_ctx,
                                  question=node.question, choice=choice)
        echo = ""
        for model in (settings.LLM_MODEL_CHAPTER, settings.LLM_MODEL_OUTLINE):
            try:
                r = self.adapter.generate(prompt, model=model,
                                          system=ECHO_SYS, max_tokens=400, temperature=0.75)
                if r.text.strip():
                    echo = r.text.strip()
                    break
            except Exception:  # noqa
                continue
        if not echo:
            raise RuntimeError("回响生成失败")
        resp = dict(node.responses or {}); resp[key] = echo
        node.responses = resp
        self.db.commit()
        return echo

    def choose(self, chapter_id: int, option_idx: int) -> Dict:
        """读者选择：返回回响段（优先缓存，否则 LLM 生成并缓存）。"""
        node = self.get_or_create_node(chapter_id)
        key = str(option_idx)
        if key in (node.responses or {}):
            return {"echo": node.responses[key], "cached": True,
                    "question": node.question, "options": node.options}
        ch = self.db.get(Chapter, chapter_id)
        book = self.db.get(Book, ch.book_id)
        next_ch = self._next_chapter(ch)
        next_ctx = self._chapter_context(next_ch, tail=False, n=600) if next_ch else "（全书完）"
        choice = node.options[option_idx] if option_idx < len(node.options) else node.options[0]
        # 兜底：预生成缺失时才现场生成（正常预生成后走不到这里）
        echo = self._gen_echo(node, ch, book, next_ctx, choice, key)
        return {"echo": echo, "cached": False,
                "question": node.question, "options": node.options}
