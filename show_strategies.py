import sqlite3

conn = sqlite3.connect("data/history.db")
cur = conn.cursor()
cur.execute("SELECT strategy_name, vt_symbol, market, status FROM strategy_config ORDER BY market, strategy_name")
rows = cur.fetchall()
print(f"共 {len(rows)} 个策略\n")
print(f"{'策略名称':40} {'标的':20} {'市场':5} {'状态':10}")
print("-"*75)
us_count = hk_count = 0
for name, symbol, market, status in rows:
    print(f"{name:40} {symbol:20} {market:5} {status:10}")
    if market == "US": us_count += 1
    else: hk_count += 1
print("-"*75)
print(f"美股: {us_count}  港股: {hk_count}  总计: {len(rows)}")
conn.close()