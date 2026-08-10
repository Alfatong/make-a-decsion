"""C 端互动接口（M2）"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel

from ..db import get_db
from ..core.response import ok, BizError, ERR_NOT_FOUND
from ..models import Chapter
from ..services.interact.service import InteractService

router = APIRouter(prefix="/api/v1", tags=["c-interact"])


class ChooseIn(BaseModel):
    chapter_id: int
    option_idx: int
    is_rerun: bool = False


@router.post("/interact/choose")
def interact_choose(body: ChooseIn, db: Session = Depends(get_db)):
    ch = db.get(Chapter, body.chapter_id)
    if not ch or ch.review_status != "manual_pass":
        raise BizError(ERR_NOT_FOUND, "章节不存在或未过审")
    svc = InteractService(db)
    r = svc.choose(body.chapter_id, body.option_idx)
    return ok({"echo": r["echo"], "cached": r["cached"], "is_rerun": body.is_rerun})


@router.get("/interact/node/{chapter_id}")
def interact_node(chapter_id: int, db: Session = Depends(get_db)):
    """取章内互动节点（提问 + 选项），供前端渲染。"""
    ch = db.get(Chapter, chapter_id)
    if not ch or ch.review_status != "manual_pass":
        raise BizError(ERR_NOT_FOUND, "章节不存在或未过审")
    svc = InteractService(db)
    node = svc.get_or_create_node(chapter_id)
    return ok({"node_id": node.id, "question": node.question, "options": node.options})
