"""生成 Worker（阶段 2）：从 Redis 队列消费建书任务，多容器并行。
用法：python -m app.worker
队列：LPUSH gen:book {book_id} ；worker BRPOP 消费。
断点续跑：generate_book 内部跳过已有正文的章节。
"""
import json, logging, time
import redis

from app.core.config import settings
from app.db import SessionLocal
from app.services.content.pipeline import ContentPipeline
from app.models import GenTask, Book

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [worker] %(levelname)s %(message)s")
logger = logging.getLogger("worker")

QUEUE_KEY = "gen:book"


def main():
    r = redis.from_url(settings.REDIS_URL, decode_responses=True)
    logger.info("worker 启动，监听 %s", QUEUE_KEY)
    while True:
        try:
            item = r.brpop(QUEUE_KEY, timeout=30)
            if not item:
                continue
            _, payload = item
            data = json.loads(payload)
            book_id = int(data["book_id"])
            task_id = data.get("task_id")
            logger.info("领取任务 book=%s task=%s", book_id, task_id)
            db = SessionLocal()
            try:
                task = db.get(GenTask, task_id) if task_id else None
                if task:
                    task.status = "running"; db.commit()
                pipe = ContentPipeline(db)
                result = pipe.generate_book(book_id)
                book = db.get(Book, book_id)
                if task:
                    task.status = "done"
                    task.result = json.dumps(result, ensure_ascii=False)
                    db.commit()
                logger.info("任务完成 book=%s -> %s 状态=%s", book_id, result,
                            book.status if book else "?")
            except Exception as e:  # noqa
                logger.exception("任务失败 book=%s: %s", book_id, e)
                try:
                    if task_id:
                        task = db.get(GenTask, task_id)
                        if task:
                            task.status = "failed"
                            task.error = str(e)[:500]
                            db.commit()
                except Exception:  # noqa
                    pass
            finally:
                db.close()
        except redis.RedisError as e:
            logger.error("Redis 连接异常，10 秒后重连: %s", e)
            time.sleep(10)
        except Exception as e:  # noqa
            logger.exception("worker 循环异常: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
