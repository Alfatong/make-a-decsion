"""海外书西语翻译 pass
逐章把 edited_content（无则 content）译成西班牙语，写入 ch.es_content。

人名地名一致性：
- 译名表（glossary）随翻译逐章积累——每章译文末尾要求模型输出本章新出现的
  专名译名映射（@@GLOSSARY@@ 行），解析后注入后续章节 prompt；
- glossary 持久化到临时目录 JSON 文件，中断重跑时已译章节跳过、译名表可复用。
"""
from __future__ import annotations
import json, logging, os, re, tempfile
from typing import Dict, Optional

from ...core.config import settings
from ...models import Book, Chapter
from ..llm.adapter import LLMAdapter

logger = logging.getLogger(__name__)

TRANSLATE_SYS = ("You are a professional English-to-Spanish literary translator of web novels. "
                 "Your Spanish reads natively for Latin American readers: vivid, idiomatic, "
                 "never stiff or machine-flavored.")
TRANSLATE_TMPL = """Translate Chapter {no} of the English web novel "{title}" into Spanish (Latin American neutral).

[Proper-noun glossary (mandatory: translate names exactly as listed)]
{glossary}

[Chapter {no} English text]
{content}

Requirements:
1. Translate the full chapter; do not summarize or omit paragraphs; keep paragraph breaks
2. Character/place/organization names must follow the glossary exactly; if a proper noun appears for the first time and is not in the glossary, coin a Spanish rendering and keep it consistent within the chapter
3. Keep dialogue punchy and emotional beats intact; honorifics/ranks may be localized naturally (e.g. "Alpha" stays "Alfa", "Luna" stays "Luna")
4. Output format (strict):
   - First the full Spanish translation
   - Then a final line starting with @@GLOSSARY@@ followed by a JSON object mapping ONLY the new proper nouns you coined this chapter, e.g. @@GLOSSARY@@ {{"Blackridge Pack": "Manada Blackridge"}}
   - If no new proper nouns, output @@GLOSSARY@@ {{}}"""


def _glossary_path(book_id: int) -> str:
    return os.path.join(tempfile.gettempdir(), f"book_{book_id}_es_glossary.json")


def _load_glossary(book_id: int) -> Dict[str, str]:
    try:
        with open(_glossary_path(book_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return {}


def _save_glossary(book_id: int, glossary: Dict[str, str]):
    try:
        with open(_glossary_path(book_id), "w", encoding="utf-8") as f:
            json.dump(glossary, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa
        logger.warning("译名表落盘失败: %s", e)


def _parse_output(text: str) -> tuple:
    """拆分译文与 @@GLOSSARY@@ 行。返回 (译文, 新译名dict)。"""
    m = re.search(r"^@@GLOSSARY@@\s*(\{.*\})\s*$", text, re.M)
    if not m:
        return text.strip(), {}
    translation = text[:m.start()].strip()
    try:
        new_names = json.loads(m.group(1))
        if not isinstance(new_names, dict):
            new_names = {}
    except Exception:  # noqa
        new_names = {}
    return translation, new_names


def translate_book_es(db, book_id: int, adapter: Optional[LLMAdapter] = None) -> Dict:
    """逐章西语翻译（同步执行，可放后台线程调用）。
    断点续跑：已有 es_content 的章节跳过。返回统计。"""
    book = db.get(Book, book_id)
    if not book:
        raise ValueError(f"书 {book_id} 不存在")
    adapter = adapter or LLMAdapter.from_env()
    chapters = [c for c in book.chapters if (c.edited_content or c.content)]
    chapters.sort(key=lambda c: c.no)
    glossary = _load_glossary(book_id)
    done, skipped, failed = 0, 0, []
    for ch in chapters:
        if ch.es_content and len(ch.es_content) > 200:
            skipped += 1
            continue
        src = ch.edited_content or ch.content
        glossary_text = ("\n".join(f"- {k} = {v}" for k, v in sorted(glossary.items()))
                         if glossary else "(empty — coin Spanish renderings as needed)")
        prompt = TRANSLATE_TMPL.format(title=book.title, no=ch.no,
                                       glossary=glossary_text, content=src)
        ok = False
        for model in (settings.LLM_MODEL_OUTLINE, settings.LLM_MODEL_CHAPTER):
            try:
                r = adapter.generate(prompt, model=model, system=TRANSLATE_SYS,
                                     max_tokens=6000, temperature=0.4,
                                     retry_waits=[15, 60])
                translation, new_names = _parse_output(r.text)
                if len(translation) < len(src) * 0.4:
                    logger.warning("第%d章译文异常短（%d/%d 字符），换模型重试",
                                   ch.no, len(translation), len(src))
                    continue
                ch.es_content = translation
                for k, v in new_names.items():
                    if k and v:
                        glossary.setdefault(str(k), str(v))
                db.commit()
                _save_glossary(book_id, glossary)
                done += 1
                ok = True
                logger.info("第%d章西语翻译完成（新译名 %d 个）", ch.no, len(new_names))
                break
            except Exception as e:  # noqa
                logger.warning("第%d章翻译 model=%s 失败: %s", ch.no, model, e)
        if not ok:
            db.rollback()
            failed.append(ch.no)
            logger.error("第%d章翻译双模型均失败", ch.no)
    logger.info("书%d西语翻译收尾：新译 %d 章，跳过 %d 章，失败 %s",
                book_id, done, skipped, failed)
    return {"book_id": book_id, "translated": done, "skipped": skipped,
            "failed": failed, "glossary_size": len(glossary)}
