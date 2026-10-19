# ViewViewView

**A lightweight, freeze-proof fault-injection results viewer — 轻量、稳定、永不冻结的故障注入结果可视化工具。**

零 JVM、双窗实时看板：上窗按分类统计命中次数，下窗滚动显示最新一次注入的完整参数（`delay`、`pulse`、`reply`、`errcode`……字段完全自定义）。API 与 CSV 格式与 [FaultViz](https://gitee.com/osr-tech/faultviz) 兼容，已有实验脚本改一行 import 即可切换。

[![License: MulanPSL-2.0](https://img.shields.io/badge/license-MulanPSL--2.0-blue.svg)](http://license.coscl.org.cn/MulanPSL2)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![JupyterLab](https://img.shields.io/badge/JupyterLab-anywidget-orange.svg)](https://anywidget.dev/)

---

## 为什么需要 ViewViewView

FaultViz 依赖 Deephaven 嵌入式 JVM（默认 2 GB 堆）和 JupyterLab 前端组件，一次注入要穿过 Python → Deephaven → JupyterLab → 浏览器四层。长时间、大批量扫描时，任一层阻塞都会让整个界面冻结，甚至拖慢实验主循环。

ViewViewView 用纯 Python 重写了同一条数据通路，只保留必要环节：

| | FaultViz | ViewViewView |
| --- | --- | --- |
| 运行时 | 内嵌 JVM + Deephaven Server + JDK | **纯 Python，零 JVM/JDK** |
| 内存 | 默认 JVM 堆 2 GB | **环形窗口限界；ring=1000 时实测约 0.3 MB** |
| 实时更新 | 每次 update 跨多层 RPC 推送 | 本地内存快照 + 50 ms 合帧推送，**不阻塞实验循环** |
| 数据安全 | UI 故障可能影响记录 | **CSV-first**：先落盘后渲染；UI 失败自动降级为仅记录 |
| 吞吐 | 大批量后渲染易卡顿 | 实测 **10 万行 0.23 s，p95 2.2 µs/行**（Python 3.12 / Windows） |
| 依赖 | deephaven-server、pygwalker、pandas、jupyterlab | **核心仅 anywidget**（plot 分析功能才需要 pygwalker/pandas） |

## 功能特性

- **上下双窗**
  - 上窗 · 分类统计：按 `state`（或任意字符串列）实时计数，如 `PASS: 8932 / FAIL: 41 / TIMEOUT: 12`
  - 下窗 · 结果明细：最新注入置顶，逐行展示完整参数
- **字段完全自定义**：实例化时用 `columns=` 显式声明列名与类型（启动即显示空表头）；也可省略该参数，由第一条 `update(...)` 的关键字自动定义 schema
- **CSV 自动落地**：默认以控件创建的起始时间命名（`YYYYMMDD-HHMMSS.csv`），也支持手动命名（含子目录）
- **CSV-first 持久化**：每条结果先写入 CSV，再更新界面；与 FaultViz 的 CSV 格式兼容
- **受限内存**：`live_rows` 环形窗口，任意注入量级内存恒定
- **深色主题**：黑色背景、白色字段文字、荧光绿计数，对齐 FaultViz 观感，适合暗室长时间盯屏
- **线程安全**：`update/flush/close` 可在工作线程并发调用
- **零前端依赖**：anywidget 单文件 ESM + Shadow DOM，不拉取 CDN、不需要 npm 构建

## 安装

尚未发布到 PyPI，直接从 GitHub 安装：

```bash
pip install "git+https://github.com/Nop3z/ViewViewView.git"
```

或本地开发安装（可编辑模式）：

```bash
git clone https://github.com/Nop3z/ViewViewView.git
cd ViewViewView
pip install -e .
```

需要 Python ≥ 3.9。`plot()/csvplot()` 的数据探索可视化需要可选依赖：

```bash
pip install -e ".[plot]"
```

## 快速开始

在你的 JupyterLab notebook 中：

```python
import viewviewview as faultviz   # 与 FaultViz 用法一致
import random

faultviz.start_view_service()      # 兼容保留：本实现无需 JVM，调用即 no-op

# 实例化时自定义字段：列名 + 类型。也可省略 columns，由第一条 update 自动定义。
vt = faultviz.ViewWidget(
    live_rows=1000,
    columns={"state": str, "delay": int, "pulse": int, "reply": str, "errcode": int},
)
vt.show()       # 立即显示自定义字段的空表；注入开始后自动实时刷新

def attack():
    delay = random.randint(100, 2000)
    pulse = random.randint(10, 50)
    reply = random.choice(["ok", "no-reply", "short"])
    errcode = 0 if reply == "ok" else random.randint(1, 8)
    state = "PASS" if reply == "ok" else "FAIL"
    vt.update(state=state, delay=delay, pulse=pulse, reply=reply, errcode=errcode)

for _ in range(500):
    attack()

print(vt.csv_path)   # 默认文件名为控件创建时刻，如 .../20261019-213005.csv
```

`update()` 的关键字就是明细窗的列。想记录什么就传什么——只要值是精确的 `int` / `float` / `str`：

```python
vt.update(state="PASS", delay=1200, pulse=30, reply="ok", errcode=0,
          voltage_mv=1350, temperature=26.5, note="glitch window #3")
```

字段也可以在实例化时一次性声明（推荐，类型在启动时即受控）：

```python
# 映射形式：列名 -> 类型（int / float / str），顺序即列序
vt = faultviz.ViewWidget(columns={"state": str, "delay": int, "errcode": int})

# 序列形式：只声明列名，类型由第一条记录推断
vt = faultviz.ViewWidget(columns=["state", "delay", "pulse", "reply", "errcode"])
```

> 注意：`bool`、numpy 标量等不会被隐式转换（与 FaultViz 的类型规则一致）。需要时请先 `.item()` 或显式转换。

### CSV-only 记录（不打开界面）

```python
vt = faultviz.ViewWidget(live_rows=0)   # 只记录，不渲染
...                                      # 正常 update
print(vt.csv_path)                       # 当前 CSV 的绝对路径
vt.close()                               # 或用 with 语句
```

### 回看历史结果

```python
faultviz.csvview("20261019-213005.csv")                            # 双窗查看
faultviz.csvview(path, limit=100)                                  # 只看最新 100 条（O(limit) 内存）
faultviz.csvplot(path)                                             # pygwalker 交互分析（可选依赖）
```

## 与 FaultViz 的兼容性

现有 FaultViz 脚本只需替换 import：

```diff
- import faultviz
+ import viewviewview as faultviz
```

| FaultViz API | ViewViewView | 说明 |
| --- | --- | --- |
| `start_view_service(port=12345, max_heap="2g")` | ✅ 同签名 | no-op，无需 JVM；参数被接受且不会抛异常 |
| `ViewWidget(csv_path=None, display_limit=None, flush_every=1, live_rows=None, fsync=False)` | ✅ 同签名 + 可选 `columns=` | 语义一致，含合法性校验错误文案；`columns` 为向后兼容的扩展参数 |
| `widget.update(**kwargs) -> bool` | ✅ | 首行定 schema；类型/键不匹配返回 `False`；CSV 写失败抛异常并锁死实例 |
| `widget.show(state="state", limit=None, full=False)` | ✅ | 上窗分类计数 + 下窗明细；`limit=0` 仅显示统计 |
| `widget.plot(state="state", limit=None, full=False)` | ✅ | pygwalker 可用则交互绘图，否则降级 CSV 查看 |
| `widget.flush()` / `close()` / `csv_path` / `rows_written` | ✅ | `close()` 幂等，支持上下文管理器 |
| `csvview(csv, state="state", limit=None, full=False)` | ✅ | 无需先启动服务 |
| `csvplot(name, limit=None, full=False)` | ✅ | 同上 |
| CSV 文件格式 | ✅ 兼容 | UTF-8、`csv` 标准方言、首行列名，可被 FaultViz 读取 |

**有意差异**（均为轻量化目标服务）：

- 不再需要 JDK/`JAVA_HOME`，也没有 Deephaven 服务与端口占用；
- 默认记录文件名为控件创建的起始时间（`YYYYMMDD-HHMMSS.csv`），并新增可选的 `columns=` 参数用于实例化时显式声明字段；
- 明细窗默认只推送最近 200 行（`live_rows`/`limit`/`full` 可覆盖），避免大表渲染拖慢界面；
- `plot`/`csvplot` 的 pygwalker 为可选依赖。

## 数据与文件

CSV 采用与 FaultViz 相同的格式：UTF-8 编码、逗号分隔、引号转义、首行为列名，按 schema 列序追加：

```csv
state,delay,pulse,reply,errcode
PASS,176,12,ok,0
FAIL,191,13,no-reply,3
```

- 默认以**控件创建时刻**（实验开始时间）命名：`<YYYYMMDD-HHMMSS>.csv`，如 `20261019-213005.csv`；同名冲突自动追加 `-1`
- 手动命名：`ViewWidget(csv_path="runs/my_experiment.csv")`，不存在的子目录会自动创建
- 追加既有文件前会校验 header 与文件完整性（末行未写完会拒绝写入，防止数据损坏）
- `flush_every` 控制落盘频率；`fsync=True` 可进一步要求每条刷盘（代价是更高 I/O 延迟）

## 开发与验证

```bash
python -m pytest -q                     # 47 项契约/一致性/压测测试
python scripts/smoke_core.py            # 13 项 M1 验收自检
```

无需 Jupyter 即可预览界面：用浏览器直接打开 [`dev/preview.html`](dev/preview.html)（深色双窗样张，含模拟载荷与“模拟推送”按钮）。

设计契约与决策记录见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)；待澄清事项见 [`docs/OPEN_QUESTIONS.md`](docs/OPEN_QUESTIONS.md)。

## 许可证

本项目采用 **木兰宽松许可证，第 2 版（MulanPSL-2.0）**，全文见 [LICENSE](LICENSE)。
Copyright (c) 2026 Nop.

ViewViewView 是 FaultViz 的独立兼容重实现（clean-room）：仅对齐其公开 API 签名与 CSV 数据格式，代码全部原创，不包含 FaultViz 源代码，与 Open Security Research（OSR）无隶属关系。FaultViz 采用 MulanPSL-2.0 发布，详见其[项目主页](https://gitee.com/osr-tech/faultviz)。
