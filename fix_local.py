"""
fix_local.py - 一键修复本地代码，对齐 GitHub main 分支
运行方式: python fix_local.py
"""
import urllib.request
import os
import sys

BASE = "https://raw.githubusercontent.com/niewenhong/Apollo-AI-Trader/main"

FILES = [
    # 远程路径 -> 本地路径
    ("ai/param_advisor.py", "ai/param_advisor.py"),
    ("ai/llm_client.py", "ai/llm_client.py"),
    ("backtest/optimizer.py", "backtest/optimizer.py"),
    ("main.py", "main.py"),
    ("monitoring/telegram_notifier.py", "monitoring/telegram_notifier.py"),
    ("ai/report_generator.py", "ai/report_generator.py"),
    ("core/strategy_engine.py", "core/strategy_engine.py"),
]

def download(remote, local):
    url = f"{BASE}/{remote}"
    try:
        urllib.request.urlretrieve(url, local)
        print(f"  ✅ {remote} -> {local}")
        return True
    except Exception as e:
        print(f"  ❌ {remote} 下载失败: {e}")
        return False

def main():
    print("=" * 56)
    print(" Apollo AI Trader v2.6.0 - 本地修复脚本")
    print("=" * 56)
    print(f"工作目录: {os.getcwd()}")
    print()

    # 确认在正确目录
    if not os.path.exists("main.py"):
        print("❌ 当前目录没有 main.py，请先 cd 到 D:\\Apollo-AI-Trader-v2.6.0")
        sys.exit(1)

    print("开始从 GitHub 拉取最新文件...")
    print()

    success = 0
    for remote, local in FILES:
        if download(remote, local):
            success += 1

    print()
    print(f"完成: {success}/{len(FILES)} 个文件")
    print()

    # 验证关键签名
    print("验证关键导入...")
    try:
        # 测试 optimizer
        exec(open("backtest/optimizer.py").read().replace("if __name__", "#if __name__"))
        print("  ✅ backtest/optimizer.py 可正常导入")
    except Exception as e:
        print(f"  ⚠️ backtest/optimizer.py: {e}")

    try:
        from ai.param_advisor import ParamAdvisor
        import inspect
        sig = inspect.signature(ParamAdvisor.__init__)
        print(f"  ✅ ParamAdvisor.__init__{sig}")
    except Exception as e:
        print(f"  ⚠️ ParamAdvisor: {e}")

    try:
        from ai.llm_client import LLMClient
        import inspect
        sig = inspect.signature(LLMClient.__init__)
        print(f"  ✅ LLMClient.__init__{sig}")
    except Exception as e:
        print(f"  ⚠️ LLMClient: {e}")

    print()
    print("修复完成！现在运行: python main.py")

if __name__ == "__main__":
    main()
