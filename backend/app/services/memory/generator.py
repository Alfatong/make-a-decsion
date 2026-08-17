"""章节生成编排（生产版）
串联：事实预置 → 生成前注入快照 → 生成（带空章重试）→ 双层校验 → 模型增量补充。
对应 S2 验证后的生产形态。
"""
from __future__ import annotations
import re, logging
from typing import List, Dict, Optional
from .fact_store import FactStore
from .checker import ConsistencyChecker

logger = logging.getLogger(__name__)

GEN_SYS = ("你是一位擅长为60岁以上老年读者创作年代家庭题材长篇小说的作家。"
           "语言口语化、温暖厚道，不用网络用语，严格遵循给定的故事事实。")

# 可信主体白名单：硬事实只信大纲预置，模型抽取不覆盖
TRUSTED_KEYS = {("借条", "state"), ("刘婶", "alive"), ("李长顺", "location"), ("李长顺", "alive")}

CONTINUITY_SYS = "你是小说编辑，专查章节间的剧情矛盾。只输出结论，简洁直接。"
CONTINUITY_TMPL = """检查下面这段小说正文中，人物做出的【具体约定、计划、承诺】是否与后续大纲冲突。

【正文】
{content}

【下一章大纲】
{next_brief}

规则：
1. 只关注正文里明确说出的具体安排（谁和谁、什么时候、去做什么）
2. 如果这些安排与下一章大纲矛盾（比如正文说甲和乙去巡山，大纲下一章是甲和丙去巡山），就是冲突
3. 没有具体安排、或安排与大纲兼容，都算无冲突
4. 【抢跑也算冲突】下一章大纲的核心事件（如巡山救人、比赛、葬礼）不得在本章正文中已经出发、已经发生或已经完成——本章最多铺垫到"决定去/准备去"，不能提前开演

输出格式（严格遵守）：
无冲突 → 只输出：OK
有冲突 → 输出：CONFLICT: 一句话说明冲突点"""

DETAIL_CHECK_SYS = "你是小说校对编辑，专查细节一致性错误，眼里揉不得沙子，但只报确凿的错。"
DETAIL_CHECK_TMPL = """校对下面这一章正文，只查三类细节错误：

【全书角色表】
{cast}

【称谓约定（全书对话必须遵守）】
{appellations}

【本章正文】
{content}

要查的三类错误：
1. 称谓违规：人物对话/叙述中的互相称呼与"称谓约定"不一致（如约定兄弟间谈论母亲说"咱妈"，正文却写成"我妈"）
2. 状态违规：人物行为与其身份/学历/年龄/身体状况矛盾（如已毕业工作的人还在写作业、目盲的老人在看书报、卧病的人挑水）
3. 道具违规：同一物件在本章内位置或状态前后矛盾（如前文说两本书都揣在怀里，后文却从包里取出书；前文说灯已吹灭，后文借灯光缝衣）

规则：
1. 只报确凿的违规，拿不准、可解释的一律不报
2. 每类最多报 2 条
3. 输出格式：每条一行，"类型: 具体描述"（不超过40字）
4. 没有违规就只输出一个字：无
"""


def check_details(adapter, content: str, cast: str, appellations: str,
                  model: str) -> List[str]:
    """细节一致性校验：称谓/人物状态/道具连续性。返回违规列表（空=通过）。"""
    if not content:
        return []
    try:
        r = adapter.generate(
            DETAIL_CHECK_TMPL.format(cast=cast[:1500], appellations=appellations or "（无）",
                                     content=content[:6000]),
            model=model, system=DETAIL_CHECK_SYS, max_tokens=300, temperature=0.1)
        text = r.text.strip()
        if text in ("无", "无。", ""):
            return []
        return [ln.strip() for ln in text.splitlines() if ln.strip() and "无" != ln.strip()][:6]
    except Exception as e:  # noqa
        logger.warning("细节校验失败（跳过）: %s", e)
        return []


def check_continuity(adapter, content: str, next_brief: str,
                     model: str) -> Optional[str]:
    """语义级衔接校验：正文中的具体约定是否与下一章大纲冲突。
    返回 None=无冲突，否则返回冲突描述。失败降级为 None（不阻塞）。"""
    if not next_brief:
        return None
    try:
        r = adapter.generate(
            CONTINUITY_TMPL.format(content=content[:3500], next_brief=next_brief),
            model=model, system=CONTINUITY_SYS, max_tokens=120, temperature=0.1)
        text = r.text.strip()
        if text.upper().startswith("CONFLICT"):
            return text.split(":", 1)[-1].strip()[:100]
        return None
    except Exception as e:  # noqa
        logger.warning("衔接校验失败（降级跳过）: %s", e)
        return None


def check_characters(content: str, cast_names: List[str],
                     strict: bool = False) -> List[str]:
    """人名合规校验。strict=True 时只报高置信度违规（用于生成重试）。"""
    conflicts = []
    if not cast_names:
        return conflicts
    candidates = set()
    if strict:
        # 高置信度：只认带称谓后缀的 老X头/老X婶/老X叔 等（单独的"老X"普通词太多）
        for m in re.findall(r"老([一-龥]{1,2})(?:头|婶|叔|哥|姐|婆|爷)", content):
            candidates.add("老" + m)
    else:
        for m in re.findall(r"老([一-龥]{1,2})(?:头|婶|叔|哥|姐|婆|爷)?", content):
            candidates.add("老" + m)
    # 中置信度：X子（需过滤物件名词，strict 模式下不用于重试）
    NON_PERSON_ZI = {"日子", "儿子", "孩子", "屋子", "孙子", "鼻子", "脖子", "面子",
                     "里子", "个子", "样子", "辈子", "被子", "院子", "嫂子",
                     "胆子", "肚子", "脑子", "嗓子", "爪子", "麦子", "稻子", "虫子",
                     "绳子", "锤子", "锯子", "钉子", "梯子", "窗子", "盆子", "罐子",
                     "袋子", "袜子", "裤子", "袄子", "帽子", "鞋子", "靴子",
                     "把子", "管子", "褂子", "筷子", "秧子", "帘子", "匣子",
                     "绊子", "点子", "根子", "苗子", "影子", "印子", "口子"}
    zi_found = set()
    for m in re.findall(r"([一-龥]{1,2})子", content):
        if (m + "子") not in NON_PERSON_ZI:
            zi_found.add(m + "子")
    # 常见非人称"老X"词（身体部位/排行/普通词）
    NON_PERSON_LAO = {"老头", "老婆", "老天", "老实", "老虎", "老鼠", "老师",
                      "老板", "老家", "老人", "老伴", "老辈", "老幺",
                      "老骨", "老脸", "老眼", "老手", "老身", "老腿", "老腰",
                      "老嘴", "老耳", "老鼻", "老茧", "老汗",
                      "老大", "老二", "老三", "老四", "老五", "老六", "老七"}
    candidates = {c for c in candidates if c not in NON_PERSON_LAO
                  and c.rstrip("头婶叔哥姐婆爷") + "头" not in ("老头子",)}
    if not strict:
        candidates |= zi_found
    cast_set = set()
    for n in cast_names:
        cast_set.add(n)
        if len(n) >= 2:
            cast_set.add(n[-2:])
            cast_set.add("老" + n[0])
            cast_set.add(n[0])
    for c in candidates:
        base = c.rstrip("头婶叔哥姐婆爷")
        ok = any(c.startswith(x) or base in x or x in c for x in cast_set if len(x) >= 2)
        if not ok and len(base) >= 1:
            conflicts.append(f"疑似表外人物称呼: {c}")
    return conflicts


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
                 cast: str = "", cast_names: Optional[List[str]] = None,
                 prev_tail: str = "", next_brief: str = "",
                 appellations: str = "",
                 preset: Optional[List[Dict]] = None) -> Dict:
        """生成一章并做一致性校验。返回 {content, conflicts, retries, words}

        cast: 全书角色表原文（硬约束，只准用这些人）
        cast_names: 角色姓名列表（用于生成后校验）
        prev_tail: 上一章结尾（衔接上下文）
        next_brief: 下一章大纲（前瞻约束：本章埋的钩子必须与之兼容）
        appellations: 称谓约定（对话称呼必须遵守）
        """
        memory = self.store.snapshot()
        mem_block = (f"\n【已确立的故事事实（必须严格遵守）】\n{memory}\n" if memory else "")
        cast_block = (f"\n【全书角色表（铁律：只准使用表中人物，不得新造有名有姓的人物。"
                      f"人物姓名、关系、住处、道具必须与表内一致）】\n{cast}\n" if cast else "")
        app_block = (f"\n【称谓约定（人物对话中的互相称呼必须严格遵守，不得混用）】\n{appellations}\n"
                     if appellations else "")
        prev_block = (f"\n【上一章结尾（本章须与之衔接）】\n…{prev_tail}\n" if prev_tail else "")
        next_block = (f"\n【下一章大纲（前瞻约束）】\n{next_brief}\n"
                      f"本章如需埋钩子或让人物做出具体约定/计划，必须与下一章大纲兼容，"
                      f"不得自创与之冲突的行程或承诺。\n" if next_brief else "")
        prompt = (f"请创作小说第{chapter}章。\n\n【题材设定】\n{theme}{cast_block}{app_block}{mem_block}"
                  f"{prev_block}{next_block}\n【本章大纲】\n{brief}\n\n要求：只写正文，2500-3500字，遵循事实与角色表，"
                  f"与上一章结尾自然衔接。"
                  f"呼应要求：本章如揭示真相、推进矛盾或处理与前文同类的事件，必须明确呼应【已确立的故事事实】中的相关事件"
                  f"（提及、对比或串联幕后原因），不得让前文已抛出的悬念在本章凭空消失、各说各话。"
                  f"直接输出正文。")

        # 空章/过短/人物违规自动重试
        content, retries = "", 0
        detail_issues: List[str] = []
        for attempt in range(self.max_gen_retries + 1):
            retries = attempt
            content = self._gen_once(prompt)
            if len(content) < 1500:
                logger.warning("第%d章第%d次生成过短(%d字)，重试", chapter, attempt+1, len(content))
                continue
            # 人名合规校验：仅高置信度违规（老X头类）才带反馈重试
            if cast_names:
                bad = check_characters(content, cast_names, strict=True)
                if bad and attempt < self.max_gen_retries:
                    logger.warning("第%d章人物违规 %s，带反馈重试", chapter, bad[:3])
                    prompt += (f"\n\n【上次生成的错误】出现了角色表以外的人物："
                               f"{','.join(bad[:5])}。本次生成只准使用角色表中的人物。")
                    continue
            # 语义衔接校验：本章具体约定不得与下一章大纲冲突
            if next_brief and attempt < self.max_gen_retries:
                conflict = check_continuity(self.adapter, content, next_brief, self.model)
                if conflict:
                    logger.warning("第%d章衔接冲突 %s，带反馈重试", chapter, conflict)
                    prompt += (f"\n\n【上次生成的错误】正文中的人物约定与下一章大纲冲突：{conflict}。"
                               f"本次生成时，人物的具体计划必须与下一章大纲兼容。")
                    continue
            # 细节一致性校验：称谓/人物状态/道具连续性（第七道防线）
            if attempt < self.max_gen_retries:
                bad_details = check_details(self.adapter, content, cast, appellations, self.model)
                if bad_details:
                    detail_issues = bad_details
                    logger.warning("第%d章细节违规 %s，带反馈重试", chapter, bad_details[:3])
                    prompt += (f"\n\n【上次生成的细节错误】{'; '.join(bad_details[:4])}。"
                               f"本次生成必须修正这些细节错误。")
                    continue
                detail_issues = []
            break
        if len(content) < 800:
            raise RuntimeError(f"第{chapter}章多次生成仍过短（{len(content)}字）")

        # 双层一致性校验 + 人名终检（strict：只计高置信度，避免误报淹没真问题）
        conflicts = self.checker.check(content, self.store)
        if cast_names:
            conflicts += check_characters(content, cast_names, strict=True)
        conflicts += detail_issues  # 细节违规（重试后仍存在的）计入终检

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
