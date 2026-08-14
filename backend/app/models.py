"""数据模型（内容管线相关，M1）
题材模板 / 书 / 章节 / 生成任务 / 审核记录
"""
from datetime import datetime
from sqlalchemy import (Column, Integer, String, Text, DateTime, Boolean,
                        ForeignKey, JSON, Float, UniqueConstraint)
from sqlalchemy.orm import relationship
from .db import Base


class Theme(Base):
    """题材模板（含权重，供生成调度）"""
    __tablename__ = "themes"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)            # 如 年代家庭
    prompt_template = Column(Text, nullable=False)       # 题材提示词模板
    weight = Column(Float, default=1.0)                  # 生成权重（回传调整）
    enabled = Column(Boolean, default=True)
    target_chapters = Column(Integer, default=30)
    created_at = Column(DateTime, default=datetime.utcnow)
    books = relationship("Book", back_populates="theme")


class Book(Base):
    __tablename__ = "books"
    id = Column(Integer, primary_key=True)
    theme_id = Column(Integer, ForeignKey("themes.id"))
    title = Column(String(128), nullable=False)
    intro = Column(Text, default="")
    outline = Column(Text, default="")                    # 全书大纲
    status = Column(String(16), default="draft")          # draft|generating|reviewing|on_shelf|off_shelf
    total_chapters = Column(Integer, default=0)
    ai_label = Column(Boolean, default=True)              # AI 生成标识（合规必须）
    audit_report = Column(JSON, default=None)             # 全书一致性审计报告
    free_chapters = Column(Integer, default=5)            # 免费章数
    price_cents = Column(Integer, default=199)            # 整书买断价（分）
    chapter_price_cents = Column(Integer, default=10)     # 单章价（分）
    created_at = Column(DateTime, default=datetime.utcnow)
    theme = relationship("Theme", back_populates="books")
    chapters = relationship("Chapter", back_populates="book", order_by="Chapter.no")


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("book_id", "no", name="uq_book_chapter"),)
    id = Column(Integer, primary_key=True)
    book_id = Column(Integer, ForeignKey("books.id"))
    no = Column(Integer, nullable=False)                  # 第几章
    title = Column(String(128), default="")
    brief = Column(String(200), default="")               # 一句话提要
    content = Column(Text, default="")
    word_count = Column(Integer, default=0)
    # 一致性校验 + 机审结果
    consistency_conflicts = Column(JSON, default=list)    # 记忆层校验冲突
    review_status = Column(String(16), default="pending") # pending|machine_pass|machine_hit|manual_pass|rejected
    review_label = Column(String(32), default="")         # TMS 返回 Label
    tts_segments = Column(JSON, default=list)             # 听书段时间轴
    created_at = Column(DateTime, default=datetime.utcnow)
    book = relationship("Book", back_populates="chapters")


class GenTask(Base):
    """生成任务（日常/分支/新书/重写），dedup_key 防重"""
    __tablename__ = "gen_tasks"
    id = Column(Integer, primary_key=True)
    task_type = Column(String(16), nullable=False)        # new_book|daily|branch|rewrite
    book_id = Column(Integer, ForeignKey("books.id"), nullable=True)
    dedup_key = Column(String(128), unique=True, nullable=False)
    status = Column(String(16), default="queued")         # queued|running|done|failed
    payload = Column(JSON, default=dict)
    result = Column(JSON, default=dict)
    error = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class ReviewRecord(Base):
    """审核记录（机审 + 人审）"""
    __tablename__ = "review_records"
    id = Column(Integer, primary_key=True)
    chapter_id = Column(Integer, ForeignKey("chapters.id"))
    stage = Column(String(16), nullable=False)            # machine|manual
    action = Column(String(16), default="")               # pass|hit|reject|approve
    label = Column(String(32), default="")
    detail = Column(Text, default="")
    reviewer = Column(String(64), default="system")
    created_at = Column(DateTime, default=datetime.utcnow)


class InteractNode(Base):
    """章内互动节点：一个互动点 + 若干选项，选项的回响段由 LLM 生成并缓存"""
    __tablename__ = "interact_nodes"
    id = Column(Integer, primary_key=True)
    chapter_id = Column(Integer, ForeignKey("chapters.id"))
    question = Column(Text, nullable=False)                # 互动提问
    options = Column(JSON, default=list)                   # ["选项A文案", "选项B文案"]
    responses = Column(JSON, default=dict)                 # {"0": "回响段A", "1": "回响段B"}（缓存）
    position = Column(Integer, default=0)                  # 出现位置（第几段后）
    created_at = Column(DateTime, default=datetime.utcnow)


class User(Base):
    """C 端用户（内测期简化：设备标识即账号）"""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    device_id = Column(String(64), unique=True, nullable=False)  # 内测期设备即用户
    mode = Column(String(16), default="standard")                # standard|care（关怀）
    created_at = Column(DateTime, default=datetime.utcnow)


class ReadProgress(Base):
    """阅读进度（服务端为跨端准绳，按用户+书唯一）"""
    __tablename__ = "read_progress"
    __table_args__ = (UniqueConstraint("user_id", "book_id", name="uq_user_book_progress"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    book_id = Column(Integer, ForeignKey("books.id"))
    chapter_no = Column(Integer, default=1)                # 读到第几章
    position = Column(Integer, default=0)                  # 章内位置（段索引或偏移）
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Order(Base):
    """订单（管钱）。状态机：pending→paid→refunding→refunded"""
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_order_idem"),)
    id = Column(Integer, primary_key=True)
    order_no = Column(String(32), unique=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    book_id = Column(Integer, ForeignKey("books.id"))
    order_type = Column(String(16), nullable=False)        # buyout|chapter
    chapter_no = Column(Integer, nullable=True)            # 按章解锁时的章号
    amount_cents = Column(Integer, nullable=False)
    status = Column(String(16), default="pending")         # pending|paid|refunding|refunded
    idempotency_key = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)


class Entitlement(Base):
    """权益（管访问权）。订单 paid → 幂等发放；退款 → frozen→revoked"""
    __tablename__ = "entitlements"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    book_id = Column(Integer, ForeignKey("books.id"))
    scope = Column(String(16), nullable=False)             # full|chapter
    chapter_no = Column(Integer, nullable=True)
    status = Column(String(16), default="active")          # active|frozen|revoked
    order_id = Column(Integer, ForeignKey("orders.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
