# -*- coding: utf-8 -*-
"""
diag_account.py - 账户查询诊断工具（v2.4.0）

用法：
  python diag_account.py [US|HK]

输出：
  - 测试 FutuGateway 连接
  - 打印 accountid / balance / frozen / available
  - 验证双 Gateway 下资金隔离是否正确
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy_futu.futu_gateway import FutuGateway
from futu import TrdEnv

def main():
    market = sys.argv[1] if len(sys.argv) > 1 else "US"
    print(f"[Diag] 测试市场: {market}")

    # 加载配置
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path, "r", encoding="utf-8-sig") as f:
        config = json.load(f)

    env_str = config.get("futu_environment", "SIMULATE")
    env = TrdEnv.REAL if env_str.upper() == "REAL" else TrdEnv.SIMULATE

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    gw_name = f"FUTU_{market}"
    print(f"[Diag] 创建网关: {gw_name}")

    gw = main_engine.add_gateway(FutuGateway, gw_name)

    setting = {
        "密码": config.get("futu_password", ""),
        "地址": config.get("futu_address", "127.0.0.1"),
        "端口": config.get("futu_port", 11111),
        "市场": market,
        "环境": env,
    }
    main_engine.connect(setting, gw_name)

    print(f"[Diag] 等待 5s 让连接就绪...")
    time.sleep(5)

    print(f"\n查询账户...")
    gw.query_account()
    time.sleep(1)

    accounts = main_engine.get_all_accounts()
    print(f"共 {len(accounts)} 个账户\n")

    for acc in accounts:
        print(f"=== AccountData ===")
        print(f"  accountid   = {getattr(acc, 'accountid', '?')}")
        print(f"  gateway_name = {getattr(acc, 'gateway_name', '?')}")
        print(f"  balance      = {getattr(acc, 'balance', 0):,.2f}")
        print(f"  frozen       = {getattr(acc, 'frozen', 0):,.2f}")
        bal = getattr(acc, 'balance', 0.0) or 0.0
        frz = getattr(acc, 'frozen', 0.0) or 0.0
        ava = getattr(acc, 'available', bal - frz) or 0.0
        print(f"  available    = {ava:,.2f}  (计算: balance - frozen)")
        print(f"  extra        = {getattr(acc, 'extra', None)}")
        print(f"  vt_accountid = {getattr(acc, 'vt_accountid', '?')}")
        print()

    # 验证
    print("=" * 40)
    print("验证结果:")
    for acc in accounts:
        bal = getattr(acc, 'balance', 0.0) or 0.0
        frz = getattr(acc, 'frozen', 0.0) or 0.0
        ava = getattr(acc, 'available', bal - frz) or 0.0

        if frz < 0:
            print(f"  ❌ {getattr(acc,'accountid','?')}: frozen < 0，异常！")
        elif ava == bal and frz == 0:
            print(f"  ✅ {getattr(acc,'accountid','?')}: 无持仓，可用=总资产={ava:,.0f}")
        elif ava > 0:
            print(f"  ✅ {getattr(acc,'accountid','?')}: 可用={ava:,.2f}, 冻结={frz:,.2f}")
        else:
            print(f"  ⚠️ {getattr(acc,'accountid','?')}: 可用={ava:,.2f}（可能无资金）")

    # 清理
    main_engine.close()
    print("\n[Diag] 完成，连接已关闭")

if __name__ == "__main__":
    main()
