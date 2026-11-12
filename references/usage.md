# DWG Reader 使用参考

## 命令

```bash
dwg-reader convert input.dwg output-dir [--backend auto|oda|libredwg]
dwg-reader text input.dxf [-o output.txt] [--font gbcbig] [--format text|json]
                [--with-metadata] [--expand-blocks] [--max-depth 8]
dwg-reader inspect input.dxf [--format text|json] [--expand-blocks] [-o report.json]
dwg-reader font gbcbig BAA3 C4CF
dwg-reader doctor [--format text|json]
dwg-reader openscad input.dxf output.stl [--height 10] [--layer CUT]
dwg-reader-mcp
```

历史兼容入口仍位于 `scripts/`，新流程优先使用统一 CLI。

## 文本解码

- `\U+XXXX` 或八位变体：Unicode 码点。
- `\M+5XXXX`：SHX shape number；按 STYLE 对应字体的 GBK、CP949、Big5 或
  Shift-JIS 编码还原。
- `%%C`、`%%D`、`%%P`：直径、度、正负符号。

自动字体匹配失败时，用 `--font <路径或内置字体名>` 显式指定。`fonts/index.json`
记录内置字体类型、编码和 shape 数量。

## 依赖与排错

- `text` 和 `font` 只需要 Python 标准库。
- `inspect` 需要 `python -m pip install -e '.[dxf]'`。
- 块展开依赖 `ezdxf`，包含 `ATTRIB` 和经过插入变换后的块内 TEXT/MTEXT；循环块
  会被跳过，递归深度默认限制为 8。
- `convert` 默认查找 macOS ODA File Converter；其他路径用 `--oda` 指定。
- DXF 很大时使用 `text`；不要使用旧的 `extract_texts_lowmem.py`，它会将整个文件
  载入内存。
- 输出乱码时，先检查实体样式与 STYLE 表 code 4 指定的 bigfont，再尝试显式字体。
- `openscad` 是显式写操作，只写指定输出文件。设置 `OPENSCAD_PATH` 可覆盖自动探测。
- `DWG_READER_ODA_PATH`、`DWG_READER_LIBREDWG_PATH`、`OPENCAD_PATH` 可覆盖后端路径。

集成来源、能力边界与许可证选择见 [integrations.md](integrations.md)。
