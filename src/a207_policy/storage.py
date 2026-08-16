"""三包共用的 Tablestore 基础设施（2026-08-15 抽取，消除 3×~250 行复制）。

背景：nutrition / care / clinical-data 三个写包此前各自实现了一整套 Tablestore
DAO（OTSClient 构造、_rev 乐观锁条件更新、损坏 JSON fail-closed 反序列化、
ensure_tablestore_tables 幂等建表……~750 行高度雷同）。SDK 一旦变动（真实踩坑：
SingleColumnCondition vs SingleColumnValueCondition 类名写错致生产 ImportError）
要 3 处同步修，修漏即行为不一致。抽取为本模块，三包 repository 继承复用。

覆盖（与原三包实现逐项对齐）：
- 连接构造：A207_OTS_* 缺参 fail-fast（不静默回退 JSON）；client 注入供测试 Fake
- 惰性建连：延迟 import tablestore SDK（JSON 后端零依赖）
- 行读：_get_row 存储故障 fail-closed 抛 RuntimeError（不静默当"行不存在"）
- 乐观锁：_put_row_conditioned（_rev 条件写）+ _save_row_locked（读-改-写 +
  冲突重试 + _merge_row 合并防 lost update；仅条件检查失败重试，SDK 错误立即抛）
- 覆盖/防撞写：_put_row（IGNORE）/ _put_row_not_exist（EXPECT_NOT_EXIST）
- 全表 GetRange：_range_all(table, pk_cols) 主键列参数化分页
- 幂等建表：ensure_tables(specs)
业务方法（各域 load/save/upsert 语义）与主键构造仍留在各包 repository。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .exceptions import ConflictError

logger = logging.getLogger("a207-policy.storage")

# 列表元素去重键的候选 id 键（care 用 record_id/plan_id/notification_id/id/entry_id）。
# B3-1（2026-08-16，十审）：**删 "date"**——date 不是身份键，任何缺真实 id 键的
# 列表元素会按"天"合并，同日两条无 id 元素被静默并成一条（实测 {food:rice} +
# {food:apple} 同日 → rice 被销毁）。删后无 id 键元素走 :42 JSON 全等兜底
# （_item_key 已有该分支），语义正确：仅内容全等才判重、不同内容各自保留。
_ITEM_ID_KEYS = ("record_id", "plan_id", "notification_id", "id", "entry_id")


def _item_key(item: Any) -> tuple:
    """列表元素去重键：dict 优先取业务 id 键，否则按 JSON 序列化全等。"""
    if isinstance(item, dict):
        for k in _ITEM_ID_KEYS:
            if item.get(k) is not None:
                return ("id", k, item[k])
    return ("json", json.dumps(item, ensure_ascii=False, sort_keys=True))


def _merge_lists(cur: list, new: list) -> list:
    """列表按元素 id 去重合并（new 优先覆盖同 id，追加新元素）。"""
    result = list(cur)
    for item in new:
        key = _item_key(item)
        replaced = False
        for index, existing in enumerate(result):
            if _item_key(existing) == key:
                result[index] = item  # 同 id 以 new 为准（后写者意图）
                replaced = True
                break
        if not replaced:
            result.append(item)
    return result


def _merge_row(current: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """冲突重试合并：以**最新行**为底，new 非 None 字段覆盖；JSON 列表字段去重合并。

    current 为存储层读回的原始属性列（列表字段是 JSON 字符串），new 为本次欲写的
    序列化列。合并后既保留并发写者的新增（列表合并），又体现本次修改（标量覆盖）。

    契约（LOW-7，2026-08-15）：**list 语义列必须序列化为 JSON 数组字符串存储**——
    合并逻辑仅当 cur_value 与 value 都是 str 且都解析为 list 时才走按 id 去重合并；
    其余情况一律普通覆盖。业务层写入列表列时必须用 `json.dumps(list, ensure_ascii=False)`
    而非 Python 原对象，否则乐观锁冲突重试时该列会被整值覆盖（丢并发新增）。
    """
    merged = dict(current)
    for key, value in new.items():
        cur_value = merged.get(key)
        # 两端都是 JSON 数组字符串 → 反序列化按 id 去重合并
        if isinstance(cur_value, str) and isinstance(value, str):
            try:
                cur_list = json.loads(cur_value)
                new_list = json.loads(value)
                if isinstance(cur_list, list) and isinstance(new_list, list):
                    merged[key] = json.dumps(_merge_lists(cur_list, new_list),
                                             ensure_ascii=False)
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
        merged[key] = value
    return merged


class TablestoreBase:
    """Tablestore 基础读写（三包共用；业务层继承后只写域方法）。"""

    OTS_ENDPOINT_ENV = "A207_OTS_ENDPOINT"
    OTS_INSTANCE_ENV = "A207_OTS_INSTANCE_NAME"
    OTS_AK_ID_ENV = "A207_OTS_ACCESS_KEY_ID"
    OTS_AK_SECRET_ENV = "A207_OTS_ACCESS_KEY_SECRET"
    _REV_COL = "_rev"
    _MAX_RETRY = 3

    def __init__(self, client: Any | None = None) -> None:
        """client 仅供测试注入内存 Fake（生产不传，走 A207_OTS_* 环境变量）。"""
        if client is not None:
            self._client = client
            return
        self.endpoint = _os_getenv(self.OTS_ENDPOINT_ENV)
        self.instance = _os_getenv(self.OTS_INSTANCE_ENV)
        self.ak_id = _os_getenv(self.OTS_AK_ID_ENV)
        self.ak_secret = _os_getenv(self.OTS_AK_SECRET_ENV)
        missing = [name for name, val in (
            (self.OTS_ENDPOINT_ENV, self.endpoint),
            (self.OTS_INSTANCE_ENV, self.instance),
            (self.OTS_AK_ID_ENV, self.ak_id),
            (self.OTS_AK_SECRET_ENV, self.ak_secret),
        ) if not val]
        if missing:
            raise RuntimeError(
                f"Tablestore 后端缺少连接参数：{', '.join(missing)}。"
                f"请注入 A207_OTS_* 环境变量（生产默认后端，勿静默回退 JSON）。")
        self._client = None  # 惰性建连

    def _get_client(self):
        if self._client is None:
            import tablestore  # 延迟导入：JSON 后端无需依赖 SDK

            self._client = tablestore.OTSClient(
                self.endpoint, self.ak_id, self.ak_secret, self.instance)
        return self._client

    # ---- 行读写 ----

    def _get_row(self, table: str, pk: list[tuple[str, str]]) -> dict[str, Any] | None:
        try:
            _, row, _ = self._get_client().get_row(table, pk)
        except Exception as exc:
            # fail-closed：存储故障（网络/超时/鉴权）抛 RuntimeError（→ INTERNAL_ERROR），
            # 不得静默当"行不存在"——否则 Tablestore 抖动会被误判为"无数据"（医疗数据
            # 可信度受损）。行不存在（row is None）是 SDK 正常返回，不抛。
            logger.error("Tablestore get_row 失败: table=%s pk=%s exc=%s", table, pk, exc)
            raise RuntimeError(
                f"Tablestore 读取失败（table={table}），详情见服务端日志") from exc
        if row is None:
            return None
        # attribute_columns 为 (name, value, timestamp) 三元组，仅取 name/value
        return {name: value for name, value, _ in row.attribute_columns}

    def _put_row(self, table: str, pk: list[tuple[str, str]],
                 attrs: dict[str, Any]) -> None:
        """幂等覆盖写（RowExistenceExpectation.IGNORE）。"""
        from tablestore import Condition, Row, RowExistenceExpectation

        condition = Condition(RowExistenceExpectation.IGNORE)
        clean = {k: v for k, v in attrs.items() if v is not None}
        row = Row(pk, list(clean.items()))
        self._get_client().put_row(table, row, condition)

    def _put_row_not_exist(self, table: str, pk: list[tuple[str, str]],
                           attrs: dict[str, Any]) -> None:
        """条件写：主键**不得已存在**（EXPECT_NOT_EXIST），已存在抛 OTSClientError。

        B1 修复（2026-08-13）：append_lab_record 不再覆盖写——显式传入的 sample_id
        与既有行撞主键必须报错暴露冲突，绝不静默覆盖（丢数据）。
        """
        from tablestore import Condition, Row, RowExistenceExpectation

        condition = Condition(RowExistenceExpectation.EXPECT_NOT_EXIST)
        clean = {k: v for k, v in attrs.items() if v is not None}
        row = Row(pk, list(clean.items()))
        self._get_client().put_row(table, row, condition)

    def _put_row_conditioned(self, table: str, pk: list[tuple[str, str]],
                             attrs: dict[str, Any], rev: int,
                             expect_exists: bool, *, force: bool = False) -> None:
        """条件写：_rev 必须等于 rev（乐观锁）。条件不满足抛 OTSClientError。

        :param force: True 时跳过 _REV 列条件（仅保留行存在性期望）——S-4 用于
          历史行（缺 _REV_COL）的无条件补列初始化。
        """
        # 🔴 真实踩坑（2026-08-13）：此前 import SingleColumnValueCondition——该符号
        # 在 tablestore 6.x SDK **不存在**（正确类名 SingleColumnCondition），生产切
        # Tablestore 写即 ImportError。已修正并补 Fake 回归，勿再改回。
        from tablestore import (ComparatorType, Condition, Row,
                                RowExistenceExpectation, SingleColumnCondition)

        expectation = (RowExistenceExpectation.EXPECT_EXIST if expect_exists
                       else RowExistenceExpectation.EXPECT_NOT_EXIST)
        col_cond = None
        if expect_exists and not force:
            col_cond = SingleColumnCondition(
                self._REV_COL, ComparatorType.EQUAL, rev)
        condition = Condition(expectation, col_cond)
        clean = {k: v for k, v in attrs.items() if v is not None}
        row = Row(pk, list(clean.items()))
        self._get_client().put_row(table, row, condition)

    def _save_row_locked(self, table: str, pk: list[tuple[str, str]],
                         attrs: dict[str, Any]) -> None:
        """乐观锁写入：读 _rev → 条件写 _rev+1 → 冲突重试。

        S5 修复（2026-08-13）：冲突重试时用 _merge_row **重新读取并合并**最新行与
        本次 attrs（此前整行覆盖旧 attrs → 高并发下后写覆盖先写的部分字段，lost
        update）。列表字段按元素 id 去重合并，标量 new 优先。
        C-B4 修复（2026-08-14）：仅**条件检查失败**（乐观锁冲突）重试——此前把所有
        OTSClientError（鉴权失败/参数非法/表不存在等 SDK 错误）一律当并发冲突重试
        3 次后报"存储并发写冲突"，把配置/环境错误误导成高并发问题。非冲突错误立即抛。
        """
        from tablestore import OTSClientError

        last_err: Exception | None = None
        # B1-2（2026-08-16，十审）：历史行补列不消耗重试预算——此前补列后
        # `continue` 消耗一次循环配额，某行始终缺 _REV_COL 时 3 次重试全耗在
        # 补列上，最终抛 ConflictError 且 last_err=None（误导性"并发写冲突"）。
        # 补列成功即进入正常 CAS；用 while 循环保证"至少一次 CAS 尝试"——
        # 补列本身幂等（force=True），重复补列无害。
        attempts = 0
        while attempts < self._MAX_RETRY:
            current = self._get_row(table, pk)
            rev = int(current.get(self._REV_COL, 0)) if current else 0
            next_attrs = dict(attrs)
            if current:
                next_attrs = _merge_row(current, next_attrs)  # S5：合并并发修改
                # S-4（2026-08-15）：**历史行无 _REV_COL**（2026-08-13 乐观锁上线前
                # 落库）——条件 `_REV_COL == 0` 对缺列恒不满足（列不存在 ≠ 等于 0），
                # 首次更新 3 次重试后 INTERNAL_ERROR 且无自动修复路径。先**无条件
                # 补列初始化**（force：仅行存在性期望，幂等无害），下一轮走正常
                # 条件更新。B1-2：补列不占 attempts（不消耗 CAS 重试预算）。
                if self._REV_COL not in current:
                    legacy = dict(next_attrs)
                    legacy[self._REV_COL] = 0
                    self._put_row_conditioned(
                        table, pk, legacy, rev=0, expect_exists=True, force=True)
                    continue
            next_attrs[self._REV_COL] = rev + 1
            attempts += 1  # 仅正常 CAS 尝试消耗预算（补列不占）
            try:
                self._put_row_conditioned(
                    table, pk, next_attrs, rev, expect_exists=current is not None)
                return
            except OTSClientError as exc:
                code = str(getattr(exc, "code", "") or "")
                msg = str(getattr(exc, "message", "") or "")
                is_conflict = ("ConditionCheck" in code or "ConditionCheck" in msg
                               or "not match" in msg.lower())
                if not is_conflict:
                    raise  # C-B4：SDK 错误（鉴权/表不存在等）立即抛，定位真实根因
                last_err = exc  # 条件不满足 → 并发写冲突，重试
        # 九审（2026-08-16）：乐观锁"重试仍失败"是**业务写冲突**（并发写者竞争），
        # 非服务端内部故障——此前 RuntimeError 经 translate_error 归 INTERNAL_ERROR，
        # 编排层无法区分"服务端坏了" vs "业务冲突（可重试/人工合并）"。
        # 统一抛 ConflictError → translate_error 显式映射 CONFLICT 信封（三包共享）。
        raise ConflictError(
            f"存储并发写冲突（{table} pk={pk}），重试 {self._MAX_RETRY} 次仍失败，"
            f"拒绝静默覆盖: {last_err}")

    # ---- 全表 GetRange ----

    def _range_all(self, table: str, pk_cols: list[str],
                   prefix: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """全表/按主键前缀 GetRange（主键升序，每页 200 行翻到底）。

        :param pk_cols: 表主键列名（有序），如 ["patient_id"] / ["patient_id", "sample_id"]。
        :param prefix: 主键前缀固定值（LOW-4，2026-08-15）——如 {"patient_id": "P001"}
          时只扫该患者行（复合主键前缀范围），不做全表 GetRange。缺省全表。
        返回 [{pk_dict, attrs_dict}]。
        """
        from tablestore import INF_MAX, INF_MIN

        pre = prefix or {}
        start = [(col, pre.get(col, INF_MIN)) for col in pk_cols]
        end = [(col, pre.get(col, INF_MAX)) for col in pk_cols]
        rows: list[dict[str, Any]] = []
        next_start = start
        while next_start is not None:
            consumed, next_start, row_list, _ = self._get_client().get_range(
                table, "FORWARD", next_start, end, limit=200)
            for row in row_list:
                pk_dict = {}
                for k, v in row.primary_key:
                    pk_dict[k] = v.decode() if isinstance(v, bytes) else v
                attrs_dict = {name: value for name, value, _ in row.attribute_columns}
                rows.append({"pk": pk_dict, "attrs": attrs_dict})
        return rows

    @staticmethod
    def _json_col(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    # ---- 幂等建表 ----

    def ensure_tables(self, specs: dict[str, list[tuple[str, str]]]) -> None:
        """创建/校验表（幂等，仅建缺失表）。

        :param specs: {table_name: [(pk_col, "STRING"), ...]}——主键 schema 单一事实源
        留在各包 repository（表名/主键是各域数据契约，不并入本模块）。
        """
        from tablestore import (CapacityUnit,
                                ReservedThroughput, TableMeta, TableOptions)

        # B5-1（2026-08-16，十审）：复用 _get_client()——此前这里重建第二个
        # OTSClient（endpoint/ak/sk/instance 从环境变量取），忽略测试注入的
        # Fake 客户端：测试中 ensure_tables 打真实 Tablestore（无参数即 fail、
        # 有参数则真建表），与注入的 Fake 完全脱节。统一走 _get_client()
        # （含 _client 注入点），幂等建表对 Fake 与真实客户端同路径。
        client = self._get_client()
        existing = set(client.list_table())

        for table_name, pk_schema in specs.items():
            if table_name in existing:
                continue
            meta = TableMeta(table_name, pk_schema)
            options = TableOptions(time_to_live=-1, max_version=1)
            throughput = ReservedThroughput(capacity_unit=CapacityUnit(0, 0))
            client.create_table(meta, options, throughput)
            logger.info("[ensure] 已创建表 %s", table_name)
        logger.info("[ensure] Tablestore 表就绪：%s", sorted(existing | set(specs)))


def _os_getenv(name: str) -> str:
    import os

    return os.environ.get(name, "").strip()


# 生产护栏（2026-08-15）：json 后端是本地开发模式（无乐观锁/持久化保证，重启即丢），
# 若有人显式设 A207_STORAGE_BACKEND=json 却部署到生产，系统会接受并失去数据保证。
# 除非显式 A207_ACCEPT_DEV_STORAGE=1，否则一律拒绝（fail-closed，堵人为误操作）。
ACCEPT_DEV_STORAGE_ENV = "A207_ACCEPT_DEV_STORAGE"


def ensure_json_backend_allowed() -> None:
    """json 后端护栏：未显式确认（A207_ACCEPT_DEV_STORAGE=1）即抛 RuntimeError。"""
    if _os_getenv(ACCEPT_DEV_STORAGE_ENV) == "1":
        return
    raise RuntimeError(
        "A207_STORAGE_BACKEND=json 是本地开发模式（无乐观锁/持久化保证，重启即丢）。"
        f"确认在非生产环境使用请显式设置 {ACCEPT_DEV_STORAGE_ENV}=1，否则拒绝启动"
        "（fail-closed：防止 json 后端误部署到生产）。")
