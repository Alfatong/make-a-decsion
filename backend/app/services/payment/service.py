"""模拟付费服务（V1 不接真实支付，但状态机与权益一致性按生产标准）
- 订单与 entitlement 分离：订单管钱、权益管访问权
- 模拟收银台回调置 paid，权益服务幂等发放
- 退款：refunding → 权益 frozen → refunded → 权益 revoked
- 消费限额：Redis DECRBY 原子预占 + 失败回补（每日限额，保护老年用户）
- 所有写接口带 idempotency_key
"""
from __future__ import annotations
import time, uuid, logging
from typing import Dict, Optional
from datetime import datetime, date
from sqlalchemy.orm import Session
import redis

from ...core.config import settings
from ...models import Order, Entitlement, Book, User

logger = logging.getLogger(__name__)

DAILY_LIMIT_CENTS = 5000  # 每日消费限额 50 元（老年用户保护）


class PaymentError(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code; self.msg = msg
        super().__init__(msg)


class PaymentService:
    def __init__(self, db: Session):
        self.db = db
        self.redis = redis.from_url(settings.REDIS_URL, decode_responses=True)

    # ---------- 限额（Redis 原子预占 + 回补） ----------
    def _limit_key(self, user_id: int) -> str:
        return f"spend:{user_id}:{date.today().isoformat()}"

    def reserve_limit(self, user_id: int, amount_cents: int) -> None:
        """限额原子预占：spent 累加，超额拒绝。"""
        spent_key = self._limit_key(user_id) + ":spent"
        spent = int(self.redis.get(spent_key) or 0)
        if spent + amount_cents > DAILY_LIMIT_CENTS:
            raise PaymentError(4001, f"已达每日消费限额 {DAILY_LIMIT_CENTS/100:.0f} 元，明天再来")
        self.redis.incrby(spent_key, amount_cents)
        self.redis.expire(spent_key, 60 * 60 * 48)

    def release_limit(self, user_id: int, amount_cents: int) -> None:
        self.redis.decrby(self._limit_key(user_id) + ":spent", amount_cents)

    # ---------- 下单 ----------
    def create_order(self, user: User, book_id: int, order_type: str,
                     idempotency_key: str, chapter_no: Optional[int] = None) -> Order:
        # 幂等：同 idempotency_key 直接返回已有订单
        existing = self.db.query(Order).filter_by(idempotency_key=idempotency_key).first()
        if existing:
            return existing
        book = self.db.get(Book, book_id)
        if not book or book.status != "on_shelf":
            raise PaymentError(4201, "作品不存在或未上架")
        if order_type == "buyout":
            amount = book.price_cents
        elif order_type == "chapter":
            if not chapter_no or chapter_no < 1 or chapter_no > book.total_chapters:
                raise PaymentError(4000, "无效章节")
            amount = book.chapter_price_cents
        else:
            raise PaymentError(4000, "无效订单类型")
        # 已拥有校验
        if self.has_access(user.id, book_id, chapter_no if order_type == "chapter" else None):
            raise PaymentError(4002, "已拥有该内容，无需重复购买")
        # 限额预占
        self.reserve_limit(user.id, amount)
        try:
            order = Order(order_no=uuid.uuid4().hex[:24], user_id=user.id, book_id=book_id,
                          order_type=order_type, chapter_no=chapter_no,
                          amount_cents=amount, status="pending",
                          idempotency_key=idempotency_key)
            self.db.add(order); self.db.commit(); self.db.refresh(order)
            return order
        except Exception:
            self.release_limit(user.id, amount)  # 失败回补
            self.db.rollback()
            raise

    # ---------- 模拟支付成功（幂等发放权益） ----------
    def mock_pay(self, order_id: int) -> Order:
        order = self.db.get(Order, order_id)
        if not order:
            raise PaymentError(4201, "订单不存在")
        if order.status == "paid":
            return order  # 幂等
        if order.status != "pending":
            raise PaymentError(4003, f"订单状态异常: {order.status}")
        order.status = "paid"
        order.paid_at = datetime.utcnow()
        self._grant_entitlement(order)
        self.db.commit(); self.db.refresh(order)
        return order

    def _grant_entitlement(self, order: Order) -> None:
        """幂等发放：同订单已发权益则跳过。"""
        dup = self.db.query(Entitlement).filter_by(order_id=order.id).first()
        if dup:
            return
        ent = Entitlement(
            user_id=order.user_id, book_id=order.book_id,
            scope="full" if order.order_type == "buyout" else "chapter",
            chapter_no=order.chapter_no, status="active", order_id=order.id)
        self.db.add(ent)

    # ---------- 退款 ----------
    def refund(self, order_id: int) -> Order:
        order = self.db.get(Order, order_id)
        if not order:
            raise PaymentError(4201, "订单不存在")
        if order.status == "refunded":
            return order
        if order.status != "paid":
            raise PaymentError(4003, "仅已支付订单可退款")
        # refunding：先冻结权益
        order.status = "refunding"
        ent = self.db.query(Entitlement).filter_by(order_id=order.id).first()
        if ent:
            ent.status = "frozen"
        self.db.commit()
        # 模拟退款完成 → revoked + 回补限额
        order.status = "refunded"
        if ent:
            ent.status = "revoked"
        self.release_limit(order.user_id, order.amount_cents)
        self.db.commit(); self.db.refresh(order)
        return order

    # ---------- 访问权校验 ----------
    def has_access(self, user_id: int, book_id: int, chapter_no: Optional[int]) -> bool:
        book = self.db.get(Book, book_id)
        if not book:
            return False
        if chapter_no is not None and chapter_no <= book.free_chapters:
            return True  # 免费章
        q = self.db.query(Entitlement).filter_by(
            user_id=user_id, book_id=book_id, status="active")
        for ent in q.all():
            if ent.scope == "full":
                return True
            if ent.scope == "chapter" and ent.chapter_no == chapter_no:
                return True
        return False
