"""C 端阅读接口（M2）
首页聚合 / 作品介绍 / 章节正文+tts轴。仅返回已上架且过审内容。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..core.response import ok, BizError, ERR_NOT_FOUND
from ..models import Book, Chapter

router = APIRouter(prefix="/api/v1", tags=["c-read"])


def _book_card(b: Book):
    return {"id": b.id, "title": b.title, "intro": b.intro,
            "total_chapters": b.total_chapters, "ai_label": b.ai_label}


@router.get("/home")
def home(db: Session = Depends(get_db)):
    """首页聚合：已上架书卡流。"""
    rows = db.query(Book).filter(Book.status == "on_shelf").all()
    return ok({"books": [_book_card(b) for b in rows]})


@router.get("/books/{bid}")
def book_intro(bid: int, db: Session = Depends(get_db)):
    b = db.get(Book, bid)
    if not b or b.status != "on_shelf":
        raise BizError(ERR_NOT_FOUND, "作品不存在或未上架")
    toc = [{"no": c.no, "title": c.title or f"第{c.no}章", "word_count": c.word_count}
           for c in b.chapters if c.review_status == "manual_pass"]
    return ok({**_book_card(b), "outline": b.outline[:500], "toc": toc})


@router.get("/books/{bid}/chapters/{no}")
def chapter_read(bid: int, no: int, db: Session = Depends(get_db)):
    """章节正文 + tts 段时间轴 + ai_label。"""
    b = db.get(Book, bid)
    if not b or b.status != "on_shelf":
        raise BizError(ERR_NOT_FOUND, "作品不存在或未上架")
    ch = next((c for c in b.chapters if c.no == no), None)
    if not ch or ch.review_status != "manual_pass":
        raise BizError(ERR_NOT_FOUND, "章节不存在或未过审")
    return ok({
        "book_id": bid, "chapter_id": ch.id, "no": no, "title": ch.title or f"第{no}章",
        "content": ch.content, "word_count": ch.word_count,
        "ai_label": b.ai_label,
        "tts_segments": ch.tts_segments or [],
        "audio_url": f"/audio/ch{ch.id}.mp3" if ch.tts_segments else None,
        "has_prev": no > 1,
        "has_next": no < b.total_chapters,
    })
