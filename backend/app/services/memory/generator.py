"""章节生成编排（生产版）
串联：事实预置 → 生成前注入快照 → 生成（带空章重试）→ 双层校验 → 模型增量补充。
对应 S2 验证后的生产形态。
"""
from __future__ import annotations
import logging
from typing import List, Dict, Optional
from .fact_store import FactStore
from .checker import ConsistencyChecker

logger = logging.getLogger(__name__)

GEN_SYS = ("你是一位擅长为60岁以上老年读者创作年代家庭题材长篇小说的作家。"
           "语言口语化、温暖厚道，不用网络用语，严格遵循给定的故事事实。")

# 可信主体白名单：硬事实只信大纲预置，模型抽取不覆盖
TRUSTED_KEYS = {("借条", "state"), ("刘婶", "alive"), ("李长顺", "location"), ("李长顺", "alive")}


class ChapterGenerator:
    def __init__(self, adapter, store: FactStore, checker: ConsistencyChecker,
                 model: str = "deepseek-flash", max_gen_retries: int = 2):
        self.adapter = adapter
        self.store = store
        self.checker = checker
        self.model = model
        self.max_gen_retries = max_gen_retries  # 空章/截断重试

    def preset_facts(self, chapter: int, facts: List[Dict]):
        """大纲预置：章节生成后，把该章规定的硬事实入库（最高优先级）。"""
        for f in facts:
            self.store.add(f["fact_type"], f["subject"], f["attr"], f["value"],
                           chapter, source="outline")

    def _gen_once(self, prompt: str) -> str:
        r = self.adapter.generate(prompt, model=self.model, system=GEN_SYS,
                                  max_tokens=4000, temperature=0.75)
        return r.text.strip()

    def generate(self, chapter: int, theme: str, brief: str,
                 preset: Optional[List[Dict]] = None) -> Dict:
        """生成一章并做一致性校验。返回 {content, conflicts, retries, words}"""
        memory = self.store.snapshot()
        mem_block = (f"\n【已确立的故事事实（必须严格遵守）】\n{memory}\n" if memory else "")
        prompt = (f"请创作小说第{chapter}章。\n\n【题材设定】\n{theme}{mem_block}"
                  f"\n【本章大纲】\n{brief}\n\n要求：只写正文，2500-3500字，遵循事实，"
                  f"保持人物/道具/住处/时间一致，直接输出正文。")

        # 空章/过短自动重试
        content, retries = "", 0
        for attempt in range(self.max_gen_retries + 1):
            retries = attempt
            content = self._gen_once(prompt)
            if len(content) >= 1500:  # 达到正常章节长度
                break
            logger.warning("第%d章第%d次生成过短(%d字)，重试", chapter, attempt+1, len(content))
        if len(content) < 800:
            raise RuntimeError(f"第{chapter}章多次生成仍过短（{len(content)}字）")

        # 双层一致性校验
        conflicts = self.checker.check(content, self.store)

        # 预置硬事实入库（无论是否冲突，事实以大纲为准）
        if preset:
            self.preset_facts(chapter, preset)

        return {"content": content, "conflicts": conflicts,
                "gen_retries": retries, "words": len(content),
                "need_review": len(conflicts) > 0}

    def add_model_facts(self, chapter: int, facts: List[Dict]):
        """模型增量补充：不覆盖可信主体，source=model。"""
        added = 0
        for f in facts:
            key = (f.get("subject", ""), f.get("attr", ""))
            if key in TRUSTED_KEYS:
                continue
            self.store.add(f.get("fact_type", "character"), f.get("subject", ""),
                           f.get("attr", "state"), f.get("value", ""), chapter, source="model")
            added += 1
        return added
