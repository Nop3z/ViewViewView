# 验证报告 · v0.1.0

日期：2026-10-19　环境：Windows / Python 3.12.0 / anywidget 0.11.0 / pytest 8.3.3 / ruff（py39 target）

## M1 核心验收（`python scripts/smoke_core.py`）

15/15 通过，覆盖：

- 1000 次 `update()` 耗时 **2.6 ms**（契约上限 1 s）
- CSV 回读与写入序列逐字节一致（header、列序、值）
- counts / `rows_written` / `csv_path`（绝对路径）
- schema 语义矩阵：空 kwargs、bool、None、类型漂移、多余键、缺失键 → `False`；键序不敏感 → `True`
- 构造参数 ValueError 文案与契约逐字一致
- 既有文件 header 不匹配、末行未完成 → ValueError
- 空文件可正常续写且只写一次 header
- CSV I/O 失败后锁定实例，后续 update 抛 RuntimeError
- 尾窗读取（O(limit)）与畸形记录定位
- 8 线程 × 200 行并发，1600 行无丢失
- `start_view_service` 任意参数 no-op、不抛异常
- `live_rows=0` CSV-only 模式
- `columns=` 预声明字段（映射/序列两种形式）与类型校验
- 默认文件名 = 控件创建起始时间，文件在首条记录时创建

## M3 测试套件（`python -m pytest -q`）

**61 passed**，覆盖 `tests/test_core.py`、`tests/test_compat.py`、`tests/test_panel.py`、`tests/test_stress.py`。

压测实测：

| 指标 | 契约目标 | 实测 |
| --- | --- | --- |
| 10 万行总耗时 | < 90 s | **0.23 s** |
| `update()` p95 | ≤ 50 µs（CI 护栏 100 µs） | **2.2 µs** |
| 5 万行 + `live_rows=1000` 内存 | < 40 MB | **0.3 MB**（tracemalloc current/peak） |

## M4 打包与文档

- `python -m build` 成功产出 `viewviewview-0.1.0.tar.gz` 与 `viewviewview-0.1.0-py3-none-any.whl`
- wheel 内容核验：`core.py/compat.py/panel.py/static/index.mjs/static/styles.css/LICENSE` 全部包含
- `ruff check src tests scripts`：**All checks passed**
- README（安装、快速开始、自定义字段、兼容矩阵、差异声明、开发命令）
- `example.ipynb`：实时双窗、自定义列、CSV-only 回看、多线程四个场景；JSON 结构校验通过
- LICENSE 替换为 MulanPSL-2.0 中英双语全文；pyproject 元数据（license/keywords/classifiers/urls）就绪

## 视觉验收

`dev/preview.html` 经 Edge 无头渲染截图（`dev/preview.png`）：深色双窗、上窗 `state/count` 分类计数（绿色 `#19ff25`）、下窗白色字段文字明细、底部“已记录 942 行 / 当前窗口 34 行 · live”状态，符合 §4.4 规格。预声明 `columns=` 时可在无数据阶段渲染空表头骨架。

列宽拖拽通过 `dev/preview.html?resize` 在真实浏览器中合成一次拖拽后截图验证：`state` 列收窄、`delay` 列同步加宽，表头分隔线可见；宽度在重绘后保留。`dev/preview.html` 由 `scripts/build_preview.py` 从 `index.mjs`/`styles.css` 生成，避免预览页与源码漂移。

## 真实核心演示（`python scripts/build_demo.py --rows 5000`）

用真实 `ViewWidget`（`columns=` 预声明、CSV 落盘、环形窗口）跑 5000 次模拟注入，捕获实际发给前端的 payload 后渲染截图：

| 指标 | 实测 |
| --- | --- |
| 注入耗时 | 0.019 s |
| CSV | 95 135 字节 / 5 000 行 |
| 分类计数 | PASS 4 461 / FAIL 436 / TIMEOUT 103 |
| 明细窗口 | 1 000 行（`live_rows=1000`） |

效果图：`dev/demo-empty.png`（空表骨架）、`dev/demo-live.png`（真实运行）、`dev/demo-resize.png`（列宽拖拽）、`dev/demo-gallery.png`（三图总览）。

## JupyterLab 实机验证（2026-10-19）

在 JupyterLab 4.6.4 + ipywidgets 8.1.9 + anywidget 0.11.0 中实际执行 `dev/jupyter_demo.ipynb`（800 次注入），双窗、绿色计数与 `delay`/`pulse` 列（`#19ff25`，`errcode` 保持白色）、列宽拖拽均正常，截图见 `dev/jupyter-live.png`。此轮验证发现并修复了两个**仅在 Jupyter 环境暴露**的问题：

1. **anywidget 默认导出契约**：必须是 `export default { render }`；写成 `export default function render(...)` 时 anywidget 会以无参调用，前端报 `Cannot destructure property 'model' of 'undefined'`，widget 完全不渲染。已在 `index.mjs` 修正并加测试断言。
2. **样式挂载位置**：anywidget 在 JupyterLab 中可能把 `_css` 注入普通 DOM（而非 shadow root），`:host` 上的 CSS 变量不生效，导致白底黑字。配色 tokens 已从 `:host` 移到 `.vvv-root`，两种挂载方式下都保持深色 + 白字 + 绿色计数（可自定义）。

复现：`python scripts/capture_jupyter.py`（需要本地 JupyterLab 与 Edge）。

## 已知限制（均为契约内有意设计）

1. 多个 `show()` 面板共享“最后一次 show 的分类列与窗口深度”；FaultViz 亦以最后一次显示为准。
2. counts 仅对 `str` 列维护（分类列语义）；数值列不参与分类计数。
3. 明细窗默认深度 200 行（`limit`/`live_rows`/`full` 可覆盖）——为渲染稳定性有意与 FaultViz 的“默认全量”不同。
4. `columns=` 预声明的 schema 在实例生命周期内固定；需要增删列时请为新一轮实验创建新的 `ViewWidget`。
5. `plot()/csvplot()` 的交互分析依赖可选 `pygwalker`/`pandas`，缺失时降级为双窗表格。
6. 尚未发布 PyPI；安装走 GitHub。
7. 多次 `show()`（或重跑 cell 不清输出）会累积多个面板：每个面板自带 shadow DOM 与 comm 通道，且旧面板仍接收推送。长实验建议复用同一个面板，或对旧 widget 调用 `close()` 后再新建；详见下方“S1 内存优化”。

## S1 前端内存优化（2026-11-01）

**背景**：长时间高强度实验（约一周）后浏览器标签页内存持续增长，实测达 1.4 GB。专项排查结论：Python 侧无泄露（tracemalloc 稳态 0.26 MB）；主体来自前端两条路径——(a) 每帧 `clearChildren` 后全量重建 6048 个 shadow DOM 节点（depth=1000/5 列，20 帧/秒 ≈ 1.2 亿节点/小时的分配压力，V8 堆持续扩张）；(b) 累积的多个 `show()` 面板（僵尸视图）各自重建并接收推送。

**改动**（`index.mjs`）：`renderPayload` 增量渲染——schema/状态列/高亮列集合不变时保留表骨架，仅原地更新 counts tbody、对齐 detail tbody 行数并逐格写变化的 `textContent`、更新 footer 文本；任一身份键变化则回退原全量重建路径。视图卸载（anywidget teardown）时释放 mounted DOM 引用。DOM 契约不变：单元格 class 与最终文本与全量重建完全一致。

**实测**（`python scripts/mem_baseline.py 3000 50 1000 5`，Edge 无头 + `--expose-gc`）：

| 指标 | 改动前 | 改动后 | 判定 |
| --- | --- | --- | --- |
| shadow 节点数（全程） | 每帧重建 6048 个 | **恒定 6048（drift 0）** | PASS |
| 每帧渲染耗时（mean） | 15.9 ms | **9.0 ms** | PASS（预算 13 ms） |
| 强制 GC 后堆漂移 | 持续爬升锯齿 | **-0.3 MB（回落基线）** | PASS（预算 5 MB） |

新增回归：`tests/test_stress.py::test_push_loop_memory_bounded`（持续 update+推送 2 s，Python traced < 5 MB、线程无累积）；套件 67 项全绿，ruff 通过。`dev/preview.html`、`dev/demo.html` 由脚本重新生成并保持视觉一致。
7. Python 3.9 环境下 pip 会自动选择兼容的 anywidget 0.9.x（0.11+ 要求 Python ≥3.10）；本地实测环境为 Python 3.12 + anywidget 0.11，Python 3.9–3.12 的兼容性由 CI 矩阵持续验证。

## 后续建议

- GitHub Actions 已配置（Linux/Windows × Python 3.9–3.12）
- 下一版：面板级独立窗口深度、浅色主题、PyPI 发布与 FaultViz 实例的 CSV 双向互通回归
