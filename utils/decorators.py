# -*- coding: utf-8 -*-
"""装饰器：重试、超时、熔断、日志"""
import time
import functools
import logging

logger = logging.getLogger("utils.decorators")

def retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """失败自动重试装饰器"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            current_delay = delay
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    logger.warning(f"[{func.__name__}] 第{attempt}次失败: {e}")
                    if attempt >= max_attempts:
                        logger.error(f"[{func.__name__}] 已达最大重试次数 {max_attempts}")
                        raise
                    time.sleep(current_delay)
                    current_delay *= backoff
        return wrapper
    return decorator

def timeout(seconds: float):
    """超时装饰器"""
    import signal
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            def handler(signum, frame):
                raise TimeoutError(f"[{func.__name__}] 超时 {seconds}s")
            old_handler = signal.signal(signal.SIGALRM, handler)
            signal.alarm(int(seconds))
            try:
                result = func(*args, **kwargs)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
            return result
        return wrapper
    return decorator

def circuit_breaker(failure_threshold: int = 5, reset_timeout: float = 60.0):
    """熔断装饰器：连续失败 N 次后熔断一段时间"""
    def decorator(func):
        func._failures = 0
        func._last_failure_time = 0.0
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            now = time.time()
            if func._failures >= failure_threshold:
                if now - func._last_failure_time < reset_timeout:
                    raise RuntimeError(f"[{func.__name__}] 熔断中，剩余 {reset_timeout - (now - func._last_failure_time):.0f}s")
                else:
                    func._failures = 0  # 重置
            try:
                result = func(*args, **kwargs)
                func._failures = 0  # 成功清零
                return result
            except Exception:
                func._failures += 1
                func._last_failure_time = now
                raise
        return wrapper
    return decorator

def log_execution(level=logging.INFO):
    """记录函数执行日志"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            logger.log(level, f"[{func.__name__}] 开始执行")
            start = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = time.time() - start
                logger.log(level, f"[{func.__name__}] 完成，耗时 {elapsed:.3f}s")
                return result
            except Exception as e:
                elapsed = time.time() - start
                logger.error(f"[{func.__name__}] 异常，耗时 {elapsed:.3f}s: {e}")
                raise
        return wrapper
    return decorator
