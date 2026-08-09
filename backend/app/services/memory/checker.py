"""一致性校验器（生产版·双层）
S2 验证结论：规则匹配能抓显性冲突（角色复活/道具复现/住处矛盾，已实测），
但会漏"换说法"的隐性冲突（如借条考点被改写规避）。故采用双层：
  L1 规则校验（快、免费）→ L2 语义校验（调廉价模型，补漏网）
"""
from __future__ import annotations
import re, logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# 默认角色称谓表（生产应按 book 配置注入）
DEFAULT_ALIAS = {
    "李长顺": ["李长顺", "长顺", "老李", "秀兰她爹"],
    "刘婶": ["刘婶", "刘家婶子"],
    "赵铁柱": ["赵铁柱", "铁柱", "老赵", "赵叔"],
    "王桂芝": ["王桂芝", "桂芝", "他娘"],
    "李秀兰": ["李秀兰", "秀兰"],
    "王建国": ["王建国", "王场长", "场长"],
}
DEFAULT_ITEMS = ["借条", "先进奖状", "旧手表"]
MOVE_VERBS = r"(说|道|问|答|喊|笑|叹|走进|走来|进来|端|拿|坐)"
ITEM_USE = r"(拿出|掏出|提起|讨要|要回|又提|说事)"


class RuleChecker:
    """L1 确定性规则校验（快、免费）。"""

    def __init__(self, alias: Optional[Dict] = None, items: Optional[List[str]] = None):
        self.alias = alias or DEFAULT_ALIAS
        self.items = items or DEFAULT_ITEMS

    def check(self, content: str, store) -> List[Dict]:
        conflicts: List[Dict] = []
        # 角色复活
        for name, aliases in self.alias.items():
            val, ch = store.latest("character", name, "alive")
            if val in ("去世", "离开林场") and ch is not None:
                for a in aliases:
                    if re.search(re.escape(a) + r".{0,8}" + MOVE_VERBS, content):
                        conflicts.append({"type": "角色复活", "level": "L1",
                            "detail": f"角色「{name}」已于第{ch}章{val}，本章却出场（'{a}'）"})
                        break
        # 道具复现
        for item in self.items:
            val, ch = store.latest("item", item, "state")
            if val in ("已归还", "已丢失") and ch is not None:
                if re.search(ITEM_USE + r".{0,8}" + re.escape(item), content) or \
                   re.search(re.escape(item) + r".{0,8}" + ITEM_USE, content):
                    conflicts.append({"type": "道具复现", "level": "L1",
                        "detail": f"道具「{item}」已于第{ch}章{val}，本章又被使用/讨要"})
        # 住处矛盾
        val, ch = store.latest("character", "李长顺", "location")
        if val and "南屋" in val and ch is not None:
            if re.search(r"(西厢房|西厢).{0,8}(糊窗|居住|住|收拾)", content) or \
               re.search(r"(在|回).{0,4}西厢房", content):
                conflicts.append({"type": "住处矛盾", "level": "L1",
                    "detail": f"已于第{ch}章搬到{val}，本章却仍写在西厢房活动"})
        return conflicts


SEMANTIC_SYS = "你是小说一致性校验器。只输出 JSON，不要解释。"
SEMANTIC_TMPL = """下面是某长篇小说"已确立的事实"和"最新一章正文"。
请判断正文是否与任何一条已确立事实矛盾（包括换了说法的隐性矛盾）。

【已确立的事实】
{facts}

【最新一章正文（节选）】
{content}

只输出 JSON：
{{"has_conflict": true/false, "conflicts": [{{"type":"冲突类型","detail":"具体矛盾说明"}}]}}
无矛盾则 conflicts 为 []。"""


class SemanticChecker:
    """L2 语义校验（调廉价模型，补 L1 漏网）。仅在 L1 通过后再跑，控制成本。"""

    def __init__(self, adapter, model: str = "deepseek-flash"):
        self.adapter = adapter
        self.model = model

    def check(self, content: str, store) -> List[Dict]:
        facts = store.snapshot()
        if not facts.strip():
            return []
        prompt = SEMANTIC_TMPL.format(facts=facts, content=content[:3000])
        try:
            r = self.adapter.generate(prompt, model=self.model, system=SEMANTIC_SYS,
                                      max_tokens=600, temperature=0.0)
            m = re.search(r"\{.*\}", r.text, re.S)
            if not m:
                return []
            import json
            data = json.loads(m.group(0))
            out = []
            for c in data.get("conflicts", []):
                out.append({"type": c.get("type", "语义冲突"), "level": "L2",
                            "detail": c.get("detail", "")})
            return out
        except Exception as e:  # noqa
            logger.warning("SemanticChecker 失败（降级跳过）: %s", e)
            return []  # L2 失败不阻塞，L1 已兜底


class ConsistencyChecker:
    """双层组合：先 L1 规则，L1 无冲突再 L2 语义补漏。"""

    def __init__(self, adapter=None, alias=None, items=None, enable_semantic=True):
        self.l1 = RuleChecker(alias, items)
        self.l2 = SemanticChecker(adapter) if (adapter and enable_semantic) else None

    def check(self, content: str, store) -> List[Dict]:
        l1_hits = self.l1.check(content, store)
        if l1_hits:
            return l1_hits          # L1 已命中，直接返回（省 L2 成本）
        if self.l2:
            return self.l2.check(content, store)  # L1 过，L2 补漏
        return []
