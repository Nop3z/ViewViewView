# OPEN_QUESTIONS / 待澄清与裁定记录

> 规则（ARCHITECTURE §8）：按格式追加，不得阻塞交付自选合理默认；架构师裁定后在此标注 RESOLVED 并同步契约。

- [x] R1 RESOLVED（架构师，2026-10-19）：许可证冲突——GitHub 远端初始 LICENSE 为 MIT，与契约 D3 MulanPSL-2.0 不一致；所有者裁定 **MulanPSL-2.0**。影响：M4 文档工程师替换 LICENSE 文件、pyproject license 表达式与 README 标注；架构契约 §0-D3 已补充裁定记录。
- [x] R2 RESOLVED（架构师，2026-10-19）：schema 键序语义。契约最初要求“键名+键序完全一致”，但 FaultViz 的 `_check_columns` 实际只校验键集与类型（顺序不敏感）；为最大化兼容性，裁定 **键传入顺序不敏感**，CSV 始终按冻结的 schema 列序落盘。契约 §2 已修订，测试 `test_key_order_is_irrelevant_but_csv_order_is_frozen` 覆盖。
- [x] B1 RESOLVED（展示，2026-10-19）：主题仅实现 `dark`（D5）；`theme` trait 与 `data-vvv-theme` 属性已保留，未来可扩展浅色/跟随 JupyterLab 主题，无需改协议。
- [x] B2 RESOLVED（展示，2026-10-19）：上窗分类桶按**首次出现顺序**（dict 插入序）展示，不做额外排序；与 FaultViz `count_by` 的呈现方式一致，避免每次刷新导致行位置跳动。
- [x] R3 RESOLVED（架构师，2026-10-19）：`csvplot` 缺 pygwalker 时的日志文案与 §3.4 的 `plot` 文案区分——“showing the table view instead” 明确告知已降级为双窗表格；行为不变（不抛异常）。
