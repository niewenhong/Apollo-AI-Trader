# API Reference

## core/engine.py — ApolloEngine

| 方法 | 说明 |
|------|------|
| `register_strategy_class(name, cls)` | 注册策略类 |
| `add_strategy(name, strategy_name, vt_symbol, settings)` | 添加策略实例 |
| `init_all()` | 初始化所有策略 |
| `start_all()` | 启动所有策略 |
| `stop_all()` | 停止所有策略 |
| `dispatch_tick(tick)` | 分发 Tick 到策略 |
| `dispatch_bar(bar)` | 分发 Bar 到策略 |
| `heartbeat()` | 返回心跳状态 |
| `handle_remote_command(cmd, ip)` | 处理远程指令 |

## core/risk_manager.py — RiskManager

| 方法 | 说明 |
|------|------|
| `reset_daily(start_equity)` | 每日重置 |
| `update_equity(current)` | 更新权益 |
| `check(symbol, price, pos, target, equity)` | 下单前检查 |
| `check_knockout(symbol, price, ko_price, is_call)` | 收回价检查 |
| `get_status()` | 返回风控状态 |

## strategies/base_strategy.py — BaseStrategy

| 方法 | 必须/可选 | 说明 |
|------|-----------|------|
| `on_init()` | 必须 | 策略初始化 |
| `calculate_signals(data)` | 必须 | 返回信号字符串 |
| `get_target_position()` | 必须 | 返回目标持仓 |
| `on_start()` | 可选 | 启动回调 |
| `on_stop()` | 可选 | 停止回调 |
| `on_tick(tick)` | 可选 | Tick 处理 |
| `on_bar(bar)` | 可选 | Bar 处理 |
| `on_order(order)` | 可选 | 订单回调 |
| `on_trade(trade)` | 可选 | 成交回调 |
| `cancel_all_orders()` | 自动 | 撤销所有订单 |
| `reload_config()` | 自动 | 热加载配置 |

## execution/allocation.py — Shelly 算法

| 方法 | 说明 |
|------|------|
| `calculate_position_size(equity, risk_pct, entry, stop, lot, tick, max)` | 计算手数 |
| `calculate_from_atr(equity, risk_pct, entry, atr, mult, lot, max)` | 基于 ATR 计算 |
| `allocate_across_accounts(total, accounts, lot)` | 多账户分配 |
| `round_to_lot(size, lot)` | 对齐手数 |

## ai/llm_client.py — LLMClient

| 方法 | 说明 |
|------|------|
| `chat(prompt, system, model)` | 发送聊天请求（自动降级） |
| `chat_json(prompt, system)` | 请求 JSON 回复 |
| `health_check()` | 检查模型健康 |

## monitoring/telegram_notifier.py — TelegramNotifier

| 方法 | 说明 |
|------|------|
| `send_message(text, level)` | 发送消息 |
| `notify_order(info)` | 订单通知 |
| `notify_trade(info)` | 成交通知 |
| `notify_alert(level, msg, ip)` | 告警 |
| `send_daily_report(report)` | 发送日报 |
| `handle_command(cmd, ip)` | 处理指令 |

## backtest/engine.py — BacktestEngine

| 方法 | 说明 |
|------|------|
| `run(strategy_cls, data, params, symbol)` | 运行回测 |
| `optimize(strategy_cls, data, grid, metric)` | 网格搜索 |
| `save_report(path)` | 保存报告 |
| `get_best_params(metric)` | 获取最优参数 |
