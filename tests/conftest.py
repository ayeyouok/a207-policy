"""pytest 收集（十二审，2026-08-17，CI 假绿阻断修复）。

@check 用例由 check 装饰器直接以 test_ 前缀注册到调用模块全局，pytest 天然收集；
本 conftest 无需钩子。保留空文件以固定 rootdir 语义。
"""
