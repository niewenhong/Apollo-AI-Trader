# utils/telegram_bot.py
import requests
import threading
import time

class TelegramBot:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._running = False

    def send_message(self, text):
        url = f"{self.base_url}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'Markdown'
        }
        try:
            resp = requests.post(url, json=payload, timeout=5)
            resp.raise_for_status()
        except Exception as e:
            print(f"[Telegram] 发送失败: {e}")

    def start_listening(self, strategy_engine=None):
        """启动轮询监听（可选）"""
        def _poll():
            offset = 0
            while self._running:
                try:
                    url = f"{self.base_url}/getUpdates?offset={offset}&timeout=30"
                    resp = requests.get(url, timeout=35)
                    data = resp.json()
                    if data.get('result'):
                        for update in data['result']:
                            offset = update['update_id'] + 1
                            if 'message' in update and 'text' in update['message']:
                                text = update['message']['text']
                                chat_id = update['message']['chat']['id']
                                if text.startswith('/status') and strategy_engine:
                                    reply = strategy_engine.get_status_summary()
                                    self.send_message(reply)
                except Exception as e:
                    print(f"[Telegram] 轮询异常: {e}")
                time.sleep(1)

        self._running = True
        thread = threading.Thread(target=_poll, daemon=True)
        thread.start()

    def stop(self):
        self._running = False