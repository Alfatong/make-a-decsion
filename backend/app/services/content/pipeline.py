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
3. 再给"## 情节线"：把全书拆成 2-4 条情节线（如分房线/前程线/感情线），每条线单独一段，格式：
   - **线名**｜涉及章节（如 1-7,12,18）｜各章在线上的功能（第N章=抛出/铺垫/推进/低谷/交汇/回收，逐章列）｜与其他线的交汇点（第N章与X线交汇，写明怎么交汇）
   要求全书关键悬念都挂在某条线上，不允许有"孤章"
4. 再给"## 章节大纲"，逐章列出（格式：第N章 标题 - 情节 - 本章冲突点 - 章末钩子）
5. 节拍要求（这是重点）：
   - 每章必须有明确的冲突点或情感张力（误会、分歧、难处、反常迹象），不允许"纯过日子"的平章
   - 每章结尾必须有钩子：悬念（发现秘密的一半）、情感爆发前夜、两难抉择留白，三选一
   - 每3-5章安排一次小高潮（矛盾激化/真相揭露一角/关系破裂或和解）
   - 全书安排2-3次大高潮，高潮前3-5章埋伏笔线索
   - 情绪节奏遵循"压抑-释放"循环：憋屈的戏不能连压超过3章，之后必须给读者一口气顺出来的释放（和解、澄清、撑腰、团聚）
   - 悬念账本：每个关键悬念（名额、秘密、误会）在首次出现的章节标注【抛出】，在解决的章节标注【回收】；若主角接连遭遇多重打击，必须在后续章节标注"与第N章事件同一幕后/相互呼应"，不允许抛出的悬念无声消失
6. 标注关键状态变化点（角色生死/道具归属/住处变动）所在章节
7. 主要角色 6-10 个，姓名符合年代感和地域特色，全书不得超表新增有名人物
直接输出大纲文本。"""


OUTLINE_CHECK_SYS = "你是大纲校对编辑，专抓章间事实漂移，只报确凿的矛盾。"
OUTLINE_CHECK_TMPL = """校对这份长篇小说大纲的章间一致性。

【大纲全文】
{outline}

专查以下问题：
1. 事实漂移：同一事件/悬念/物件在不同章节的指称或结果不一致。例如：
   - 第4章说"分房名额被压下"，第5章却写成"夜大名额被挡"（同一悬念的对象漂移）
   - 第3章说把怀表给了大儿子，第8章又说怀表在二儿子手里（归属漂移）
   - 前面说某人不知情，后面却在描写他早就知道（信息状态漂移）
2. 重复混淆桥段：相邻或相近章节里，同一人物遭遇多个说法不同但性质雷同的挫折（如第4章分房名额被夺、第5章夜大名额被夺）——即使逻辑上是两件事，读者也会混淆，应报告建议合并为同一件事或明确错开
规则：
1. 只报确凿的矛盾或明显易混淆的桥段；题材、场景、对手完全不同的独立事件不报
2. 拿不准的不报
3. 输出 JSON（不要输出其他文字）：{{"issues":[{{"chapters":"涉及的章号","desc":"矛盾描述(40字内)","fix":"统一为哪种说法(30字内)"}}]}}
4. 没有矛盾输出空数组"""

OUTLINE_FIX_TMPL = """修订这份长篇小说大纲。校对发现以下章间事实漂移，逐条修正（统一各章指称，保持情节走向不变）：

【问题清单】
{issues}

【大纲全文（待修订）】
{outline}

要求：只改问题涉及的章节的表述，其余章节原样保留；保持大纲原有结构（角色表/称谓约定/章节大纲格式不变）。直接输出修订后的完整大纲。"""

EVENT_SYS = "你是剧情记录员，用一句话准确概括剧情事件，不含评论。"
EVENT_TMPL = """从下面这章正文中提取 3-5 条关键剧情事件，每条一句话（包含谁、做了什么、结果如何）。

要求：
1. 只记对后续剧情有影响的事件（名额/物品归属变动、秘密揭露、约定、关系变化、重要决定）
2. 事件中的物件、名额、结果要用正文里的原词，不得换说法（如正文是"分房名额"就写"分房名额"）
3. 每行一条，不加序号不评论

【第{no}章正文】
{content}"""

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


# ==================== 海外分支（market="overseas"）：英文 prompt 镜像 ====================
# 结构与国内九道防线一一对应：角色表 / 称谓约定 / 情节线分线表 / 悬念账本 / 章节大纲 / 逐章注入。
# 海外书默认 60 章、每章约 1500 英文词，题材公式见 OVERSEAS_GENRES。

OVERSEAS_GENRES = {
    "werewolf": """Genre formula: Werewolf ABO romance (rejected-mate paradigm).
- World: werewolf packs with Alpha/Beta/Omega dynamics, fated mate bonds, pack hierarchy, mate mark, heat/rut cycles, Luna ceremony, rogue threats.
- Core engine: the heroine is rejected or humiliated by her fated mate (the Alpha heir) in chapter 1-3; she leaves, grows powerful or hides a secret identity; he realizes his mistake and grovels; a rival she-wolf and pack politics supply conflict.
- Mandatory beats: rejection scene, the bond's pull neither can fully deny, a second-chance or new-mate temptation, a pack war or rogue attack forcing alliance, public vindication of the heroine, final Luna ascension or chosen freedom.
- Tone: visceral, possessive, high emotional stakes; sensory wolf imagery (scent, growl, mark).""",
    "ceo": """Genre formula: Billionaire CEO romance (霸总 paradigm).
- World: ruthless CEO male lead with a traumatic past; heroine with a hidden identity or talent; corporate warfare, family patriarchs, exes and scheming socialites.
- Core engine: a forced proximity deal (contract engagement, debt, marriage of convenience); he is cold and controlling, she refuses to bow; misunderstandings and jealousies escalate; his possessiveness flips into devotion; he grovels after hurting her.
- Mandatory beats: the deal signed on paper, public humiliation avenged, the heroine's secret revealed at the worst moment, a rival's scheme exposing the leads' feelings, grand gesture reconciliation.
- Tone: glossy, sharp dialogue, power-play tension, slow-burn desire.""",
    "contract_marriage": """Genre formula: Contract marriage romance (契约婚姻 paradigm).
- World: two leads bound by a written marriage contract with explicit terms (duration, no feelings clause, separate rooms, public appearances); families or business interests enforce the charade.
- Core engine: forced cohabitation breeds intimacy; small domestic moments erode the contract's clauses one by one; an ex or a family crisis tests the facade; the contract expiry date looms as the emotional deadline.
- Mandatory beats: signing scene with enumerated terms, accidental intimacy, jealousy neither may admit, a public event where they must perform as a couple, contract expiry crisis, choosing each other without the paper.
- Tone: warm banter, domestic detail, restrained longing.""",
}

OUTLINE_SYS_EN = ("You are a senior story editor for English web novels. You design "
                  "serialized page-turners with rigorous structure and output structured outlines.")
OUTLINE_TMPL_EN = """Based on the genre formula below, create the full-book outline for the English web novel "{title}" ({n} chapters total).

[Genre Formula]
{theme_prompt}

Requirements:
1. Start with "## Main Cast": one line per character, strictly in this format:
   - **Name** | Age | Role/Identity | Relationship to leads | Initial situation | Key item
2. Then "## Naming Conventions": state exactly how the main characters address each other (nicknames, titles, who calls the heroine what, how pack/company ranks are addressed). All dialogue in the whole book must obey these conventions.
3. Then "## Plot Lines": split the book into 2-4 plot lines (e.g. mate-bond line / revenge line / pack-politics line). One paragraph per line, format:
   - **Line name** | Chapters involved (e.g. 1-7,12,18) | Function of each chapter on the line (Chapter N = setup/build/escalation/low point/collision/payoff, listed chapter by chapter) | Intersections with other lines (at Chapter N it collides with line X, state how)
   Every key mystery of the book must hang on some line; no orphan chapters allowed.
4. Then "## Suspense Ledger": list every key mystery/secret/misunderstanding; mark the chapter where it is first planted with [SETUP] and the chapter where it is resolved with [PAYOFF]. If the heroine suffers stacked blows, later chapters must note "echoes Chapter N"; no planted mystery may silently vanish.
5. Then "## Chapter Outline": chapter by chapter, format: Chapter N. Title - Plot - Conflict of the chapter - Ending hook
6. Pacing requirements (this is the priority):
   - Every chapter must have a clear conflict or emotional tension; no flat filler chapters
   - Every chapter must end on a hook: a half-revealed secret, the eve of an emotional blowup, or an unresolved dilemma
   - A mini-climax every 3-5 chapters (escalation / partial truth reveal / rupture or reconciliation)
   - 2-3 grand climaxes across the book, with foreshadowing planted 3-5 chapters ahead
   - Tension-release cycle: never suppress the heroine for more than 3 consecutive chapters without a release (vindication, backup, reunion)
7. Mark the chapters where key state changes happen (deaths, item ownership, relationship status).
8. 6-10 main characters with names fitting the genre; the book must not introduce named characters beyond the cast sheet.
Output the outline text only."""

OUTLINE_CHECK_SYS_EN = ("You are an outline proofreader hunting cross-chapter factual drift. "
                        "Report only confirmed contradictions.")
OUTLINE_CHECK_TMPL_EN = """Proofread this web-novel outline for cross-chapter consistency.

[Full Outline]
{outline}

Look specifically for:
1. Factual drift: the same event/mystery/object is described inconsistently across chapters (e.g. Chapter 4 says the mate mark faded, Chapter 9 treats it as intact; a character is said not to know a secret, later chapters show he knew all along).
2. Confusing duplicate beats: adjacent chapters give the same character multiple setbacks of the same nature with different wording — readers will conflate them; report and suggest merging or clearly separating them.
Rules:
1. Report only confirmed contradictions or clearly confusable beats; independent events with different settings/opponents are fine.
2. When in doubt, do not report.
3. Output JSON only (no other text): {{"issues":[{{"chapters":"chapter numbers involved","desc":"contradiction (under 40 words)","fix":"which version to unify to (under 30 words)"}}]}}
4. If there are no issues, output an empty issues array."""

OUTLINE_FIX_TMPL_EN = """Revise this web-novel outline. The proofreader found the cross-chapter factual drifts below; fix them one by one (unify the wording across chapters, keep the plot direction unchanged):

[Issue List]
{issues}

[Full Outline (to revise)]
{outline}

Requirements: only change the chapters involved in the issues; keep all other chapters untouched; keep the outline's original structure (Main Cast / Naming Conventions / Plot Lines / Suspense Ledger / Chapter Outline). Output the complete revised outline directly."""

INTRO_SYS_EN = "You are a web-novel editor writing blurbs that hook readers in three lines."
INTRO_TMPL_EN = """Write a blurb (80-120 words) for the English web novel "{title}".

[Full Outline]
{outline}

Requirements: punchy, sensory, led by the heroine's wound and the central conflict; make readers need to click chapter 1. No ending spoilers. Do not start with "This book tells". Output the blurb text only."""

EVENT_SYS_EN = "You are a plot recorder. Summarize plot events in one accurate sentence each, no commentary."
EVENT_TMPL_EN = """Extract 3-5 key plot events from the chapter below, one sentence each (who did what, with what result).

Requirements:
1. Record only events that matter for later plot (secrets revealed, deals made, relationship shifts, ownership changes, major decisions)
2. Use the exact nouns from the text for objects, deals and results; never paraphrase them
3. One event per line, no numbering, no commentary

[Chapter {no} Text]
{content}"""

POLISH_SYS_EN = ("You are a veteran web-novel line editor. You kill AI-flavored prose "
                 "without touching the plot.")
POLISH_TMPL_EN = """Below is the draft of Chapter {no} of the English web novel "{title}". Polish and rewrite it.

[Style Sample (learn the feel, do not copy the content)]
{style_sample}

[Polish Requirements]
1. Plot, characters, dialogue direction and factual details stay exactly the same; polish only at the sentence level
2. Kill AI-flavored prose: cut filler hedges ("as if", "a wave of warmth", "couldn't help but"), stacked adjectives, and generic metaphors
3. Break long sentences; one sentence does one thing; dialogue must be short, sharp, and carry each character's temper
4. Add one concrete sensory beat every ~300 words — a gesture, a texture, a sound, a smell — chosen from objects already present in the chapter; invent no new plot
5. Vary paragraph density; heavy emotional moments get short paragraphs
6. The chapter must end on a hook: an unlanded thought, an off-note sound, an unfinished sentence
7. Keep the total length at 85%-115% of the draft
Output the polished full text directly, no title, no commentary.

[Draft]
{draft}"""

STYLE_SAMPLE_EN = """The rejection tasted like iron. Kaia stood in the center of the gathering hall, the bond-thread between her and Darius snapping taut, then slack, then gone — a phantom limb where her whole future had been.
"I, Darius Thorn, Alpha heir of the Blackridge Pack, reject you." His voice didn't shake. Hers did.
The pack watched. Nobody moved. Somewhere behind the Alpha's shoulder, Liora smiled into her wine.
Kaia lifted her chin. "Then keep your crown," she said. "I'll keep my name."
She walked out before her knees could buckle, and the night air outside bit her lungs like it was glad she was free."""

BRIEF_SYS_EN = "You are a web-novel editor. Summarize a chapter in one sentence."
BRIEF_TMPL_EN = """Summarize this chapter in one sentence (15-30 words), highlighting the concrete event so a new reader knows this chapter's hook. Output the sentence only, no "This chapter tells".

[Chapter Text]
{content}"""

CHAPTER_SYS_EN = ("You are a bestselling English web-novel author. Your prose is visceral and "
                  "addictive, and you follow the given story facts strictly.")
CHAPTER_TMPL_EN = """Write Chapter {chapter} of the web novel.

[Genre Setting]
{theme}{cast_block}{app_block}{line_block}{mem_block}{prev_block}{next_block}
[This Chapter's Outline]
{brief}

Requirements: write only the chapter body, around 1500 English words (1300-1800 words), follow the facts and the cast sheet strictly, connect naturally with the previous chapter's ending.
Echo requirement: if this chapter reveals a truth, escalates a conflict, or handles an event of the same kind as before, it must explicitly echo the related events in [Established Story Facts] (mention, contrast, or link the hidden cause). No previously planted mystery may vanish without a trace.
Output the chapter body directly."""



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


def _extract_storylines(outline: str) -> str:
    """从大纲提取情节线段落（生成时注入：让每章看见自己所在的线）。"""
    m = re.search(r"#{1,4}\s*(?:[一二三四五六\d]+[、.．]\s*)?情节线(.+?)(?:\n\s*---|\n#{1,4}\s|\Z)",
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


# ---------- 海外分支：英文大纲段落提取 ----------
def _extract_section_en(outline: str, header: str) -> str:
    """从英文大纲提取指定小节（标题兼容 # 层级与序号前缀）。"""
    m = re.search(r"#{1,4}\s*(?:\d+[、.．]\s*)?" + re.escape(header) +
                  r"\b(.+?)(?:\n\s*---|\n#{1,4}\s|\Z)",
                  outline, re.S | re.I)
    return m.group(1).strip() if m else ""


def _extract_cast_en(outline: str) -> str:
    return _extract_section_en(outline, "Main Cast") or _extract_section_en(outline, "Cast")


def _extract_appellations_en(outline: str) -> str:
    return _extract_section_en(outline, "Naming Conventions")


def _extract_storylines_en(outline: str) -> str:
    return _extract_section_en(outline, "Plot Lines")


def _extract_cast_names_en(cast: str) -> List[str]:
    """从英文角色表提取人名（- **Darius Thorn** | 24 | ... 格式）。"""
    names = []
    for m in re.finditer(r"\*\*([A-Z][A-Za-z'’.\- ]{1,40}?)\*\*\s*[|｜]", cast):
        name = m.group(1).strip()
        if name.lower() not in ("name",) and name not in names:
            names.append(name)
    return names


def _is_overseas(book) -> bool:
    return (getattr(book, "market", None) or "cn") == "overseas"


def _en_words(text: str) -> int:
    return len(text.split())


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

    def create_book(self, theme_id: int, title: str, chapters: Optional[int] = None,
                    market: str = "cn", language: str = "zh") -> Book:
        theme = self.db.get(Theme, theme_id)
        if not theme:
            raise ValueError(f"题材 {theme_id} 不存在")
        overseas = market == "overseas"
        n = chapters or theme.target_chapters  # 海外题材模板 target_chapters 默认 60
        # 生成全书大纲（pro 优先，pro 抖动不可用时降级 flash，保证建书不阻塞；
        # 完整性校验：残缺大纲直接重生，宁缺毋滥——595字残纲事故）
        def _outline_ok(t: str) -> bool:
            if overseas:
                # 英文大纲等价校验：必备小节 + 章节大纲段含完整 n 章
                low = t.lower()
                if not (len(t) >= 6000 and "plot lines" in low
                        and "naming conventions" in low and "cast" in low):
                    return False
                sec = re.search(r"chapter outline(.+)", t, re.S | re.I)
                if not sec:
                    return False
                found = set(int(m.group(1)) for m in
                            re.finditer(r"chapter\s+(\d+)\b", sec.group(1), re.I))
                return all(i in found for i in range(1, n + 1))
            if not (len(t) >= 2500 and "情节线" in t and "称谓约定" in t and "角色表" in t):
                return False
            # 章节大纲段必须有完整的 n 章（防"情节线含'第30章'字样蒙混、章节大纲只有6章"事故）
            sec = re.search(r"章节大纲(.+)", t, re.S)
            if not sec:
                return False
            found = set(int(m.group(1)) for m in re.finditer(r"第(\d+)章", sec.group(1)))
            return all(i in found for i in range(1, n + 1))
        outline_tmpl = OUTLINE_TMPL_EN if overseas else OUTLINE_TMPL
        outline_sys = OUTLINE_SYS_EN if overseas else OUTLINE_SYS
        prompt = outline_tmpl.format(title=title, n=n, theme_prompt=theme.prompt_template)
        r = None
        for attempt in range(3):
            for model in (settings.LLM_MODEL_OUTLINE, settings.LLM_MODEL_CHAPTER):
                try:
                    r = self.adapter.generate(prompt, model=model,
                                              system=outline_sys, max_tokens=8000, temperature=0.7,
                                              retry_waits=[15, 60])
                    if _outline_ok(r.text):
                        if model != settings.LLM_MODEL_OUTLINE:
                            logger.warning("大纲生成降级到 %s（pro 不可用）", model)
                        break
                    logger.warning("大纲不完整(%d字,第%d轮)，重试", len(r.text), attempt + 1)
                    r = None
                except Exception as e:  # noqa
                    logger.warning("大纲生成 %s 失败: %s", model, e)
            if r is not None:
                break
        if r is None:
            raise RuntimeError("大纲生成失败（多轮后仍不完整或双模型均不可用）")
        outline_text = r.text
        # 大纲自检：章间事实漂移（同一事件/悬念/物件指称不一）→ 自动修订
        try:
            outline_text = self._outline_self_check(outline_text, overseas=overseas)
        except Exception as e:  # noqa
            logger.warning("大纲自检跳过: %s", e)
        # 生成作品简介
        intro = ""
        try:
            intro_tmpl = INTRO_TMPL_EN if overseas else INTRO_TMPL
            intro_sys = INTRO_SYS_EN if overseas else INTRO_SYS
            ri = self.adapter.generate(
                intro_tmpl.format(title=title, outline=outline_text[:2000]),
                model=settings.LLM_MODEL_CHAPTER, system=intro_sys,
                max_tokens=300, temperature=0.7)
            intro = ri.text.strip()
        except Exception as e:  # noqa
            logger.warning("简介生成失败: %s", e)
        book = Book(theme_id=theme_id, title=title, intro=intro, outline=outline_text,
                    status="draft", total_chapters=n, ai_label=True,
                    market=market, language=("en" if overseas and language == "zh" else language))
        self.db.add(book); self.db.commit(); self.db.refresh(book)
        logger.info("创建书籍 id=%s market=%s 大纲 %d 字", book.id, market, len(outline_text))
        return book

    def _outline_self_check(self, outline: str, overseas: bool = False) -> str:
        """大纲章间一致性自检 + 自动修订（pro 优先，失败降级 flash，再失败用原稿）。"""
        check_tmpl = OUTLINE_CHECK_TMPL_EN if overseas else OUTLINE_CHECK_TMPL
        check_sys = OUTLINE_CHECK_SYS_EN if overseas else OUTLINE_CHECK_SYS
        fix_tmpl = OUTLINE_FIX_TMPL_EN if overseas else OUTLINE_FIX_TMPL
        fix_sys = OUTLINE_SYS_EN if overseas else OUTLINE_SYS
        check_prompt = check_tmpl.format(outline=outline[:14000])
        issues = []
        for model in (settings.LLM_MODEL_OUTLINE, settings.LLM_MODEL_CHAPTER):
            try:
                rc = self.adapter.generate(check_prompt, model=model,
                                           system=check_sys,
                                           max_tokens=1500, temperature=0.1,
                                           retry_waits=[10, 40])
                m = re.search(r"\{.*\}", rc.text, re.S)
                if m:
                    issues = json.loads(m.group(0)).get("issues", [])
                break
            except Exception as e:  # noqa
                logger.warning("大纲自检 %s 失败: %s", model, e)
        if not issues:
            logger.info("大纲自检通过，无事实漂移")
            return outline
        logger.warning("大纲自检发现 %d 处漂移: %s", len(issues),
                       [i.get("desc", "")[:30] for i in issues[:3]])
        issue_text = "\n".join(f"- 章节{i.get('chapters','')}: {i.get('desc','')}（统一为：{i.get('fix','')}）"
                               for i in issues)
        fix_prompt = fix_tmpl.format(issues=issue_text, outline=outline)
        for model in (settings.LLM_MODEL_OUTLINE, settings.LLM_MODEL_CHAPTER):
            try:
                rf = self.adapter.generate(fix_prompt, model=model,
                                           system=fix_sys,
                                           max_tokens=8000, temperature=0.3,
                                           retry_waits=[10, 40])
                if len(rf.text) > len(outline) * 0.5:
                    logger.info("大纲已自动修订 %d 处漂移", len(issues))
                    return rf.text.strip()
            except Exception as e:  # noqa
                logger.warning("大纲修订 %s 失败: %s", model, e)
        logger.error("大纲修订失败，使用原稿（漂移未修）")
        return outline

    def _gen_chapter_core(self, book_id: int, no: int) -> Chapter:
        """章节核心生成（串行环节）：生成 + 人名/衔接校验 + 入库。
        不润色、不提要、不机审——这些走 _post_chapter 异步。"""
        book = self.db.get(Book, book_id)
        if not book:
            raise ValueError(f"书 {book_id} 不存在")
        if _is_overseas(book):
            return self._gen_chapter_core_en(book, no)
        store = self._fact_store(book_id)
        gen = ChapterGenerator(self.adapter, store, self.checker,
                               model=settings.LLM_MODEL_CHAPTER)
        brief = self._chapter_brief(book.outline, no)
        theme_prompt = book.theme.prompt_template if book.theme else ""
        # 一致性硬约束：角色表 + 上一章结尾 + 下一章前瞻
        cast = _extract_cast(book.outline)
        cast_names = _extract_cast_names(cast)
        appellations = _extract_appellations(book.outline)
        storylines = _extract_storylines(book.outline)
        prev = self.db.query(Chapter).filter_by(book_id=book_id, no=no - 1).first()
        prev_tail = prev.content[-800:] if prev and prev.content else ""
        next_brief = self._chapter_brief(book.outline, no + 1) if no < book.total_chapters else ""
        result = gen.generate(no, theme_prompt, brief,
                              cast=cast, cast_names=cast_names,
                              prev_tail=prev_tail, next_brief=next_brief,
                              appellations=appellations, storylines=storylines,
                              preset=None)
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
        # 章节事件入库：后续章节生成时能看到"已发生的剧情"（防跨章事件漂移）
        try:
            for ev in self._extract_events(no, content):
                gen.store.add("event", "剧情", f"第{no}章事件", ev,
                              chapter=no, source="outline")
        except Exception as e:  # noqa
            logger.warning("第%d章事件提取失败: %s", no, e)
        return ch

    def _gen_chapter_core_en(self, book: Book, no: int) -> Chapter:
        """海外书章节核心生成：英文 prompt + 角色表/情节线/事实库逐章注入 + 词数重试。
        不润色、不提要——这些走 _post_chapter。中文细节校验器不适用于英文文本，这里跳过。"""
        book_id = book.id
        store = self._fact_store(book_id)
        brief = self._chapter_brief_en(book.outline, no)
        theme_prompt = book.theme.prompt_template if book.theme else ""
        cast = _extract_cast_en(book.outline)
        appellations = _extract_appellations_en(book.outline)
        storylines = _extract_storylines_en(book.outline)
        prev = self.db.query(Chapter).filter_by(book_id=book_id, no=no - 1).first()
        prev_tail = prev.content[-800:] if prev and prev.content else ""
        next_brief = (self._chapter_brief_en(book.outline, no + 1)
                      if no < book.total_chapters else "")
        memory = store.snapshot()
        cast_block = (f"\n[Main Cast (iron rule: only these characters; no new named characters. "
                      f"Names, relationships and items must match the sheet)]\n{cast}\n" if cast else "")
        app_block = (f"\n[Naming Conventions (dialogue address must obey)]\n{appellations}\n"
                     if appellations else "")
        line_block = (f"\n[Plot Lines (this chapter is a node on a line: carry forward the line's "
                      f"prior events and emotions; at collision chapters write the lines echoing)]\n"
                      f"{storylines}\n" if storylines else "")
        mem_block = (f"\n[Established Story Facts (must obey)]\n{memory}\n" if memory else "")
        prev_block = (f"\n[Previous Chapter Ending (this chapter must connect)]\n…{prev_tail}\n"
                      if prev_tail else "")
        next_block = (f"\n[Next Chapter Outline (forward constraint)]\n{next_brief}\n"
                      f"Any hook planted or concrete promise made in this chapter must be compatible "
                      f"with the next chapter's outline.\n" if next_brief else "")
        prompt = CHAPTER_TMPL_EN.format(
            chapter=no, theme=theme_prompt, cast_block=cast_block, app_block=app_block,
            line_block=line_block, mem_block=mem_block, prev_block=prev_block,
            next_block=next_block, brief=brief)
        # 词数重试：目标约 1500 词，低于 900 词视为过短重生，低于 500 词多轮后报错
        content = ""
        for attempt in range(3):
            r = self.adapter.generate(prompt, model=settings.LLM_MODEL_CHAPTER,
                                      system=CHAPTER_SYS_EN,
                                      max_tokens=4000, temperature=0.75)
            content = r.text.strip()
            if _en_words(content) >= 900:
                break
            logger.warning("第%d章第%d次生成过短(%d词)，重试", no, attempt + 1, _en_words(content))
        if _en_words(content) < 500:
            raise RuntimeError(f"第{no}章多次生成仍过短（{_en_words(content)}词）")
        ch = self.db.query(Chapter).filter_by(book_id=book_id, no=no).first()
        if not ch:
            ch = Chapter(book_id=book_id, no=no)
            self.db.add(ch)
        ch.content = content
        ch.word_count = _en_words(content)
        ch.consistency_conflicts = []
        ch.review_status = "pending"
        self.db.commit(); self.db.refresh(ch)
        # 章节事件入库：后续章节生成时能看到"已发生的剧情"
        try:
            for ev in self._extract_events_en(no, content):
                store.add("event", "plot", f"Chapter {no} event", ev,
                          chapter=no, source="outline")
        except Exception as e:  # noqa
            logger.warning("第%d章英文事件提取失败: %s", no, e)
        return ch

    @staticmethod
    def _chapter_brief_en(outline: str, no: int) -> str:
        """从英文大纲取第 no 章的一行提要（防 Chapter 1 误配 Chapter 10）。"""
        prefix = f"chapter {no}"
        for line in outline.splitlines():
            s = line.strip().lstrip("*- ").strip()
            if s.lower().startswith(prefix):
                rest = s[len(prefix):]
                if not rest or not rest[0].isdigit():
                    return s.rstrip("*").strip()
            if s.startswith(f"{no}."):
                return s.rstrip("*").strip()
        return f"Chapter {no}"

    def _extract_events_en(self, no: int, content: str) -> List[str]:
        """英文章节关键剧情事件提取（失败不阻塞）。"""
        r = self.adapter.generate(EVENT_TMPL_EN.format(no=no, content=content[:6000]),
                                  model=settings.LLM_MODEL_CHAPTER, system=EVENT_SYS_EN,
                                  max_tokens=400, temperature=0.1)
        return [ln.strip() for ln in r.text.splitlines()
                if ln.strip() and len(ln.strip()) > 12][:5]

    def _extract_events(self, no: int, content: str) -> List[str]:
        """提取本章关键剧情事件（flash，失败返回空不阻塞）。"""
        r = self.adapter.generate(EVENT_TMPL.format(no=no, content=content[:6000]),
                                  model=settings.LLM_MODEL_CHAPTER, system=EVENT_SYS,
                                  max_tokens=400, temperature=0.1)
        return [ln.strip() for ln in r.text.splitlines()
                if ln.strip() and len(ln.strip()) > 8][:5]

    def _post_chapter(self, chapter_id: int, title: str, no: int,
                      polish: bool = True):
        """章节后处理（可异步并行）：润色 → 提要 → 机审。失败均降级不阻塞。"""
        ch = self.db.get(Chapter, chapter_id)
        if not ch or not ch.content:
            return
        overseas = _is_overseas(ch.book) if ch.book else False
        # 润色 pass（pro 抖动时登记，等补跑）
        if polish:
            try:
                if overseas:
                    polished = self.polish_text_en(title, no, ch.content)
                    ch.content = polished
                    ch.word_count = _en_words(polished)
                else:
                    polished = self.polish_text(title, no, ch.content)
                    ch.content = polished
                    ch.word_count = len(re.sub(r"\s", "", polished))
                self.db.commit()
            except Exception as e:  # noqa
                logger.warning("第%d章润色失败，用初稿: %s", no, e)
        # 章节一句话提要
        try:
            ch.brief = (self.gen_brief_en(ch.content) if overseas
                        else self.gen_brief(ch.content))
            self.db.commit()
        except Exception as e:  # noqa
            logger.warning("第%d章提要生成失败: %s", no, e)
        # 机审（海外书面向海外市场，不走国内 TMS 内容安全审核，直接标记通过）
        if overseas:
            ch.review_label = "overseas-skip"
            ch.review_status = "machine_pass"
            self.db.commit()
        else:
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

    def polish_text_en(self, title: str, no: int, draft: str) -> str:
        """英文润色（海外书，质感优先用 Pro）。"""
        r = self.adapter.generate(
            POLISH_TMPL_EN.format(title=title, no=no,
                                  style_sample=STYLE_SAMPLE_EN, draft=draft),
            model=settings.LLM_MODEL_OUTLINE,
            system=POLISH_SYS_EN, max_tokens=6000, temperature=0.6)
        text = r.text.strip()
        if _en_words(text) < _en_words(draft) * 0.5:
            raise RuntimeError("润色结果异常短，丢弃")
        return text

    def gen_brief_en(self, content: str) -> str:
        """英文章节一句话提要（15-30 词）。"""
        r = self.adapter.generate(BRIEF_TMPL_EN.format(content=content[:2500]),
                                  model=settings.LLM_MODEL_CHAPTER,
                                  system=BRIEF_SYS_EN, max_tokens=120, temperature=0.5)
        return r.text.strip().strip('"').strip()[:200]

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
        overseas = _is_overseas(book)
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
        if overseas:
            # 海外书：中文一致性审计/中文 TTS/互动节点不适用，编辑报告直接入库收尾
            if editor_report:
                book = self.db.get(Book, book_id)
                book.audit_report = {"overseas": True, "editor": editor_report}
                self.db.commit()
            logger.info("书%d海外流水线收尾：跳过中文审计与 TTS/互动", book_id)
        else:
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

        # 低覆盖只报告不拦截（配角戏份少属正常创作选择，编辑知情即可）；
        # 硬性拦截：违规（表外人物/称谓矛盾）+ 过短章
        passed = (not violations) and (not short_chapters)
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
