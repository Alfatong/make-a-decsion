"""海外书试读 pass（挑剔读者视角）
全书生成后分块（每 4 章）试读英文成稿（edited_content 优先，无则 content），
产出结构化修改建议清单写入 EditSuggestion 表，由站长在管理台逐条应用/忽略。

设计要点：
- prompt 用中文写（模型为 DeepSeek，双语能力强），但要求 issue_zh 用中文解释
  （站长不懂英文），excerpt 必须是原文逐字子串（apply 端点按子串校验替换）。
- 每块独立调用，单块失败跳过不阻塞全书。
"""
from __future__ import annotations
import json, logging, re
from typing import Dict, List, Optional

from ...core.config import settings
from ...models import Book, Chapter, EditSuggestion
from ..llm.adapter import LLMAdapter

logger = logging.getLogger(__name__)

PROOFREAD_SYS = ("你是一名付费英文网文的挑剔老读者，口味刁钻，专挑影响阅读体验的毛病，"
                 "但只报真问题，不报口味偏好。")
PROOFREAD_TMPL = """你是英文网文《{title}》的挑剔付费读者，正在试读第 {s}-{e} 章的成稿。站长不懂英文，需要你找出会让他丢失付费读者的真问题。

【第 {s}-{e} 章成稿】
{block}

只挑以下几类真问题（每类每块最多 3 条，宁缺毋滥）：
1. 出戏的现代梗/AI 腔：与题材时代或人设不符的用词、翻译腔、空洞的华丽辞藻
2. 人称/视角错误：第三人称里突然冒出第一人称、名字写错、性别代词错乱
3. 逻辑硬伤：同一章内人物位置/动作/已死角色复活等明显矛盾
4. 节奏塌点：关键爽点被一笔带过、该爽的章不爽、冗长重复段落（指出具体段落）
5. 钩子失效：章末该留悬念却写死了，或悬念突兀到读不懂

输出 JSON 数组（严格遵守，不要输出任何其他文字）：
[{{"chapter":章号,"issue_zh":"用中文向不懂英文的站长解释问题（50字内）","excerpt":"章节原文中需修改的精确片段","replacement":"建议替换成的英文文本"}}]

铁律：
- excerpt 必须是上面原文中逐字摘出的连续片段（标点、大小写都不能改），站长的系统会按子串精确匹配替换，差一个字符都会失败
- excerpt 长度控制在 1-3 句话（20-80 个英文词），太短定位不到、太长替换风险大
- replacement 是替换 excerpt 后的英文文本，要与上下文衔接
- 没有问题就输出空数组 []"""


class ProofreadPass:
    """海外书试读：分块产出 EditSuggestion。"""

    def __init__(self, db, adapter: Optional[LLMAdapter] = None):
        self.db = db
        self.adapter = adapter or LLMAdapter.from_env()

    def run(self, book_id: int, block_size: int = 4) -> Dict:
        book = self.db.get(Book, book_id)
        if not book:
            raise ValueError(f"书 {book_id} 不存在")
        chapters = [c for c in book.chapters if (c.edited_content or c.content)]
        chapters.sort(key=lambda c: c.no)
        if not chapters:
            return {"suggestions": 0, "blocks_done": 0, "blocks_failed": 0}

        total, blocks_done, blocks_failed = 0, 0, 0
        for i in range(0, len(chapters), block_size):
            block = chapters[i:i + block_size]
            text = "\n\n".join(
                f"=== Chapter {c.no} ===\n{c.edited_content or c.content}" for c in block)
            prompt = PROOFREAD_TMPL.format(title=book.title, s=block[0].no,
                                           e=block[-1].no, block=text[:18000])
            items: List[Dict] = []
            done = False
            for model in (settings.LLM_MODEL_OUTLINE, settings.LLM_MODEL_CHAPTER):
                try:
                    r = self.adapter.generate(
                        prompt, model=model, system=PROOFREAD_SYS,
                        max_tokens=2500, temperature=0.3,
                        retry_waits=[15, 60])
                    items = self._parse(r.text)
                    done = True
                    break
                except Exception as e:  # noqa
                    logger.warning("试读第%d块 model=%s 失败: %s",
                                   i // block_size + 1, model, e)
            if not done:
                blocks_failed += 1
                logger.error("试读第%d块双模型均失败，跳过", i // block_size + 1)
                continue
            blocks_done += 1
            for it in items:
                try:
                    ch_no = int(it.get("chapter", 0))
                except Exception:  # noqa
                    continue
                excerpt = (it.get("excerpt") or "").strip()
                replacement = (it.get("replacement") or "").strip()
                if not ch_no or not excerpt or not replacement:
                    continue
                # 落库前预校验：excerpt 不是原文子串的建议直接丢弃（apply 端仍会二次校验）
                ch = next((c for c in block if c.no == ch_no), None)
                src = (ch.edited_content or ch.content) if ch else ""
                if excerpt not in src:
                    logger.warning("第%d章建议 excerpt 非原文子串，丢弃: %s",
                                   ch_no, excerpt[:60])
                    continue
                self.db.add(EditSuggestion(
                    book_id=book_id, chapter_no=ch_no,
                    issue_zh=(it.get("issue_zh") or "")[:500],
                    excerpt=excerpt, replacement=replacement,
                    status="pending"))
                total += 1
            self.db.commit()
        logger.info("书%d试读完成：%d 条建议（%d 块成功 %d 块失败）",
                    book_id, total, blocks_done, blocks_failed)
        return {"suggestions": total, "blocks_done": blocks_done,
                "blocks_failed": blocks_failed}

    @staticmethod
    def _parse(text: str) -> List[Dict]:
        """从模型输出解析 JSON 数组（容错 markdown 代码块包裹）。"""
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, list) else []
        except Exception as e:  # noqa
            logger.warning("试读建议 JSON 解析失败: %s", e)
            return []
