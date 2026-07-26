"""
Dockerfile — Apollo AI Trader v2.8.0 用户容器镜像
"""
FROM python:3.11-slim

# 系统依赖
RUN apt-get update && apt-get install -y \
    gcc g++ make \
    && rm -rf /var/lib/apt/lists/*

# 工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建必要目录
RUN mkdir -p logs data

# 环境变量默认值
ENV USER_ID=default_user
ENV DB_PATH=/app/data/trading.db
ENV FUTU_HOST=127.0.0.1
ENV FUTU_PORT=11111
ENV TRADE_ENV=SIMULATE

# 启动脚本
COPY start.sh .
RUN chmod +x start.sh

# 健康检查
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sqlite3; conn=sqlite3.connect('/app/data/trading.db'); conn.execute('SELECT 1'); print('OK')" || exit 1

CMD ["bash", "/app/start.sh"]
