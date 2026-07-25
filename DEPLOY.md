# Apollo AI Trader v2.7.0 部署指南

## 文件清单（18个文件）

```
main.py                                           ← 覆盖
UPGRADE_NOTES.md                                   ← 新建（说明文档）
CHANGELOG.md                                       ← 覆盖
DEPLOY.md                                          ← 新建（本文件）
test_validation.py                                 ← 新建（验证脚本）

core/
  __init__.py                                      ← 新建
  subscription_manager.py                          ← 新建（全套订阅管理）
  multi_period_db.py                               ← 新建（多周期数据库）
  market_data_bus.py                               ← 新建（行情总线）
  strategy_matcher.py                              ← 新建（策略匹配）

backtest/
  __init__.py                                      ← 新建
  multi_period_engine.py                           ← 新建（多周期回测）

vnpy_futu/
  __init__.py                                      ← 新建
  vnpy_futu/
    __init__.py                                    ← 新建
    multi_period_kline_handler.py                  ← 新建（多周期K线回调）
```

## 部署步骤（3分钟）

### Step 1: 备份
```powershell
# 备份当前版本
Copy-Item D:\Apollo-AI-Trader D:\Apollo-AI-Trader-v2.6.0-backup -Recurse -Force
```

### Step 2: 解压覆盖
```powershell
# 解压到项目目录（覆盖已有文件）
Expand-Archive -Path .\Apollo-AI-Trader-v2.7.0.zip -DestinationPath D:\Apollo-AI-Trader -Force
```

### Step 3: 手动补丁（必须！）
编辑 `D:\Apollo-AI-Trader\vnpy_futu\vnpy_futu\futu_gateway.py`

在 `connect()` 方法末尾（最后一个 `self.quote_ctx.start()` 之前）添加：
```python
from vnpy_futu.multi_period_kline_handler import MultiPeriodKlineHandler
self.kline_handler = MultiPeriodKlineHandler(self)
self.quote_ctx.set_handler(self.kline_handler)
```

### Step 4: 验证
```powershell
# 运行验证脚本
cd D:\Apollo-AI-Trader
C:\veighna_studio\python.exe test_validation.py
```
应输出：`🎉 全部通过！升级包可安全部署。`

### Step 5: 启动
```powershell
C:\veighna_studio\python.exe main.py
```

## 验证清单
- [ ] `✅ 全套订阅: US.AAPL (K_1M+5M+15M+60M)`
- [ ] `✅ 全套订阅: HK.00700 (K_1M+5M+15M+60M)`
- [ ] `📊 额度: 已用X/300 剩余Y`
- [ ] `[BAR] US.AAPL MINUTE 1 O=... H=... L=... C=...`
- [ ] `📈 日线 US.AAPL: XX条`
- [ ] `[GATE] US.AAPL 15m OK`
- [ ] 数据库 `data/history.db` 含 `kline_1m/5m/15m/60m/1d` 表
- [ ] Telegram `/status` 正常响应

## 回滚方案
```powershell
# 如果出问题，恢复到v2.6.0
Remove-Item D:\Apollo-AI-Trader -Recurse -Force
Move-Item D:\Apollo-AI-Trader-v2.6.0-backup D:\Apollo-AI-Trader
```

## 注意事项
1. 反订阅有60秒延迟（富途硬约束）——代码已自动处理
2. 美股订阅自动带 session=ALL（盘前盘后）
3. 日线走历史接口，不占订阅额度
4. 回测100%读本地库，0额度消耗
5. 首次运行自动创建多周期表
6. 保留你的 `config/` 和 `data/` 目录不动
