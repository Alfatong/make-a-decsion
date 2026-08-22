"""C 端用户接口（M2）：进度上报 / 双模式切换 / 搜索
内测期用户体系简化：设备标识即账号（device_id 换取 user）。
"""
from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from ..db import get_db
from ..core.response import ok, BizError, ERR_NOT_FOUND
from ..models import User, ReadProgress, Book, Chapter

router = APIRouter(prefix="/api/v1", tags=["c-user"])


def _get_or_create_user(db: Session, device_id: str) -> User:
    u = db.query(User).filter_by(device_id=device_id).first()
    if not u:
        u = User(device_id=device_id)
        db.add(u); db.commit(); db.refresh(u)
    return u


# ---------- 阅读进度 ----------
class ProgressIn(BaseModel):
    device_id: str
    book_id: int
    chapter_no: int
    position: int = 0


@router.post("/progress")
def report_progress(body: ProgressIn, db: Session = Depends(get_db)):
    u = _get_or_create_user(db, body.device_id)
    p = db.query(ReadProgress).filter_by(user_id=u.id, book_id=body.book_id).first()
    if not p:
        p = ReadProgress(user_id=u.id, book_id=body.book_id)
        db.add(p)
    p.chapter_no = body.chapter_no
    p.position = body.position
    db.commit()
    return ok({"book_id": body.book_id, "chapter_no": p.chapter_no})


@router.get("/progress")
def get_progress(device_id: str, book_id: int, db: Session = Depends(get_db)):
    u = db.query(User).filter_by(device_id=device_id).first()
    if not u:
        return ok({"chapter_no": 1, "position": 0})
    p = db.query(ReadProgress).filter_by(user_id=u.id, book_id=book_id).first()
    if not p:
        return ok({"chapter_no": 1, "position": 0})
    return ok({"chapter_no": p.chapter_no, "position": p.position})


# ---------- 双模式 ----------
class ModeIn(BaseModel):
    device_id: str
    mode: str   # standard|care


@router.post("/user/mode")
def set_mode(body: ModeIn, db: Session = Depends(get_db)):
    if body.mode not in ("standard", "care"):
        raise BizError(4000, "无效模式")
    u = _get_or_create_user(db, body.device_id)
    u.mode = body.mode
    db.commit()
    # 返回新页面配置：关怀模式更大字 + 广告位恒为 0
    cfg = {
        "standard": {"font_size": 22, "ad_slots": 2, "line_height": 2.0},
        "care": {"font_size": 28, "ad_slots": 0, "line_height": 2.2},
    }
    return ok({"mode": u.mode, "config": cfg[u.mode]})


@router.get("/user/mode")
def get_mode(device_id: str, db: Session = Depends(get_db)):
    u = db.query(User).filter_by(device_id=device_id).first()
    mode = u.mode if u else "standard"
    cfg = {
        "standard": {"font_size": 22, "ad_slots": 2, "line_height": 2.0},
        "care": {"font_size": 28, "ad_slots": 0, "line_height": 2.2},
    }
    return ok({"mode": mode, "config": cfg[mode]})


# ---------- 全局搜索 ----------
@router.get("/search")
def search(q: str, db: Session = Depends(get_db)):
    q = (q or "").strip()
    if not q:
        return ok({"books": [], "chapters": []})
    like = f"%{q}%"
    books = db.query(Book).filter(
        Book.status == "on_shelf", Book.market == "cn",
        (Book.title.like(like)) | (Book.intro.like(like))
    ).all()
    chapters = (db.query(Chapter).join(Book, Chapter.book_id == Book.id)
                .filter(Book.status == "on_shelf", Book.market == "cn",
                        Chapter.review_status == "manual_pass",
                        Chapter.title.like(like))
                .limit(20).all())
    return ok({
        "books": [{"id": b.id, "title": b.title, "intro": b.intro} for b in books],
        "chapters": [{"book_id": c.book_id, "no": c.no, "title": c.title} for c in chapters],
    })
