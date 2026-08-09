FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
WORKDIR /app

# 系统依赖（psycopg2 编译等）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（分层缓存）
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY backend/app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
