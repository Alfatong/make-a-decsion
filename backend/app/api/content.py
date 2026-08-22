"""后台内容管线接口（M1）
题材 CRUD / 生成任务 / 审核队列 / 上架
写接口带 idempotency_key / dedup_key 防重。
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
import os, logging

logger = logging.getLogger(__name__)

from ..db import get_db, Base, engine
from ..core.response import ok, BizError, ERR_NOT_FOUND, ERR_REVIEW
from ..models import Theme, Book, Chapter, GenTask, ReviewRecord, EditSuggestion
from ..services.content.pipeline import ContentPipeline, OVERSEAS_GENRES
from ..services.moderation.review import ReviewService
from .admin_auth import verify_admin

router = APIRouter(prefix="/admin/api", tags=["admin-content"],
                   dependencies=[Depends(verify_admin)])


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
    theme_id: Optional[int] = None   # 海外书可空（按 genre 自动取/建题材模板）
    title: str
    chapters: Optional[int] = None
    dedup_key: str
    auto_generate: bool = False   # 是否立即逐章生成
    max_chapters: Optional[int] = None
    # 海外生产分支
    market: str = "cn"            # cn|overseas
    language: str = "zh"          # zh|en
    genre: Optional[str] = None   # 海外题材公式：werewolf|ceo|contract_marriage


@router.post("/books")
def create_book(body: NewBookIn, background: BackgroundTasks,
                db: Session = Depends(get_db)):
    """创建书籍。auto_generate=true 时逐章生成放后台任务（避免 HTTP 超时）。
    market="overseas" 时走海外生产分支：按 genre 取/建英文题材模板（默认 60 章），
    全程英文 prompt。"""
    if db.query(GenTask).filter_by(dedup_key=body.dedup_key).first():
        raise BizError(4301, "重复任务（dedup_key 已存在）")
    if body.market not in ("cn", "overseas"):
        raise BizError(4000, f"market 非法: {body.market}")
    theme_id = body.theme_id
    language = body.language
    if body.market == "overseas":
        genre = body.genre or "werewolf"
        if genre not in OVERSEAS_GENRES:
            raise BizError(4000, f"genre 非法: {genre}（可选 {sorted(OVERSEAS_GENRES)}）")
        theme_id = _get_or_create_overseas_theme(db, genre)
        if language == "zh":
            language = "en"
    if not theme_id:
        raise BizError(4000, "缺少 theme_id")
    task = GenTask(task_type="new_book", dedup_key=body.dedup_key,
                   status="running", payload=body.dict())
    db.add(task); db.commit(); db.refresh(task)
    try:
        pipe = ContentPipeline(db)
        book = pipe.create_book(theme_id, body.title, body.chapters,
                                market=body.market, language=language)
        result = {"book_id": book.id, "outline_len": len(book.outline)}
        if body.auto_generate:
            _bg_generate_book(task.id, book.id, body.max_chapters)
            task.status = "generating"
        else:
            task.status = "done"; task.result = result
            task.finished_at = datetime.utcnow()
        db.commit()
        return ok({**result, "generating": bool(body.auto_generate)})
    except Exception as e:  # noqa
        task.status = "failed"; task.error = str(e); task.finished_at = datetime.utcnow()
        db.commit()
        raise BizError(5000, f"生成失败: {e}")


def _get_or_create_overseas_theme(db: Session, genre: str) -> int:
    """海外题材模板：按 genre 查，不存在则用英文题材公式建一条（默认 60 章）。"""
    name = f"overseas-{genre}"
    t = db.query(Theme).filter_by(name=name).first()
    if t:
        return t.id
    t = Theme(name=name, prompt_template=OVERSEAS_GENRES[genre],
              weight=1.0, target_chapters=60, enabled=True)
    db.add(t); db.commit(); db.refresh(t)
    logger.info("创建海外题材模板 %s id=%s", name, t.id)
    return t.id


def _bg_generate_book(task_id: int, book_id: int, max_chapters):
    """生成任务派单：优先入 Redis 队列（worker 容器并行消费）；
    Redis 不可用时降级为 API 进程内线程（单机模式）。"""
    import json as _json
    try:
        import redis as _redis
        from ..core.config import settings
        r = _redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=3)
        r.ping()
        r.lpush("gen:book", _json.dumps({"book_id": book_id, "task_id": task_id,
                                         "max_chapters": max_chapters}))
        logger.info("任务入队 gen:book book=%s task=%s", book_id, task_id)
        return
    except Exception as e:  # noqa
        logger.warning("Redis 队列不可用，降级本地线程: %s", e)

    import threading
    from ..db import SessionLocal

    def work():
        db2 = SessionLocal()
        try:
            pipe = ContentPipeline(db2)
            gen = pipe.generate_book(book_id, max_chapters)
            t = db2.get(GenTask, task_id)
            t.status = "done"; t.result = gen; t.finished_at = datetime.utcnow()
            db2.commit()
        except Exception as e:  # noqa
            t = db2.get(GenTask, task_id)
            t.status = "failed"; t.error = str(e); t.finished_at = datetime.utcnow()
            db2.commit()
        finally:
            db2.close()
    threading.Thread(target=work, daemon=True).start()


@router.get("/gen-tasks")
def list_gen_tasks(db: Session = Depends(get_db)):
    """生成任务列表（B 端轮询进度）。"""
    rows = db.query(GenTask).order_by(GenTask.id.desc()).limit(20).all()
    return ok([{"id": t.id, "type": t.task_type, "status": t.status,
                "dedup_key": t.dedup_key, "result": t.result, "error": t.error,
                "created_at": t.created_at.isoformat() if t.created_at else ""}
               for t in rows])


@router.get("/events/stats")
def event_stats(days: int = 7, db: Session = Depends(get_db)):
    """C 端行为漏斗：锁章曝光 → 各通道点击 → 解锁成功（近 N 天）。"""
    from ..models import Event
    from sqlalchemy import func
    since = datetime.utcnow() - timedelta(days=days)
    rows = (db.query(Event.event, Event.channel, func.count())
            .filter(Event.created_at >= since)
            .group_by(Event.event, Event.channel).all())
    funnel = {}
    for event, channel, cnt in rows:
        key = f"{event}:{channel or '-'}"
        funnel[key] = cnt
    # 按天分布
    daily = (db.query(func.date(Event.created_at), Event.event, func.count())
             .filter(Event.created_at >= since)
             .group_by(func.date(Event.created_at), Event.event)
             .order_by(func.date(Event.created_at)).all())
    return ok({"since_days": days,
               "funnel": funnel,
               "daily": [{"date": str(d), "event": e, "count": c} for d, e, c in daily]})


class OutlineEditIn(BaseModel):
    outline: str
    intro: Optional[str] = None
    total_chapters: Optional[int] = None


@router.put("/books/{bid}/outline")
def edit_outline(bid: int, body: OutlineEditIn, db: Session = Depends(get_db)):
    """人工干预：编辑全书大纲/简介（建书后、逐章生成前）。"""
    b = db.get(Book, bid)
    if not b:
        raise BizError(ERR_NOT_FOUND, "书不存在")
    if b.status not in ("draft",):
        raise BizError(4003, f"当前状态 {b.status} 不可改大纲（仅草稿可改）")
    b.outline = body.outline
    if body.intro is not None:
        b.intro = body.intro
    if body.total_chapters:
        b.total_chapters = body.total_chapters
    db.commit()
    return ok({"book_id": bid, "outline_len": len(b.outline)})


class StartGenIn(BaseModel):
    max_chapters: Optional[int] = None


@router.post("/books/{bid}/generate")
def start_generate(bid: int, body: StartGenIn, db: Session = Depends(get_db)):
    """人工确认大纲后，手动触发后台逐章生成。"""
    b = db.get(Book, bid)
    if not b:
        raise BizError(ERR_NOT_FOUND, "书不存在")
    if b.status not in ("draft",):
        raise BizError(4003, f"当前状态 {b.status} 不可启动生成")
    task = GenTask(task_type="gen_book", dedup_key=f"gen-{bid}-{int(datetime.utcnow().timestamp())}",
                   status="generating", payload={"book_id": bid})
    db.add(task); db.commit(); db.refresh(task)
    _bg_generate_book(task.id, bid, body.max_chapters)
    b.status = "generating"; db.commit()
    return ok({"book_id": bid, "task_id": task.id, "status": "generating"})


@router.post("/books/{bid}/machine-review-all")
def machine_review_all(bid: int, db: Session = Depends(get_db)):
    """全书批量机审：pending 章节走一遍 TMS，pass 的进人工复核队列。"""
    b = db.get(Book, bid)
    if not b:
        raise BizError(ERR_NOT_FOUND, "书不存在")
    rs = ReviewService()
    passed, hit = 0, 0
    for ch in b.chapters:
        if ch.review_status not in ("pending",):
            continue
        try:
            res = rs.review_text(ch.content[:3000])
            ch.review_label = res.get("label", "")
            if res.get("hit"):
                ch.review_status = "machine_hit"; hit += 1
            else:
                ch.review_status = "machine_pass"; passed += 1
        except Exception as e:  # noqa
            logger.warning("机审失败 ch%s: %s", ch.id, e)
    db.commit()
    return ok({"book_id": bid, "machine_pass": passed, "machine_hit": hit})


@router.get("/books")
def list_books(db: Session = Depends(get_db)):
    rows = db.query(Book).all()
    return ok([{"id": b.id, "title": b.title, "status": b.status,
                "total_chapters": b.total_chapters, "intro": b.intro or "",
                "market": b.market or "cn", "language": b.language or "zh",
                "theme": b.theme.name if b.theme else "",
                "chapters": len(b.chapters)} for b in rows])


@router.get("/books/{bid}")
def book_detail(bid: int, db: Session = Depends(get_db)):
    b = db.get(Book, bid)
    if not b:
        raise BizError(ERR_NOT_FOUND, "书不存在")
    return ok({"id": b.id, "title": b.title, "intro": b.intro, "outline": b.outline,
               "status": b.status, "total_chapters": b.total_chapters,
               "market": b.market or "cn", "language": b.language or "zh",
               "chapters": [{"no": c.no, "title": c.title, "word_count": c.word_count,
                             "review_status": c.review_status,
                             "has_edited": bool(c.edited_content),
                             "has_es": bool(c.es_content),
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
                "title": c.title, "word_count": c.word_count,
                "book_title": c.book.title if c.book else "",
                "review_status": c.review_status, "label": c.review_label,
                "conflicts": c.consistency_conflicts} for c in rows])


@router.get("/chapters/{cid}")
def chapter_detail(cid: int, db: Session = Depends(get_db)):
    """审核台章节详情：正文 + 机审记录。"""
    ch = db.get(Chapter, cid)
    if not ch:
        raise BizError(ERR_NOT_FOUND, "章节不存在")
    records = (db.query(ReviewRecord).filter_by(chapter_id=cid)
               .order_by(ReviewRecord.id.desc()).limit(10).all())
    return ok({
        "chapter_id": ch.id, "book_id": ch.book_id, "no": ch.no,
        "title": ch.title, "content": ch.content, "word_count": ch.word_count,
        "review_status": ch.review_status, "label": ch.review_label,
        "conflicts": ch.consistency_conflicts,
        "records": [{"stage": r.stage, "action": r.action, "label": r.label,
                     "detail": r.detail,
                     "created_at": r.created_at.isoformat() if r.created_at else ""}
                    for r in records],
    })


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
    # 一致性审计门槛：没有报告先跑一次；FAIL 不允许上架
    report = b.audit_report
    if not report:
        report = ContentPipeline(db).consistency_audit(bid)
    if not report.get("passed"):
        raise BizError(ERR_REVIEW,
                       f"一致性审计未通过：表外人物{len(report.get('violations', []))}处，"
                       f"低覆盖人物{len(report.get('low_coverage', {}))}个，"
                       f"过短章节{len(report.get('short_chapters', []))}章。请先修复再上架")
    b.status = "on_shelf"; db.commit()
    return ok({"book_id": bid, "status": "on_shelf"})


@router.get("/books/{bid}/audit")
def get_audit(bid: int, db: Session = Depends(get_db)):
    """查看/触发全书一致性审计报告。"""
    b = db.get(Book, bid)
    if not b:
        raise BizError(ERR_NOT_FOUND, "书不存在")
    report = b.audit_report or ContentPipeline(db).consistency_audit(bid)
    return ok(report)


@router.post("/books/{bid}/off-shelf")
def off_shelf_book(bid: int, db: Session = Depends(get_db)):
    """整书下架。"""
    b = db.get(Book, bid)
    if not b:
        raise BizError(ERR_NOT_FOUND, "书不存在")
    b.status = "off_shelf"; db.commit()
    return ok({"book_id": bid, "status": "off_shelf"})


# ---------- 海外生产分支：试读 / 建议审阅 / 西语翻译 / 全书导出 ----------

def _get_overseas_book(db: Session, bid: int) -> Book:
    b = db.get(Book, bid)
    if not b:
        raise BizError(ERR_NOT_FOUND, "书不存在")
    if (b.market or "cn") != "overseas":
        raise BizError(4000, "该书不是海外书（market != overseas）")
    return b


@router.post("/books/{bid}/proofread")
def proofread_book(bid: int, db: Session = Depends(get_db)):
    """触发海外书试读（挑剔读者 pass，同步执行）：产出修改建议入 EditSuggestion。"""
    _get_overseas_book(db, bid)
    from ..services.content.proofread import ProofreadPass
    try:
        result = ProofreadPass(db).run(bid)
    except Exception as e:  # noqa
        raise BizError(5000, f"试读失败: {e}")
    return ok({"book_id": bid, **result})


@router.get("/books/{bid}/suggestions")
def list_suggestions(bid: int, status: str = "", db: Session = Depends(get_db)):
    """海外书修改建议列表（可按 status=pending|applied|rejected 过滤）。"""
    _get_overseas_book(db, bid)
    q = db.query(EditSuggestion).filter_by(book_id=bid)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(EditSuggestion.id).all()
    return ok([{"id": s.id, "book_id": s.book_id, "chapter_no": s.chapter_no,
                "issue_zh": s.issue_zh, "excerpt": s.excerpt,
                "replacement": s.replacement, "status": s.status,
                "created_at": s.created_at.isoformat() if s.created_at else ""}
               for s in rows])


@router.post("/suggestions/{sid}/apply")
def apply_suggestion(sid: int, db: Session = Depends(get_db)):
    """应用一条修改建议：校验 excerpt 是 edited_content/content 的精确子串后替换。
    替换结果写入 edited_content（首次写前把原稿备份进 raw_content），不覆盖 content。
    子串找不到返回 409。"""
    s = db.get(EditSuggestion, sid)
    if not s:
        raise BizError(ERR_NOT_FOUND, "建议不存在")
    if s.status != "pending":
        raise BizError(ERR_CONFLICT, f"建议已处理（status={s.status}）", http_status=409)
    ch = (db.query(Chapter)
          .filter_by(book_id=s.book_id, no=s.chapter_no).first())
    if not ch:
        raise BizError(ERR_NOT_FOUND, f"第{s.chapter_no}章不存在")
    base = ch.edited_content or ch.content or ""
    if s.excerpt not in base:
        raise BizError(ERR_CONFLICT,
                       "excerpt 不是该章现有文本的精确子串（可能已被其他建议改过），未应用",
                       http_status=409)
    if not ch.raw_content:
        ch.raw_content = ch.content
    ch.edited_content = base.replace(s.excerpt, s.replacement, 1)
    ch.word_count = len(ch.edited_content.split())
    s.status = "applied"
    db.add(ReviewRecord(chapter_id=ch.id, stage="manual", action="approve",
                        label="proofread",
                        detail=f"建议#{sid} 应用：{s.issue_zh[:100]}"))
    db.commit()
    return ok({"suggestion_id": sid, "status": "applied", "chapter_no": ch.no})


@router.post("/suggestions/{sid}/reject")
def reject_suggestion(sid: int, db: Session = Depends(get_db)):
    """忽略一条修改建议。"""
    s = db.get(EditSuggestion, sid)
    if not s:
        raise BizError(ERR_NOT_FOUND, "建议不存在")
    if s.status != "pending":
        raise BizError(ERR_CONFLICT, f"建议已处理（status={s.status}）", http_status=409)
    s.status = "rejected"
    ch = (db.query(Chapter)
          .filter_by(book_id=s.book_id, no=s.chapter_no).first())
    db.add(ReviewRecord(chapter_id=ch.id if ch else None, stage="manual",
                        action="reject", label="proofread",
                        detail=f"建议#{sid} 忽略：{s.issue_zh[:100]}"))
    db.commit()
    return ok({"suggestion_id": sid, "status": "rejected"})


@router.post("/books/{bid}/translate_es")
def translate_book_es_endpoint(bid: int, db: Session = Depends(get_db)):
    """触发海外书西语翻译。章节级耗时长，放后台线程执行，前端轮询书详情看 has_es 进度。"""
    _get_overseas_book(db, bid)
    import threading
    from ..db import SessionLocal

    def work():
        db2 = SessionLocal()
        try:
            from ..services.content.translate import translate_book_es
            result = translate_book_es(db2, bid)
            logger.info("书%d西语翻译完成: %s", bid, result)
        except Exception as e:  # noqa
            logger.error("书%d西语翻译异常: %s", bid, e)
        finally:
            db2.close()
    threading.Thread(target=work, daemon=True).start()
    return ok({"book_id": bid, "started": True})


@router.get("/books/{bid}/export")
def export_book(bid: int, version: str = "edited", db: Session = Depends(get_db)):
    """导出海外书全本纯文本。version=raw|edited|es。
    raw=原稿（raw_content 无则 content）；edited=修订稿（edited_content 无则 content）；
    es=西语译稿（只含已译章节）。"""
    b = _get_overseas_book(db, bid)
    if version not in ("raw", "edited", "es"):
        raise BizError(4000, f"version 非法: {version}（raw|edited|es）")
    chapters = sorted(b.chapters, key=lambda c: c.no)
    parts = []
    for c in chapters:
        if version == "raw":
            body = c.raw_content or c.content
        elif version == "edited":
            body = c.edited_content or c.content
        else:
            body = c.es_content
        if body:
            parts.append(f"Chapter {c.no}\n\n{body.strip()}")
    if not parts:
        raise BizError(ERR_NOT_FOUND, f"该书没有可导出的 {version} 版本内容")
    text = f"{b.title}\n{'=' * 40}\n\n" + "\n\n\n".join(parts) + "\n"
    from urllib.parse import quote
    from fastapi.responses import Response
    fname = f"{b.title}_{version}.txt"
    disposition = (f"attachment; filename=\"book{bid}_{version}.txt\"; "
                   f"filename*=UTF-8''{quote(fname)}")
    return Response(content=text.encode("utf-8"),
                    media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": disposition})
