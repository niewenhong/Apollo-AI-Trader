"""
monitoring/webhook_server.py - v2.6.0
Webhook服务器：接收外部信号、远程控制指令
支持：接收交易信号、参数调整、系统状态查询
"""
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from typing import Optional
import threading


class WebhookHandler(BaseHTTPRequestHandler):
    """Webhook请求处理器"""

    def __init__(self, *args, callback=None, **kwargs):
        self.callback = callback
        super().__init__(*args, **kwargs)

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode('utf-8'))
        except:
            data = {"raw": body.decode('utf-8')}

        # 调用回调函数
        if self.callback:
            response = self.callback(data)
        else:
            response = {"status": "received"}

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode('utf-8'))

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        response = {
            "service": "Apollo Webhook",
            "version": "2.6.0",
            "time": datetime.now().isoformat(),
            "params": params
        }
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode('utf-8'))

    def log_message(self, format, *args):
        pass  # 静默日志


class WebhookServer:
    """Webhook服务器管理"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8888):
        self.host = host
        self.port = port
        self.server = None
        self._thread = None
        self._handlers = []

    def register_handler(self, handler_func):
        """注册回调函数"""
        self._handlers.append(handler_func)

    def _handle_request(self, data: dict) -> dict:
        """处理传入请求"""
        results = []
        for handler in self._handlers:
            try:
                result = handler(data)
                results.append(result)
            except Exception as e:
                results.append({"error": str(e)})
        return {"results": results}

    def start(self):
        """启动服务器（非阻塞）"""
        def make_handler(*args):
            return WebhookHandler(*args, callback=self._handle_request)

        self.server = HTTPServer((self.host, self.port), make_handler)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        print(f"[Webhook] 服务器已启动: http://{self.host}:{self.port}")

    def stop(self):
        if self.server:
            self.server.shutdown()
            print("[Webhook] 服务器已停止")