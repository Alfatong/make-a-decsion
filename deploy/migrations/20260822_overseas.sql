-- 20260822 海外小说生产分支（PostgreSQL，可重复执行）
-- books: 市场/语言标记；chapters: 原稿备份/修订稿/西语译稿；edit_suggestions: 试读修改建议

ALTER TABLE books ADD COLUMN IF NOT EXISTS market VARCHAR(16) NOT NULL DEFAULT 'cn';
ALTER TABLE books ADD COLUMN IF NOT EXISTS language VARCHAR(8) NOT NULL DEFAULT 'zh';

ALTER TABLE chapters ADD COLUMN IF NOT EXISTS raw_content TEXT NOT NULL DEFAULT '';
ALTER TABLE chapters ADD COLUMN IF NOT EXISTS edited_content TEXT NOT NULL DEFAULT '';
ALTER TABLE chapters ADD COLUMN IF NOT EXISTS es_content TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS edit_suggestions (
    id SERIAL PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id),
    chapter_no INTEGER NOT NULL,
    issue_zh TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    replacement TEXT NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_edit_suggestions_book_status
    ON edit_suggestions (book_id, status);
