"""故事事实库（生产版）
长篇一致性的事实来源：角色状态/道具归属/住处/时间线。
设计原则（S2 验证结论）：
  - 关键硬事实【从大纲结构化预置】，不靠模型读后抽取
  - 模型抽取仅作增量补充，且不覆盖可信主体
  - 事实带来源标记（outline 预置 / model 增量），校验只用高可信事实
"""
from __future__ import annotations
import sqlite3, os, time, threading
from typing import Optional, List, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id TEXT NOT NULL,
  fact_type TEXT NOT NULL,      -- character|item|timeline|foreshadow
  subject TEXT NOT NULL,
  attr TEXT NOT NULL,           -- alive|location|state|season
  value TEXT NOT NULL,
  source TEXT NOT NULL,         -- outline(预置,高可信)|model(增量,低可信)
  chapter INTEGER NOT NULL,
  created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_book_subj ON facts(book_id, fact_type, subject);
"""


class FactStore:
    """按 book_id 隔离的事实库。生产环境用 PostgreSQL 实现同一接口。"""

    def __init__(self, path: str, book_id: str):
        self.book_id = book_id
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def add(self, fact_type: str, subject: str, attr: str, value: str,
            chapter: int, source: str = "outline"):
        with self._lock:
            self.conn.execute(
                "INSERT INTO facts(book_id,fact_type,subject,attr,value,source,chapter,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (self.book_id, fact_type, subject, attr, value, source, chapter, int(time.time())))
            self.conn.commit()

    def latest(self, fact_type: str, subject: str, attr: str,
               trusted_only: bool = True) -> Tuple[Optional[str], Optional[int]]:
        """取主体某属性最新值。trusted_only=True 时只看 outline 预置的高可信事实。"""
        sql = ("SELECT value,chapter FROM facts WHERE book_id=? AND fact_type=? AND subject=? AND attr=?")
        params: list = [self.book_id, fact_type, subject, attr]
        if trusted_only:
            sql += " AND source='outline'"
        sql += " ORDER BY chapter DESC,id DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def snapshot(self, trusted_only: bool = True) -> str:
        """导出事实快照供生成前注入。"""
        sql = "SELECT fact_type,subject,attr,value,chapter FROM facts WHERE book_id=?"
        params: list = [self.book_id]
        if trusted_only:
            sql += " AND source='outline'"
        sql += " ORDER BY chapter,id"
        rows = self.conn.execute(sql, params).fetchall()
        return "\n".join(f"[第{ch}章] {s}·{a} = {v}" for ft, s, a, v, ch in rows)

    def close(self):
        self.conn.close()
