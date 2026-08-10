"""互动回响生成服务（M2）
读者在互动节点选择后，由 LLM 生成贴合剧情的"回响段"（实时反馈 + 剧情延展）。
回响段生成后缓存进 InteractNode.responses，重选直接命中缓存。
"""
from __future__ import annotations
import re, json, logging
from typing import Dict, List
from sqlalchemy.orm import Session

from ...core.config import settings
from ...models import Chapter, InteractNode, Book
from ..llm.adapter import LLMAdapter

logger = logging.getLogger(__name__)

ECHO_SYS = "你是老年年代题材长篇小说的作者，根据读者选择续写贴合剧情的回响段，语言口语化温暖。"
ECHO_TMPL = """长篇小说《{title}》第{no}章，读者刚读到以下内容（节选）：

【上下文】
{context}

【互动提问】
{question}

【读者的选择】
{choice}

请根据读者的选择，写一段 80-150 字的"回响段"：承接上下文、体现读者选择带来的走向，语气温暖厚道，作为剧情的即时回应。直接输出回响段正文，不要标题。"""

GEN_NODE_SYS = "你是长篇小说互动设计编辑，输出 JSON。"
GEN_NODE_TMPL = """为长篇小说《{title}》第{no}章设计一个互动节点。

【章节内容节选】
{context}

设计一个贴合剧情的互动提问和 2 个走向选项（一个平稳、一个波折）。
只输出 JSON：{{"question":"互动提问","options":["选项A","选项B"]}}"""


class InteractService:
    def __init__(self, db: Session):
        self.db = db
        self.adapter = LLMAdapter.from_env()

    def get_or_create_node(self, chapter_id: int) -> InteractNode:
        node = self.db.query(InteractNode).filter_by(chapter_id=chapter_id).first()
        if node:
            return node
        ch = self.db.get(Chapter, chapter_id)
        book = self.db.get(Book, ch.book_id)
        context = ch.content[:1500]
        prompt = GEN_NODE_TMPL.format(title=book.title, no=ch.no, context=context)
        r = self.adapter.generate(prompt, model=settings.LLM_MODEL_CHAPTER,
                                  system=GEN_NODE_SYS, max_tokens=400, temperature=0.7)
        question, options = "读到这儿，你想怎么发展？", ["平平淡淡接着过", "来点波折添看头"]
        try:
            m = re.search(r"\{.*\}", r.text, re.S)
            if m:
                d = json.loads(m.group(0))
                question = d.get("question", question)
                opts = d.get("options", [])
                if isinstance(opts, list) and len(opts) >= 2:
                    options = opts[:2]
        except Exception:  # noqa
            pass
        node = InteractNode(chapter_id=chapter_id, question=question,
                            options=options, position=0, responses={})
        self.db.add(node); self.db.commit(); self.db.refresh(node)
        return node

    def choose(self, chapter_id: int, option_idx: int) -> Dict:
        """读者选择：返回回响段（优先缓存，否则 LLM 生成并缓存）。"""
        node = self.get_or_create_node(chapter_id)
        key = str(option_idx)
        if key in (node.responses or {}):
            return {"echo": node.responses[key], "cached": True,
                    "question": node.question, "options": node.options}
        ch = self.db.get(Chapter, chapter_id)
        book = self.db.get(Book, ch.book_id)
        choice = node.options[option_idx] if option_idx < len(node.options) else node.options[0]
        prompt = ECHO_TMPL.format(title=book.title, no=ch.no,
                                  context=ch.content[-1500:],
                                  question=node.question, choice=choice)
        r = self.adapter.generate(prompt, model=settings.LLM_MODEL_CHAPTER,
                                  system=ECHO_SYS, max_tokens=400, temperature=0.75)
        echo = r.text.strip()
        resp = dict(node.responses or {}); resp[key] = echo
        node.responses = resp
        self.db.commit()
        return {"echo": echo, "cached": False,
                "question": node.question, "options": node.options}
