"""可写状态路径解析（A3 核心）。

旧方案：状态文件写在 os.path.dirname(__file__)（即 site-packages 安装目录），
容器/云沙箱里通常是只读的 → 写入抛异常（架构复盘 P1-3，与 M2 数据缺失败同源）。

新方案：统一经本函数解析可写路径：
- 若设环境变量 A207_DATA_DIR，落到该目录；
- 否则落到系统临时目录下的 a207_state/（总是可写）。
各包把原先写死的 STORE 常量改成 `resolve_state_path("xxx_store.json")` 即可。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

ENV_KEY = "A207_DATA_DIR"


def resolve_state_path(filename: str, *, base: str | None = None) -> Path:
    """返回可写的 filename 绝对路径，并确保父目录存在。

    :param filename: 状态文件名，如 "followup_store.json"
    :param base: 测试可传入临时基目录覆盖环境变量

    B3（2026-08-15）：filename 必须为**纯文件名**（不含路径分隔/../穿越）——此前仅
    靠"调用点传常量"的隐式约定，未来任一调用点拼接用户输入（如 patient_id）即可
    把状态文件写到任意目录（越权写/覆盖）。fail-closed：含路径成分即拒绝。
    """
    if not isinstance(filename, str) or not filename:
        raise ValueError("状态文件名不能为空")
    name = filename.replace("\\", "/")
    if "/" in name or ".." in name:
        raise ValueError(
            f"状态文件名必须是纯文件名（不含目录/../），收到：{filename!r}")
    if base:
        root = Path(base)
    else:
        override = os.environ.get(ENV_KEY)
        if override:
            root = Path(override)
        else:
            # 未设环境变量：落到系统临时目录，保证可写（避免只读安装目录崩溃）
            root = Path(tempfile.gettempdir()) / "a207_state"
    # B2-1（2026-08-16，十审）+ 2026-08-22 修正（off-by-one）：基准目录与**包目录本身**
    # 比对——此前用 `parent.parent` 取到的是 `src`（包目录的**上一级**），会误把
    # `src/data` 等合法工程子目录判成"安装目录"而误拒；同时又没精确钉住真正的包目录
    # `a207_policy`。现改为比对 `__file__` 所在包目录（.../a207_policy），用 is_relative_to
    # 仅拦截"包目录及其子目录"（写进包=污染+重启丢数据），而放行 `src` 下的合法工程
    # 子目录（如 src/data、/tmp、A207_DATA_DIR 指向的任意非包路径）。
    _installed = Path(__file__).resolve().parent  # a207_policy 包目录本身（非 src 父级）
    _root_resolved = root.resolve()
    if _root_resolved == _installed or _root_resolved.is_relative_to(_installed):
        raise ValueError(
            f"状态目录 {root} 指向包安装目录（{_installed}）——状态文件会写进"
            "安装目录（只读/重启丢数据），请改配 A207_DATA_DIR 到可写数据目录")
    root.mkdir(parents=True, exist_ok=True)
    return root / filename


def atomic_write_json(path, data, *, encoding: str = "utf-8") -> None:
    """原子写 JSON：先写同目录临时文件，再 os.replace 换入，避免半写截断。

    OD-014（P2-3）：各包原 `open(path,"w") + json.dump` 直接覆盖写，进程在写一半时
    被 kill / 磁盘满会留下截断文件，下次 _load_store 静默读成空壳（丢数据）。
    改为：写 `<path>.<rand>.tmp` → flush+fsync → os.replace 原子替换（POSIX 与
    Windows 均保证同卷 replace 原子性）。调用方无感知，返回值 None。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # S-20260822：tempfile.mkstemp 在 POSIX 创建 0600 临时文件，os.replace 后目标
        # 文件权限被锁为 0600（仅属主可读写）。多容器部署（魔搭各 MCP 独立容器、共享
        # A207_DATA_DIR 卷）下，guardian_tokens.json / food_diary 等状态文件会被其他 UID
        # 的容器读权限拒绝（跨容器读共享状态失败，典型的如 P2 读 P1 签发的 guardian token）。
        # 修复：目标已存在则继承其原权限，新建则按标准 0644（与 umask 协同），避免
        # 共享卷读被锁死。
        if target.exists():
            # 2026-08-22（Claim 2A 修复）：st_mode 含文件类型位（S_IFREG 等），os.chmod
            # 只认权限位；OverlayFS/部分网络卷传完整 st_mode 会触发 EINVAL 致权限继承
            # 静默失败。用 stat.S_IMODE 过滤为纯权限位（0o7777）。
            try:
                os.chmod(tmp_name, stat.S_IMODE(target.stat().st_mode))
            except OSError:
                pass
        else:
            # 2026-08-22（Claim 2B 修复）：新建文件尊重宿主机 umask 安全基线（umask
            # 027/077 时不应暴力开 others 读）。标准 umask 022 → 0o666&~022 = 0o644，
            # 与跨容器共享卷可读需求一致；更严 umask 收紧为运维显式安全选择。
            # Windows 无 os.umask（NotImplemented/AttributeError）→ 退化为 0o644。
            mode = 0o644
            try:
                um = os.umask(0)
                os.umask(um)
                mode = 0o666 & ~um
            except (OSError, AttributeError, NotImplementedError):
                pass
            try:
                os.chmod(tmp_name, mode)
            except OSError:
                pass
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
