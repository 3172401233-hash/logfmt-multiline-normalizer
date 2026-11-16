# logfmt-multiline-normalizer
Python 混合日志规范化库，处理 logfmt、多行堆栈、截断段和字段漂移

## 用法

```python
from log_normalizer import StreamNormalizer

norm = StreamNormalizer(max_stack_lines=1000)  # 可选：限制单条记录的堆栈行数
for chunk in collector_chunks:                 # chunk 为 str 或 bytes，按固定字节块到达
    for record in norm.feed(chunk):            # 返回本次完成的 LogRecord 列表
        sink.write(record.to_logfmt())
for record in norm.close():                    # 冲刷不完整尾部、待定缓冲和当前记录
    sink.write(record.to_logfmt())
```

无第三方依赖，要求 Python 3.8+。运行测试：`python -m unittest`。

## 记录判定规则

- 行首匹配 ISO 8601 时间戳（`2026-11-16T10:00:00.123+08:00` 等）→ 新记录。
- 行首无缩进且首个 token 为合法 `key=` 形式 → 视为缺时间戳的新记录（`timestamp=None`）。
- 其余行（缩进的堆栈帧、裸文本等）→ 续行，合并进当前记录的 `stack`。
- 块边界：未遇到 `\n` 的不完整尾部保存在内部进位缓冲，与下一个块拼接后再解析；
  `close()` 时若仍有残留尾部，照常解析并将记录标记 `truncated=True`。

## 字段命名规范

归一化后的 `LogRecord` 顶层字段固定为：

| 字段 | 来源 key（按优先级） | 说明 |
| --- | --- | --- |
| `timestamp` | 行首时间戳 | 原样保留字符串；缺失为 `None` |
| `level` | `level` / `lvl` / `severity` | 归一化为大写标准级别 |
| `trace_id` | `trace_id` / `traceid` / `trace-id` / `trace` | 缺失为 `None` |
| `msg` | `msg` / `message` | 缺失为空串 |
| `fields` | 其余所有 key | 保持出现顺序 |
| `stack` | 续行 | 按行保存，不去缩进 |

其他约定：

- **level 归一化**：`warn/warning→WARN`、`err/error→ERROR`、`crit/critical→CRITICAL`、
  `fatal/panic→FATAL` 等（见 `LEVEL_ALIASES`）。无法识别的级别归一为 `UNKNOWN`，
  原始值保留在 `fields["level_raw"]`，不丢弃。
- **重复 key**：同名 key 的值合并为列表，按出现顺序保存（`tag=a tag=b` → `{"tag": ["a", "b"]}`）。
- **转义**：引号值内支持 `\"`、`\\`、`\n`、`\t`、`\r`；未知转义保留反斜杠原样。
- **乱序字段**：`msg`、`level`、`trace_id` 与普通字段可按任意顺序出现，解析结果一致。
- **标志位**：`origin`（`record`/`pending`）、`truncated`（尾部被截断）、
  `stack_truncated`（堆栈超过 `max_stack_lines` 被截断）。

## 待定记录（pending）释放规则

无法确认归属的续行（在任何记录头出现之前到达的堆栈/裸文本行）**不立即丢弃**，
进入待定缓冲，按以下规则释放：

1. **遇到新记录头时释放**：新的记录头证明待定行不可能归属于未来的记录，
   待定缓冲立即打包为一条 `origin="pending"` 的记录输出（首行作为 `msg`，
   其余行进入 `stack`，`level=UNKNOWN`、`timestamp=None`），先于新记录返回。
2. **`close()` 时释放**：流结束时仍无归属的待定行同样打包为一条 pending 记录输出。
3. **不合并、不回溯**：待定行永远不会被并入之后到达的记录——归属无法确认时
   保持独立，避免误合并。
