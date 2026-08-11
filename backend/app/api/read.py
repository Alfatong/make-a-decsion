"""C 端阅读接口（M2）
首页聚合 / 作品介绍 / 章节正文+tts轴。仅返回已上架且过审内容。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..core.response import ok, BizError, ERR_NOT_FOUND
from ..models import Book, Chapter, Theme, User, ReadProgress

router = APIRouter(prefix="/api/v1", tags=["c-read"])


def _book_card(b: Book):
    return {"id": b.id, "title": b.title, "intro": b.intro,
            "total_chapters": b.total_chapters, "ai_label": b.ai_label,
            "theme": b.theme.name if b.theme else ""}


@router.get("/home")
def home(device_id: str = "", db: Session = Depends(get_db)):
    """首页聚合：精选轮播 + 频道 + 书卡流 + 续读。"""
    rows = db.query(Book).filter(Book.status == "on_shelf").all()
    # 精选轮播：取最新上架的
    featured = [_book_card(b) for b in rows[:3]]
    # 频道：按题材分组
    channels = {}
    for b in rows:
        t = b.theme.name if b.theme else "其他"
        channels.setdefault(t, []).append(_book_card(b))
    channel_list = [{"name": k, "books": v} for k, v in channels.items()]
    # 续读
    resume = None
    if device_id:
        u = db.query(User).filter_by(device_id=device_id).first()
        if u:
            p = (db.query(ReadProgress).filter_by(user_id=u.id)
                 .order_by(ReadProgress.updated_at.desc()).first())
            if p:
                b = db.get(Book, p.book_id)
                if b and b.status == "on_shelf":
                    resume = {"book_id": b.id, "title": b.title,
                              "chapter_no": p.chapter_no}
    return ok({"featured": featured, "channels": channel_list,
               "books": [_book_card(b) for b in rows], "resume": resume})


@router.get("/books/{bid}")
def book_intro(bid: int, db: Session = Depends(get_db)):
    b = db.get(Book, bid)
    if not b or b.status != "on_shelf":
        raise BizError(ERR_NOT_FOUND, "作品不存在或未上架")
    toc = [{"no": c.no, "title": c.title or f"第{c.no}章", "word_count": c.word_count,
            "brief": c.brief or ""}
           for c in b.chapters if c.review_status == "manual_pass"]
    return ok({**_book_card(b), "outline": b.outline[:500], "toc": toc})


@router.get("/books/{bid}/chapters/{no}")
def chapter_read(bid: int, no: int, device_id: str = "", db: Session = Depends(get_db)):
    """章节正文 + tts 段时间轴 + ai_label + unlock 状态。"""
    b = db.get(Book, bid)
    if not b or b.status != "on_shelf":
        raise BizError(ERR_NOT_FOUND, "作品不存在或未上架")
    ch = next((c for c in b.chapters if c.no == no), None)
    if not ch or ch.review_status != "manual_pass":
        raise BizError(ERR_NOT_FOUND, "章节不存在或未过审")
    # 锁章校验：免费章直接可读，付费章查权益
    locked = False
    if no > b.free_chapters:
        unlocked = False
        if device_id:
            from ..models import Entitlement
            u = db.query(User).filter_by(device_id=device_id).first()
            if u:
                for ent in db.query(Entitlement).filter_by(
                        user_id=u.id, book_id=bid, status="active").all():
                    if ent.scope == "full" or (ent.scope == "chapter" and ent.chapter_no == no):
                        unlocked = True; break
        locked = not unlocked
    if locked:
        return ok({
            "book_id": bid, "chapter_id": ch.id, "no": no,
            "title": ch.title or f"第{no}章", "locked": True,
            "free_chapters": b.free_chapters,
            "price_cents": b.price_cents, "chapter_price_cents": b.chapter_price_cents,
            "has_prev": no > 1, "has_next": no < b.total_chapters,
        })
    return ok({
        "book_id": bid, "chapter_id": ch.id, "no": no, "title": ch.title or f"第{no}章",
        "content": ch.content, "word_count": ch.word_count,
        "ai_label": b.ai_label, "locked": False,
        "tts_segments": ch.tts_segments or [],
        "audio_url": f"/audio/ch{ch.id}.mp3" if ch.tts_segments else None,
        "has_prev": no > 1,
        "has_next": no < b.total_chapters,
    })
