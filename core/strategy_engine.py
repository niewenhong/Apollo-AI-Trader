"""
core/strategy_engine.py - v2.6.0
策略引擎：加载/启动/停止策略，管理下单
"""
import json
import os
import importlib
from typing import List
from vnpy.trader.constant import Interval
from vnpy.trader.object import BarData, SubscribeRequest
from vnpy.trader.object import OrderData, TradeData, TickData
from vnpy.trader.constant import Direction, Offset


class StrategyEngine:
    """策略引擎管理器"""

    def __init__(self, main_us=None, main_hk=None, db=None):
        self.main_us = main_us
        self.main_hk = main_hk
        self.db = db
        self.strategies = {}
        self._main_engine_map = {}

    def load_strategies(self, config_path: str = "config/strategies.json"):
        if not os.path.exists(config_path):
            print(f"[StrategyEngine] 策略配置文件不存在: {config_path}")
            return

        with open(config_path, "r", encoding="utf-8") as f:
            configs = json.load(f)

        print(f"[StrategyEngine] 加载 {len(configs)} 个策略配置")

        for cfg in configs:
            market = cfg.get("market", "US")
            class_name = cfg.get("class_name")
            strategy_name = cfg.get("strategy_name")
            vt_symbol = cfg.get("vt_symbol")
            setting = cfg.get("setting", {})

            main_engine = self.main_us if market == "US" else self.main_hk
            self._main_engine_map[strategy_name] = main_engine

            # -------- 修正点：强制要求 module 字段 --------
            module_path = cfg.get("module")
            if not module_path:
                print(f"[StrategyEngine] ❌ 策略 {class_name} 缺少 module 字段，跳过")
                continue
            # ------------------------------------------------

            try:
                module = importlib.import_module(f"strategies.{module_path}")
                strategy_class = getattr(module, class_name)
            except (ImportError, AttributeError) as e:
                print(f"[StrategyEngine] 导入策略失败 {class_name}: {e}")
                continue

            strategy = strategy_class(
                cta_engine=self,
                strategy_name=strategy_name,
                vt_symbol=vt_symbol,
                setting=setting
            )
            strategy.main_engine = main_engine

            strategy.on_init()
            self.strategies[strategy_name] = strategy
            print(f"[StrategyEngine] 策略已加载: {strategy_name} ({market})")

    def start_all(self):
        for name, strategy in self.strategies.items():
            try:
                strategy.on_start()
                main_engine = strategy.main_engine
                if main_engine:
                    self._subscribe(strategy.vt_symbol, main_engine)
                    print(f"[StrategyEngine] {name} 已启动，订阅 {strategy.vt_symbol}")
            except Exception as e:
                print(f"[StrategyEngine] 启动策略 {name} 失败: {e}")

    def stop_all(self):
        for name, strategy in self.strategies.items():
            try:
                strategy.on_stop()
                print(f"[StrategyEngine] {name} 已停止")
            except Exception as e:
                print(f"[StrategyEngine] 停止策略 {name} 失败: {e}")

    def _subscribe(self, vt_symbol: str, main_engine):
        """正确构造 SubscribeRequest 并订阅"""
        try:
            from vnpy.trader.constant import Exchange
            
            parts = vt_symbol.split(".")
            if len(parts) == 2:
                symbol, exch_str = parts
            else:
                symbol, exch_str = vt_symbol, "SMART"

            exchange_map = {
                "SEHK": Exchange.SEHK,
                "SMART": Exchange.SMART,
                "NYSE": Exchange.NYSE,
                "NASDAQ": Exchange.NASDAQ,
            }
            exchange = exchange_map.get(exch_str, Exchange.SMART)

            req = SubscribeRequest(
                symbol=symbol,
                exchange=exchange
            )
            main_engine.subscribe(req, "FUTU")
            print(f"[StrategyEngine] 订阅成功: {vt_symbol}")
        except Exception as e:
            print(f"[StrategyEngine] 订阅失败 {vt_symbol}: {e}")

    def send_order(self, strategy, vt_symbol, direction, offset, volume, order_type, price=None):
        main_engine = getattr(strategy, 'main_engine', None)
        if not main_engine:
            print(f"[StrategyEngine] ⚠️ 策略 {strategy.strategy_name} 没有引擎引用")
            return ""

        try:
            order_id = main_engine.send_order(
                symbol=vt_symbol,
                direction=direction,
                offset=offset,
                volume=volume,
                price=price,
                order_type=order_type,
                gateway_name="FUTU"
            )
            log_msg = (
                f"📤 下单: {direction.value} {vt_symbol} "
                f"数量 {volume} 价格 {price or '市价'}"
            )
            print(f"[StrategyEngine] {log_msg}")
            if self.db:
                self.db.log_event(log_msg)
            return order_id
        except Exception as e:
            err_msg = f"❌ 下单失败: {e}"
            print(f"[StrategyEngine] {err_msg}")
            if self.db:
                self.db.log_event(err_msg)
            return ""

    def load_bar(self, *args, **kwargs):
        """
        从 FUTU 加载历史K线数据
        CtaTemplate 调用: self.cta_engine.load_bar(vt_symbol, bar_count, interval, use_database)
        """
        try:
            from vnpy.trader.constant import Exchange
            from datetime import datetime, timedelta
            from futu import OpenQuoteContext, KLType, AuType, RET_ERROR, RET_OK
            from vnpy.trader.object import BarData

            # 解析参数
            if len(args) >= 2:
                vt_symbol = args[0]
                bar_count = args[1]
            else:
                vt_symbol = kwargs.get('vt_symbol', 'unknown')
                bar_count = kwargs.get('bar_count', 30)

            interval = kwargs.get('interval', Interval.MINUTE)

            # 解析 vt_symbol -> 富途代码格式
            # VeighNa: 00700.SEHK -> 富途: HK.00700
            # VeighNa: AAPL.SMART -> 富途: US.AAPL
            parts = vt_symbol.split('.')
            if len(parts) != 2:
                self.write_log(f"load_bar 失败: {vt_symbol} 格式错误")
                return []

            symbol, exch_str = parts

            if exch_str in ("SEHK", "HKEX"):
                futu_code = f"HK.{symbol.zfill(5)}"  # 港股5位代码，前补零
            elif exch_str in ("SMART", "NYSE", "NASDAQ"):
                futu_code = f"US.{symbol}"
            else:
                futu_code = symbol

            # K线类型映射
            ktype_map = {
                Interval.MINUTE: KLType.K_1M,
                Interval.HOUR: KLType.K_60M,
                Interval.DAILY: KLType.K_DAY,
                Interval.WEEKLY: KLType.K_WEEK,
            }
            ktype = ktype_map.get(interval, KLType.K_DAY)

            # 时间范围
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=bar_count)
            start_str = start_dt.strftime('%Y-%m-%d')
            end_str = end_dt.strftime('%Y-%m-%d')

            # 调用富途 API
            quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
            self.write_log(f"正在从 FUTU 拉取历史数据: {futu_code} (原始: {vt_symbol}), 天数: {bar_count}, 周期: {ktype}")

            all_bars = []
            page_req_key = None
            total_requested = 0

            while total_requested < 1000:  # 最多拉 1000 根
                ret, data, page_req_key = quote_ctx.request_history_kline(
                    code=futu_code,
                    start=start_str,
                    end=end_str,
                    ktype=ktype,
                    autype=AuType.QFQ,
                    max_count=min(1000, bar_count * 390),  # 1分钟约每天390根
                    page_req_key=page_req_key
                )

                if ret == RET_ERROR:
                    self.write_log(f"FUTU 拉取数据失败: {data}")
                    break

                if data is None or data.empty:
                    self.write_log(f"FUTU 返回空数据")
                    break

                # 转换 DataFrame -> BarData
                exchange_map = {
                    "SEHK": Exchange.SEHK,
                    "SMART": Exchange.SMART,
                    "NYSE": Exchange.NYSE,
                    "NASDAQ": Exchange.NASDAQ,
                }
                exchange = exchange_map.get(exch_str, Exchange.SMART)

                for _, row in data.iterrows():
                    try:
                        bar = BarData(
                            symbol=symbol,
                            exchange=exchange,
                            datetime=datetime.strptime(row['time_key'], '%Y-%m-%d %H:%M:%S'),
                            interval=interval,
                            volume=float(row.get('volume', 0)),
                            turnover=float(row.get('turnover', 0)),
                            open_price=float(row['open']),
                            high_price=float(row['high']),
                            low_price=float(row['low']),
                            close_price=float(row['close']),
                            gateway_name="FUTU"
                        )
                        all_bars.append(bar)
                    except Exception as e:
                        self.write_log(f"转换 Bar 数据失败: {e}")
                        continue

                total_requested += len(data)
                if not page_req_key:
                    break

            quote_ctx.close()

            self.write_log(f"成功加载 {len(all_bars)} 根历史 K 线 for {vt_symbol}")
            return all_bars

        except Exception as e:
            self.write_log(f"load_bar 异常: {str(e)}")
            import traceback
            traceback.print_exc()
            return []

    def write_log(self, msg: str, strategy=None):
        print(f"[StrategyEngine] {msg}")

    def put_event(self, event):
        pass

    def update_trading(self, strategy=None):
        pass

    def on_tick(self, vt_symbol, tick):
        for strategy in self.strategies.values():
            if getattr(strategy, 'vt_symbol', '') == vt_symbol:
                strategy.on_tick(tick)

    def on_trade(self, trade):
        for strategy in self.strategies.values():
            if hasattr(strategy, 'on_trade'):
                strategy.on_trade(trade)

    def on_order(self, order):
        for strategy in self.strategies.values():
            if hasattr(strategy, 'on_order'):
                strategy.on_order(order)

    def get_all_strategies(self):
        result = []
        for name, s in self.strategies.items():
            result.append({
                "name": name,
                "pos": getattr(s, 'pos', 0),
                "total_premium": getattr(s, 'total_premium', 0),
                "trades": getattr(s, 'trades_count', 0),
                "last_trade": getattr(s, 'last_trade_time', "")
            })
        return result