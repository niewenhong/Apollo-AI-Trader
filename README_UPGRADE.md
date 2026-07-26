# ============================================
# Apollo AI Trader v2.8.0 — 升级包说明
# ============================================
#
# 本次升级核心内容：
#   1. Regime Trainer（多周期概率分布 + GMM 集成 + Walk-Forward 验证）
#   2. Strategy Matcher（概率加权策略路由）
#   3. Param Optimizer（离线贝叶斯参数优化）
#   4. Trade Mode Manager（模拟/实盘一键切换 + 风险提示 + 热加载）
#   5. Telegram Bot（独立 Bot Token 支持，命令交互）
#   6. Risk Profiles（保守/稳健/进取/激进 四级模板）
#   7. Docker 容器化（K8s 就绪）
#   8. 共享数据库 Schema（用户隔离 + 全局共享）
#
# ─────────────────────────────────────────────
# 安装步骤：
# ─────────────────────────────────────────────
#
# 1. 备份现有数据库
#    cp data/apollo.db data/apollo.db.backup
#
# 2. 安装依赖
#    pip install -r requirements.txt
#
# 3. 初始化数据库（创建新表）
#    python -c "from core.data_fetcher import init_database; init_database('data/trading.db')"
#
# 4. 拉取历史K线（首次需要富途连接）
#    python -c "
#    import futu
#    ctx = futu.OpenQuoteContext(host='127.0.0.1', port=11111)
#    from core.data_fetcher import fetch_all_intervals, save_klines
#    for sym in ['NVDA','AAPL','MSFT','AMZN','META','GOOGL','AMD','TSLA']:
#        fetch_all_intervals(ctx, sym, 'US', 'data/trading.db')
#    ctx.close()
#    "
#
# 5. 运行 Regime 检测
#    python -c "
#    from core.regime_trainer import batch_detect
#    syms = ['NVDA','AAPL','MSFT','AMZN','META','GOOGL','AMD','TSLA']
#    results = batch_detect('data/trading.db', syms)
#    for s, p in results.items():
#        print(f'{s}: {p}')
#    "
#
# 6. 运行 Walk-Forward 验证
#    python -c "
#    from core.regime_trainer import walk_forward_validate
#    for s in ['NVDA','AAPL']:
#        wf = walk_forward_validate('data/trading.db', s, 'SMART')
#        print(f'{s}: {wf}')
#    "
#
# 7. 运行参数优化（夜间任务，可后台运行）
#    python -c "
#    from core.param_optimizer import ParamOptimizer
#    opt = ParamOptimizer('data/trading.db')
#    syms = ['NVDA','AAPL','MSFT','AMZN','META','GOOGL','AMD','TSLA']
#    for s in syms:
#        for st in ['TrendStrategy','GridStrategy','OrderFlowStrategy']:
#            opt.optimize(s, st, 'all', n_trials=100)
#    "
#
# 8. 启动主程序（含调度器 + Telegram Bot）
#    python main.py
#
# ─────────────────────────────────────────────
# 多用户 K8s 部署：
# ─────────────────────────────────────────────
#
# 每个用户一个 Pod，通过环境变量注入：
#   USER_ID, DB_PATH, FUTU_HOST, FUTU_PORT, TRADE_ENV
#   TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
#
# 共享 PostgreSQL + Redis（见 core/data_fetcher.py 和 core/telegram_bot.py）
#
# ─────────────────────────────────────────────
# 验证清单：
# ─────────────────────────────────────────────
#
# [ ] 数据库表创建成功（kline_cache, regime_records, param_optimization_results, user_*）
# [ ] Regime 概率输出合理（每只股票输出 trend/range/volatile 概率）
# [ ] Walk-Forward WFE > 0.5（至少部分股票达标）
# [ ] 策略权重分配正确（不同 regime 下权重分布不同）
# [ ] 参数优化结果写入数据库
# [ ] Telegram Bot 响应命令（/start, /status, /position, /help）
# [ ] 模拟/实盘切换提示风险并需确认
# [ ] 调度器正常启动（regime 每30分钟，优化每日22:00）
#
# ============================================
# 版本: v2.8.0
# 日期: 2026-07-26
# 作者: Apollo Team
# ============================================
