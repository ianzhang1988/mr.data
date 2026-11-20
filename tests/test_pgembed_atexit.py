"""F-24：PgEmbedManager 的 atexit 注册只发生一次的回归测试。

注册逻辑在实例创建处（__new__）而非 start()：stop→start 反复不再重复注册；
同 data_dir 的缓存实例也不会重复注册。
"""

from mr_data.db import pgembed_manager


def test_atexit_registered_once_per_instance(monkeypatch):
    calls = []
    monkeypatch.setattr(pgembed_manager.atexit, "register", calls.append)

    a = pgembed_manager.PgEmbedManager(data_dir="/tmp/pgembed-atexit-test-a")
    b = pgembed_manager.PgEmbedManager(data_dir="/tmp/pgembed-atexit-test-b")
    assert len(calls) == 2  # 每实例恰好注册一次

    # 同 data_dir 返回缓存实例，不重复注册
    again = pgembed_manager.PgEmbedManager(data_dir="/tmp/pgembed-atexit-test-a")
    assert again is a
    assert len(calls) == 2
