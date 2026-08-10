"""后台内容管线接口（M1）
题材 CRUD / 生成任务 / 审核队列 / 上架
写接口带 idempotency_key / dedup_key 防重。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import os

from ..db import get_db, Base, engine
from ..core.response import ok, err, BizError, ERR_NOT_FOUND, ERR_REVIEW
from ..models import Theme, Book, Chapter, GenTask, ReviewRecord
from ..services.content.pipeline import ContentPipeline
from ..services.moderation.review import ReviewService

router = APIRouter(prefix="/admin/api", tags=["admin-content"])


# ---------- 题材模板 ----------
class ThemeIn(BaseModel):
    name: str
    prompt_template: str
    weight: float = 1.0
    target_chapters: int = 30
    enabled: bool = True


@router.post("/themes")
def create_theme(body: ThemeIn, db: Session = Depends(get_db)):
    t = Theme(**body.dict())
    db.add(t); db.commit(); db.refresh(t)
    return ok({"id": t.id})


@router.get("/themes")
def list_themes(db: Session = Depends(get_db)):
    rows = db.query(Theme).all()
    return ok([{"id": t.id, "name": t.name, "weight": t.weight,
                "target_chapters": t.target_chapters, "enabled": t.enabled} for t in rows])


@router.put("/themes/{tid}")
def update_theme(tid: int, body: ThemeIn, db: Session = Depends(get_db)):
    t = db.get(Theme, tid)
    if not t:
        raise BizError(ERR_NOT_FOUND, "题材不存在")
    for k, v in body.dict().items():
        setattr(t, k, v)
    db.commit()
    return ok({"id": tid})


# ---------- 生成任务 ----------
class NewBookIn(BaseModel):
    theme_id: int
    title: str
    chapters: Optional[int] = None
    dedup_key: str
    auto_generate: bool = False   # 是否立即逐章生成
    max_chapters: Optional[int] = None


@router.post("/books")
def create_book(body: NewBookIn, db: Session = Depends(get_db)):
    # dedup 防重
    if db.query(GenTask).filter_by(dedup_key=body.dedup_key).first():
        raise BizError(4301, "重复任务（dedup_key 已存在）")
    task = GenTask(task_type="new_book", dedup_key=body.dedup_key,
                   status="running", payload=body.dict())
    db.add(task); db.commit(); db.refresh(task)
    try:
        pipe = ContentPipeline(db)
        book = pipe.create_book(body.theme_id, body.title, body.chapters)
        result = {"book_id": book.id, "outline_len": len(book.outline)}
        if body.auto_generate:
            gen = pipe.generate_book(book.id, body.max_chapters)
            result.update(gen)
        task.status = "done"; task.result = result; task.finished_at = datetime.utcnow()
        db.commit()
        return ok(result)
    except Exception as e:  # noqa
        task.status = "failed"; task.error = str(e); task.finished_at = datetime.utcnow()
        db.commit()
        raise BizError(5000, f"生成失败: {e}")


@router.get("/books")
def list_books(db: Session = Depends(get_db)):
    rows = db.query(Book).all()
    return ok([{"id": b.id, "title": b.title, "status": b.status,
                "total_chapters": b.total_chapters,
                "chapters": len(b.chapters)} for b in rows])


@router.get("/books/{bid}")
def book_detail(bid: int, db: Session = Depends(get_db)):
    b = db.get(Book, bid)
    if not b:
        raise BizError(ERR_NOT_FOUND, "书不存在")
    return ok({"id": b.id, "title": b.title, "intro": b.intro, "outline": b.outline,
               "status": b.status, "total_chapters": b.total_chapters,
               "chapters": [{"no": c.no, "title": c.title, "word_count": c.word_count,
                             "review_status": c.review_status,
                             "conflicts": c.consistency_conflicts} for c in b.chapters]})


# ---------- 审核 ----------
class GenChapterIn(BaseModel):
    book_id: int
    no: int


@router.post("/gen-chapter")
def gen_chapter(body: GenChapterIn, db: Session = Depends(get_db)):
    """单章生成（走记忆层一致性校验）。"""
    pipe = ContentPipeline(db)
    try:
        ch = pipe.generate_chapter(body.book_id, body.no)
    except ValueError as e:
        raise BizError(ERR_NOT_FOUND, str(e))
    except Exception as e:  # noqa
        raise BizError(5000, f"章节生成失败: {e}")
    return ok({"chapter_id": ch.id, "no": ch.no, "word_count": ch.word_count,
               "conflicts": ch.consistency_conflicts})


AUDIO_DIR = os.environ.get("AUDIO_DIR", "/opt/novel-app/deploy/web/audio")


def _save_audio_local(chapter_id: int):
    os.makedirs(AUDIO_DIR, exist_ok=True)
    path = os.path.join(AUDIO_DIR, f"ch{chapter_id}.mp3")
    def _upload(data: bytes, ext: str) -> str:
        with open(path, "wb") as f:
            f.write(data)
        return f"/audio/ch{chapter_id}.{ext}"
    return _upload


@router.post("/chapters/{cid}/tts")
def chapter_tts(cid: int, voice: int = 101002, db: Session = Depends(get_db)):
    """章节 TTS 合成 + 时间轴入库 + 音频存静态目录（故障降级不阻塞阅读）。"""
    pipe = ContentPipeline(db)
    try:
        r = pipe.synthesize_chapter_audio(cid, voice=voice,
                                          upload=_save_audio_local(cid))
    except ValueError as e:
        raise BizError(ERR_NOT_FOUND, str(e))
    return ok(r)


@router.post("/chapters/{cid}/machine-review")
def machine_review(cid: int, db: Session = Depends(get_db)):
    ch = db.get(Chapter, cid)
    if not ch:
        raise BizError(ERR_NOT_FOUND, "章节不存在")
    try:
        rs = ReviewService()
        r = rs.is_safe(ch.content, data_id=f"ch{cid}")
    except Exception as e:  # noqa
        # 接口异常 → 进延迟队列，不得直接上架
        ch.review_status = "pending"
        db.commit()
        raise BizError(5001, f"机审接口异常，已入延迟队列: {e}")
    ch.review_label = r["label"]
    ch.review_status = "machine_pass" if r["pass"] else "machine_hit"
    db.add(ReviewRecord(chapter_id=cid, stage="machine",
                        action="pass" if r["pass"] else "hit",
                        label=r["label"], detail=str(r)))
    db.commit()
    return ok(r)


@router.get("/review-queue")
def review_queue(db: Session = Depends(get_db)):
    rows = db.query(Chapter).filter(Chapter.review_status.in_(
        ["machine_hit", "pending"])).all()
    return ok([{"chapter_id": c.id, "book_id": c.book_id, "no": c.no,
                "review_status": c.review_status, "label": c.review_label,
                "conflicts": c.consistency_conflicts} for c in rows])


class ManualReviewIn(BaseModel):
    action: str   # approve | reject


@router.post("/chapters/{cid}/review")
def manual_review(cid: int, body: ManualReviewIn, db: Session = Depends(get_db)):
    ch = db.get(Chapter, cid)
    if not ch:
        raise BizError(ERR_NOT_FOUND, "章节不存在")
    if body.action == "approve":
        ch.review_status = "manual_pass"
    else:
        ch.review_status = "rejected"
    db.add(ReviewRecord(chapter_id=cid, stage="manual", action=body.action))
    db.commit()
    return ok({"chapter_id": cid, "status": ch.review_status})


@router.post("/books/{bid}/shelf")
def shelf_book(bid: int, db: Session = Depends(get_db)):
    """整书上架：要求章节数齐整 + 所有章节过了机审且人工通过。"""
    b = db.get(Book, bid)
    if not b:
        raise BizError(ERR_NOT_FOUND, "书不存在")
    if len(b.chapters) < b.total_chapters:
        raise BizError(ERR_REVIEW,
                       f"章节未生成齐整：{len(b.chapters)}/{b.total_chapters}")
    not_passed = [c.no for c in b.chapters if c.review_status != "manual_pass"]
    if not_passed:
        raise BizError(ERR_REVIEW, f"以下章节未通过审核: {not_passed}")
    b.status = "on_shelf"; db.commit()
    return ok({"book_id": bid, "status": "on_shelf"})
