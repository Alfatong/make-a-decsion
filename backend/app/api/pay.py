"""C 端付费接口（模拟）。订单/权益分离 + 幂等 + 限额预占。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from ..db import get_db
from ..core.response import ok, BizError
from ..models import User, Book, Order
from ..services.payment.service import PaymentService, PaymentError

router = APIRouter(prefix="/api/v1", tags=["c-pay"])


def _user(db: Session, device_id: str) -> User:
    u = db.query(User).filter_by(device_id=device_id).first()
    if not u:
        u = User(device_id=device_id); db.add(u); db.commit(); db.refresh(u)
    return u


class OrderIn(BaseModel):
    device_id: str
    book_id: int
    order_type: str            # buyout|chapter
    chapter_no: Optional[int] = None
    idempotency_key: str


@router.post("/orders")
def create_order(body: OrderIn, db: Session = Depends(get_db)):
    u = _user(db, body.device_id)
    svc = PaymentService(db)
    try:
        order = svc.create_order(u, body.book_id, body.order_type,
                                 body.idempotency_key, body.chapter_no)
    except PaymentError as e:
        raise BizError(e.code, e.msg)
    return ok({"order_id": order.id, "order_no": order.order_no,
               "amount_cents": order.amount_cents, "status": order.status,
               "order_type": order.order_type, "chapter_no": order.chapter_no})


@router.post("/orders/{order_id}/mock-pay")
def mock_pay(order_id: int, db: Session = Depends(get_db)):
    """模拟收银台回调：置 paid + 幂等发放权益。"""
    svc = PaymentService(db)
    try:
        order = svc.mock_pay(order_id)
    except PaymentError as e:
        raise BizError(e.code, e.msg)
    return ok({"order_id": order.id, "status": order.status})


@router.post("/orders/{order_id}/refund")
def refund(order_id: int, db: Session = Depends(get_db)):
    svc = PaymentService(db)
    try:
        order = svc.refund(order_id)
    except PaymentError as e:
        raise BizError(e.code, e.msg)
    return ok({"order_id": order.id, "status": order.status})


@router.get("/entitlements")
def my_entitlements(device_id: str, book_id: int, db: Session = Depends(get_db)):
    """查用户对某书的权益（前端判断是否解锁）。"""
    u = db.query(User).filter_by(device_id=device_id).first()
    if not u:
        return ok({"has_full": False, "chapters": []})
    from ..models import Entitlement
    ents = db.query(Entitlement).filter_by(
        user_id=u.id, book_id=book_id, status="active").all()
    has_full = any(e.scope == "full" for e in ents)
    chapters = [e.chapter_no for e in ents if e.scope == "chapter"]
    return ok({"has_full": has_full, "chapters": chapters})


@router.get("/pricing/{book_id}")
def pricing(book_id: int, db: Session = Depends(get_db)):
    b = db.get(Book, book_id)
    if not b:
        raise BizError(4201, "作品不存在")
    return ok({"free_chapters": b.free_chapters,
               "price_cents": b.price_cents,
               "chapter_price_cents": b.chapter_price_cents})


class FreeUnlockIn(BaseModel):
    device_id: str
    book_id: int
    chapter_no: int
    channel: str            # share|ad


@router.post("/unlock/free")
def free_unlock(body: FreeUnlockIn, db: Session = Depends(get_db)):
    """免费解锁单章：看广告 / 分享裂变（order_id 为空的权益即免费权益）。
    幂等：已解锁直接放行。防薅：每设备每书每日限 8 次免费解锁。
    注：ad 通道当前为 H5 广告位协议（前端倒计时），小程序化后改服务端校验广告凭证。"""
    if body.channel not in ("share", "ad"):
        raise BizError(4001, "非法解锁通道")
    b = db.get(Book, body.book_id)
    if not b or b.status != "on_shelf":
        raise BizError(4201, "作品不存在或未上架")
    if body.chapter_no <= b.free_chapters:
        return ok({"unlocked": True, "msg": "本章免费"})
    u = _user(db, body.device_id)
    from ..models import Entitlement
    exist = db.query(Entitlement).filter_by(
        user_id=u.id, book_id=body.book_id, status="active").all()
    if any(e.scope == "full" or (e.scope == "chapter" and e.chapter_no == body.chapter_no)
           for e in exist):
        return ok({"unlocked": True, "msg": "已解锁"})
    today = datetime.utcnow().date()
    today_free = [e for e in db.query(Entitlement).filter_by(
        user_id=u.id, book_id=body.book_id).all()
        if e.order_id is None and e.created_at and e.created_at.date() == today]
    if len(today_free) >= 8:
        raise BizError(4202, "今日免费次数已用完，明天再来吧")
    ent = Entitlement(user_id=u.id, book_id=body.book_id, scope="chapter",
                      chapter_no=body.chapter_no, status="active", order_id=None)
    db.add(ent); db.commit()
    return ok({"unlocked": True, "channel": body.channel,
               "remaining_today": 8 - len(today_free) - 1})
