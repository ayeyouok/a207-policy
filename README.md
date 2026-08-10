# a207-policy

A207 统一身份注入 / 权限矩阵 / 状态路径策略包（Plan A 单一事实源）。

13 个 MCP 统一依赖本包，取代原先散落在 M1/M2/M4/M11/M13 的多份权限副本。

## 用法

```python
from a207_policy import get_caller, enforce_read, enforce_write, LIS_READ_FULL

def get_labs(patient_id: str) -> dict:
    caller = get_caller()                 # 来自 env A207_CALLER，模型碰不到
    enforce_read("a207-lis-mcp")          # 确定性拒绝越权（fail-closed）
    ...
```

- 身份注入：`A207_CALLER` 环境变量由部署配置写入，模型无法自证（修复 P0-1）。
- 测试：`from a207_policy import set_caller; set_caller("doctor_assistant")`。
- 状态路径：`from a207_policy import resolve_state_path; resolve_state_path("followup_store.json")`
  落到 `A207_DATA_DIR` 或系统临时目录，避免只读安装目录崩溃（修复 P1-3）。
