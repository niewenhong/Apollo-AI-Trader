#!/bin/bash
# start.sh — Apollo AI Trader v2.8.0 容器启动脚本
set -e

echo "🚀 Apollo AI Trader v2.8.0 容器启动"
echo "   用户ID: ${USER_ID:-default_user}"
echo "   数据库: ${DB_PATH:-/app/data/trading.db}"
echo "   富途: ${FUTU_HOST:-127.0.0.1}:${FUTU_PORT:-11111}"
echo "   环境: ${TRADE_ENV:-SIMULATE}"

# 等待 OpenD 就绪
echo "⏳ 等待 OpenD 连接..."
for i in $(seq 1 30); do
    if python -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.connect(('${FUTU_HOST:-127.0.0.1}', ${FUTU_PORT:-11111}))
    print('OK')
except:
    print('WAIT')
s.close()
" | grep -q "OK"; then
        echo "✅ OpenD 已就绪"
        break
    fi
    sleep 2
done

# 初始化数据库
echo "📊 初始化数据库..."
python -c "
import sys
sys.path.insert(0, '/app')
from core.data_fetcher import init_database
init_database('${DB_PATH:-/app/data/trading.db}')
print('✅ 数据库就绪')
"

# 启动主程序
echo "▶️ 启动策略引擎..."
exec python /app/main.py
