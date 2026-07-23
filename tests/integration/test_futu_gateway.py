# -*- coding: utf-8 -*-
"""FutuGatewayWrapper 集成测试（Mock 版，不依赖真实连接）"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from execution.futu_gateway_wrapper import FutuGatewayWrapper


class TestFutuGatewayWrapper(unittest.TestCase):

    def setUp(self):
        self.main_engine = MagicMock()
        self.wrapper = FutuGatewayWrapper(self.main_engine, setting={
            "host": "127.0.0.1",
            "port": 11111,
            "market": "SIMULATE"
        })

    @patch("execution.futu_gateway_wrapper.FutuGateway")
    def test_connect_success(self, mock_gateway_cls):
        """连接成功"""
        mock_gateway = MagicMock()
        mock_gateway_cls.return_value = mock_gateway

        self.wrapper.connect()
        self.assertTrue(self.wrapper.connected)
        mock_gateway.connect.assert_called_once()

    @patch("execution.futu_gateway_wrapper.FutuGateway")
    def test_connect_failure_retry(self, mock_gateway_cls):
        """连接失败重试"""
        mock_gateway = MagicMock()
        mock_gateway.connect.side_effect = Exception("Connection refused")
        mock_gateway_cls.return_value = mock_gateway

        with self.assertRaises(Exception):
            self.wrapper.connect()
        self.assertFalse(self.wrapper.connected)

    def test_send_order_not_connected(self):
        """未连接时拒绝下单"""
        self.wrapper.connected = False
        with self.assertRaises(RuntimeError):
            self.wrapper.send_order("NVDA.SMART", None, None, 100.0, 1)

    @patch("execution.futu_gateway_wrapper.FutuGateway")
    def test_send_order_success(self, mock_gateway_cls):
        """下单成功"""
        mock_gateway = MagicMock()
        mock_order = MagicMock()
        mock_order.orderid = "TEST-001"
        mock_gateway.send_order.return_value = mock_order
        mock_gateway_cls.return_value = mock_gateway
        self.wrapper.gateway = mock_gateway
        self.wrapper.connected = True

        from vnpy.trader.constant import Direction, Offset
        order_id = self.wrapper.send_order(
            "NVDA.SMART", Direction.LONG, Offset.OPEN, 100.0, 1
        )
        self.assertEqual(order_id, "TEST-001")

    def test_cancel_order(self):
        """撤单"""
        self.wrapper.connected = True
        self.wrapper.gateway = MagicMock()
        self.wrapper.cancel_order("TEST-001")
        self.wrapper.gateway.cancel_order.assert_called_with("TEST-001")


if __name__ == "__main__":
    unittest.main()
