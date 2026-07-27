"""
core/duallink.py - v2.8.4 双引擎版（修复 is_connected 与重连参数）
变更说明：
  - 使用网关对象的 quote_ctx/trade_ctx 判断连接状态
  - 重连时使用正确的 TrdEnv 枚举值
  - 保存初始 setting 以便重连时复用
"""
import time
import logging
from datetime import datetime
from typing import Optional

from vnpy.trader.constant import Exchange
from futu import TrdEnv

logger = logging.getLogger("DualLink")


class DualLink:
    """
    双链路管理器：定期检查美股/港股行情与交易接口存活
    v2.8.4: 修复 is_connected 调用与重连参数
    """

    def __init__(self, main_us=None, main_hk=None, db=None, config: dict = None):
        self.main_us = main_us
        self.main_hk = main_hk
        self.db = db
        self.config = config or {}
        self._running = False
        self._last_us_state = False
        self._last_hk_state = False
        self._reconnect_attempts = {"us": 0, "hk": 0}
        self._max_reconnect = self.config.get("duallink", {}).get("max_reconnect", 5)
        self._check_interval = self.config.get("duallink", {}).get("check_interval", 60)

        # 保存初始连接设置，用于重连
        self._initial_settings = {
            "US": None,
            "HK": None,
        }

    def save_initial_settings(self, us_setting: dict, hk_setting: dict):
        """由 main.py 在首次连接后调用，保存 setting"""
        self._initial_settings["US"] = us_setting.copy()
        self._initial_settings["HK"] = hk_setting.copy()

    # ──────────────────────────────
    #  健康检查（修复：不使用 is_connected）
    # ──────────────────────────────
    def _get_gateway(self, market: str):
        """获取指定市场的网关实例"""
        if market == "US":
            return self.main_us.gateways.get("FUTU_US") if self.main_us else None
        else:
            return self.main_hk.gateways.get("FUTU_HK") if self.main_hk else None

    def _is_gateway_alive(self, gateway) -> bool:
        """通过 quote_ctx 是否有效判断网关存活"""
        if gateway is None:
            return False
        try:
            # 检查行情上下文是否仍在运行
            if hasattr(gateway, 'quote_ctx') and gateway.quote_ctx is not None:
                return True
            return False
        except Exception:
            return False

    def check_us(self) -> bool:
        """检查美股链路"""
        gw = self._get_gateway("US")
        alive = self._is_gateway_alive(gw)
        return alive

    def check_hk(self) -> bool:
        """检查港股链路"""
        gw = self._get_gateway("HK")
        alive = self._is_gateway_alive(gw)
        return alive

    def health(self) -> dict:
        us_alive = self.check_us()
        hk_alive = self.check_hk()
        return {
            "us_md": us_alive,
            "hk_md": hk_alive,
            "us_reconnects": self._reconnect_attempts["us"],
            "hk_reconnects": self._reconnect_attempts["hk"],
            "ts": datetime.now().isoformat(),
        }

    # ──────────────────────────────
    #  自动重连（修复：使用正确的 setting）
    # ──────────────────────────────
    def reconnect_if_needed(self) -> dict:
        results = {"us": False, "hk": False}

        # ---- US 链路 ----
        if not self.check_us():
            if self._reconnect_attempts["us"] < self._max_reconnect:
                self._reconnect_attempts["us"] += 1
                logger.warning(
                    f"[DualLink] ⚠️ US链路断开，尝试重连 "
                    f"({self._reconnect_attempts['us']}/{self._max_reconnect})..."
                )
                try:
                    gw = self._get_gateway("US")
                    if gw and hasattr(gw, "connect"):
                        # 使用保存的初始 setting，确保 TrdEnv 正确
                        setting = self._initial_settings.get("US", {})
                        if not setting:
                            # 如果没有保存，则使用默认值（注意：必须包含 TrdEnv 枚举）
                            setting = {
                                "地址": "127.0.0.1",
                                "端口": 11111,
                                "市场": "US",
                                "环境": TrdEnv.SIMULATE,
                                "密码": "",
                            }
                        gw.connect(setting)
                        time.sleep(2)
                        if self.check_us():
                            logger.info("[DualLink] ✅ US链路重连成功")
                            self._reconnect_attempts["us"] = 0
                            results["us"] = True
                except Exception as e:
                    logger.error(f"[DualLink] US重连失败: {e}")
            else:
                logger.error("[DualLink] ❌ US链路重连次数超限，需人工介入")
        else:
            self._reconnect_attempts["us"] = 0

        # ---- HK 链路 ----
        if not self.check_hk():
            if self._reconnect_attempts["hk"] < self._max_reconnect:
                self._reconnect_attempts["hk"] += 1
                logger.warning(
                    f"[DualLink] ⚠️ HK链路断开，尝试重连 "
                    f"({self._reconnect_attempts['hk']}/{self._max_reconnect})..."
                )
                try:
                    gw = self._get_gateway("HK")
                    if gw and hasattr(gw, "connect"):
                        setting = self._initial_settings.get("HK", {})
                        if not setting:
                            setting = {
                                "地址": "127.0.0.1",
                                "端口": 11111,
                                "市场": "HK",
                                "环境": TrdEnv.SIMULATE,
                                "密码": "",
                            }
                        gw.connect(setting)
                        time.sleep(2)
                        if self.check_hk():
                            logger.info("[DualLink] ✅ HK链路重连成功")
                            self._reconnect_attempts["hk"] = 0
                            results["hk"] = True
                except Exception as e:
                    logger.error(f"[DualLink] HK重连失败: {e}")
            else:
                logger.error("[DualLink] ❌ HK链路重连次数超限，需人工介入")
        else:
            self._reconnect_attempts["hk"] = 0

        return results

    # ──────────────────────────────
    #  持续健康检查循环
    # ──────────────────────────────
    def start(self):
        self._running = True
        logger.info(f"[DualLink] ✅ 双链路健康检查已启动 (间隔 {self._check_interval}s)")

    def stop(self):
        self._running = False
        logger.info("[DualLink] 双链路健康检查已停止")

    def is_running(self) -> bool:
        return self._running

    def run_forever(self):
        self.start()
        logger.info("[DualLink] 🔄 进入持续健康检查循环")
        while self._running:
            try:
                health = self.health()
                us_icon = "✅" if health["us_md"] else "❌"
                hk_icon = "✅" if health["hk_md"] else "❌"
                logger.info(
                    f"[DualLink] US:{us_icon} HK:{hk_icon} "
                    f"@ {health['ts']}"
                )
                if not health["us_md"] or not health["hk_md"]:
                    self.reconnect_if_needed()
            except Exception as e:
                logger.error(f"[DualLink] 循环异常: {e}")
            time.sleep(self._check_interval)
        logger.info("[DualLink] 健康检查循环已退出")