import sqlite3

conn = sqlite3.connect("data/history.db")
cur = conn.cursor()

# 列出所有表
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [t[0] for t in cur.fetchall()]
print("数据库中的表:", tables)

# 查找可能的策略状态表
for table in ["strategy_status", "strategy_config", "strategies"]:
    if table in tables:
        print(f"\n--- {table} ---")
        try:
            cur.execute(f"SELECT * FROM {table} LIMIT 5")
            cols = [desc[0] for desc in cur.description]
            print("列名:", cols)
            rows = cur.fetchall()
            for row in rows:
                print(row)
        except Exception as e:
            print(f"查询失败: {e}")

conn.close()