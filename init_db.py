import sqlite3
import os

# 获取当前脚本所在目录的绝对路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 假设数据库文件在 data 目录下，如果不确定，可以先在项目根目录找找看有没有 .db 文件
# 如果数据库就在根目录，就把下面的 'data/app.db' 改成 'app.db'
DB_PATH = os.path.join(BASE_DIR, 'data', 'apollo_trader.db') 

# 确保 data 目录存在
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def init_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print(f"正在初始化数据库: {DB_PATH}")
    
    # 1. 创建 stock_selections 表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_selections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            score REAL NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    print("✅ stock_selections 表已创建或已存在")
    
    # 2. 创建 events 表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    print("✅ events 表已创建或已存在")
    
    # 提交更改并关闭连接
    conn.commit()
    conn.close()
    print("🎉 数据库初始化完成！")

if __name__ == "__main__":
    init_database()