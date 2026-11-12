#!/usr/bin/env python3
"""稳健低内存提取 DXF 全部文字：逐行解析 DXF tag 对，支持 MTEXT 多段。

DXF 文本格式：每两行一个 tag，code 行/value 行。本脚本逐行扫描，
跟踪实体起点（code 0），收集 TEXT/MTEXT 的 code 1 和 code 3 内容。

支持：
- 大字体（BigFont）解码：\\U+XXXX / \\M+5XXXX / %%
- 块参照（INSERT）内的文字展开（含缩放、旋转、镜像变换）
- 块属性（ATTRIB）世界坐标提取
- 尺寸标注（DIMENSION）文字提取（覆盖文字或测量值）
"""
import json
import math
import re
import sys


# DXF $DWGCODEPAGE 名 → Python 编解码器
CODEPAGE_MAP = {
    "ANSI_1252": "cp1252", "ANSI_1250": "cp1250", "ANSI_1251": "cp1251",
    "ANSI_1253": "cp1253", "ANSI_1254": "cp1254", "ANSI_1255": "cp1255",
    "ANSI_1256": "cp1256", "ANSI_1257": "cp1257", "ANSI_1258": "cp1258",
    "ANSI_936": "gbk", "ANSI_950": "big5", "ANSI_949": "cp949",
    "ANSI_932": "cp932", "ANSI_874": "cp874",
    "DOS_437": "cp437", "DOS_850": "cp850", "DOS_852": "cp852",
    "DOS_866": "cp866", "DOS_936": "gbk",
    "MACINTOSH": "mac_roman", "UTF-8": "utf-8", "UTF8": "utf-8",
    "UTF-16": "utf-16",
}


def _detect_codepage(raw):
    """从 DXF 头部 $DWGCODEPAGE 推断 Python 编解码器，缺省 utf-8。"""
    m = re.search(rb"\$DWGCODEPAGE\b.*?\n\s*3\s*\n([^\n]+)", raw, re.S)
    if m:
        name = m.group(1).decode("ascii", "ignore").strip().upper()
        if name in CODEPAGE_MAP:
            return CODEPAGE_MAP[name]
    return "utf-8"


def _read_text(path):
    """按 DXF 实际编码读取为文本，避免非 ASCII 字符被误码。

    策略：优先尝试 UTF-8（现代 CAD/DWG→DXF 普遍采用，且自校验）；
    若整文件非合法 UTF-8（如按 GBK/Big5 存储高字节），再回退到
    $DWGCODEPAGE 指示的编解码器。这样既能正确读取 UTF-8 文字，
    也能兼容标了错误 codepage 却存了 UTF-8 字节的文件。
    """
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(_detect_codepage(raw), errors="replace")


def resolve_stack(s):
    """解析 MTEXT 堆叠文字 \\S<top>^<bottom> / \\S<top>#<bottom> / \\S<top>/<bottom>。

    AutoCAD 用 \\S 表示堆叠（分数/公差）：'^' 与 '/' 渲染为 top/bottom，'#' 渲染为 top#bottom。
    堆叠内容的顶/底部分若包在 {...} 内（常含 \\H 高度控制码），会先剥离控制码再保留真实文字，
    使输出可直接阅读。本函数供 MTEXT 解码统一调用。
    """
    def clean_part(p):
        # 去掉内部字体/高度控制码（如 \H0.7x;），保留括号内的真实文字
        p = re.sub(r"\\[A-Za-z][^;\\\s{]*;?", "", p)
        p = p.replace("{", "").replace("}", "")
        return p.strip()

    def repl(m):
        top = clean_part(m.group(1))
        sep = m.group(2)
        bottom = clean_part(m.group(3))
        if sep == "#":
            return f"{top}#{bottom}"
        return f"{top}/{bottom}"

    # 堆叠内容以空白或下一个控制码结束
    return re.sub(r"\\S(.+?)([\^#/])(.+?)(?=\s|\\|$)", repl, s)


def clean_mtext_format(s):
    """解码后的 MTEXT 清理：解析堆叠文字并去除 CAD 格式控制码，保留可读文本。"""
    s = resolve_stack(s)
    s = s.replace("\\P", "\n").replace("\\p", "\n")
    s = re.sub(r"\{[^}]*\}", "", s)
    s = re.sub(r"\\[A-Za-z][^;\\\n]*;?", "", s)
    s = s.replace("~", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def decode_bigfont(s, font=None):
    """解码 CAD 大字体编码。

    格式:
    - \\U+XXXX     : Unicode 码点 (int(s,16))
    - \\M+5XXXX    : BigFont/GBK 编码, 后 4 位为 GBK 两字节码
      (\\M+5A1DD + 数字800 这种相邻文本不会被误吞)

    font: ShxBigFont 实例或 None。提供时优先用字体映射表验证/解码。
    """
    def repl_u(m):
        try:
            return chr(int(m.group(1), 16))
        except Exception:
            return m.group(0)

    def repl_m(m):
        hexstr = m.group(1)
        try:
            code = int(hexstr, 16)
        except ValueError:
            return m.group(0)
        # 优先用字体映射表
        if font is not None:
            ch = font.get_char(code)
            if ch is not None:
                return ch
        # 回退: 多编码尝试（gbk → big5/cp950 → cp949 → shift_jis）
        b = bytes([(code >> 8) & 0xFF, code & 0xFF])
        for codec in ("gbk", "big5", "cp949", "shift_jis"):
            try:
                s2 = b.decode(codec, errors="strict")
                if s2 and "\ufffd" not in s2:
                    return s2
            except Exception:
                continue
        return m.group(0)

    s = re.sub(r"\\U\+([0-9A-Fa-f]{4})", repl_u, s)
    s = re.sub(r"\\M\+5([0-9A-Fa-f]{4})", repl_m, s)
    s = resolve_stack(s)
    s = decode_percent_escapes(s)
    return s


# AutoCAD %% 转义序列 → Unicode（key 为 %% 后的单个字母，小写）
PERCENT_ESCAPES = {
    "c": "Ø",
    "d": "°",
    "p": "±",
    "%": "%",
    "u": "",
    "o": "",
}


def decode_percent_escapes(s):
    """解码 AutoCAD `%%` 转义序列（%%C/%%D/%%P/%%%/%%数字，大小写不敏感）。"""
    def repl(m):
        code = m.group(1).lower()
        if code in PERCENT_ESCAPES:
            return PERCENT_ESCAPES[code]
        try:
            n = int(m.group(1), 10)
            if n < 256:
                return chr(n)
        except ValueError:
            pass
        return m.group(0)

    return re.sub(r"%%([A-Za-z%]|\d{1,3})", repl, s)


def load_font(font_arg):
    """加载字体，返回 ShxBigFont 或 None。"""
    if not font_arg:
        return None
    try:
        from shxfont import ShxBigFont
        return ShxBigFont(font_arg)
    except Exception as e:
        print(f"字体加载失败({font_arg}): {e}", file=sys.stderr)
        return None


def extract_font_map(src):
    """从 DXF 的 STYLE 表提取字体映射。

    返回 {style_name: bigfont_name}，供解码时按文本样式选用字体。
    大字体文件名常含 `\\M+` 编码，需一并解码。
    """
    font_map = {}
    try:
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        # 解析 STYLE 表条目: code 2=样式名, code 3=字体, code 4=大字体
        entries = re.findall(
            r"\n  0\nSTYLE\n(.*?)(?=\n  0\n)", data, re.S
        )
        for block in entries:
            name = re.search(r"\n  2\n([^\n]+)", block)
            bigfont = re.search(r"\n  4\n([^\n]+)", block)
            if name and bigfont:
                style = name.group(1)
                bf = bigfont.group(1)
                if bf and bf.lower() != "txt.shx":
                    # 解码样式名/字体名中的 \M+ 编码
                    style = decode_bigfont(style)
                    bf = decode_bigfont(bf)
                    font_map[style] = bf
    except Exception as e:
        print(f"STYLE 表解析失败: {e}", file=sys.stderr)
    return font_map


def resolve_font(font_path, src):
    """解析字体：优先 --font 参数，否则从 STYLE 表自动匹配。"""
    if font_path:
        return load_font(font_path), None
    # 自动模式：解析 STYLE 表，收集所需大字体
    font_map = extract_font_map(src)
    if not font_map:
        return None, None
    from shxfont import get_font
    cache = {}
    for style, bf in font_map.items():
        if bf:
            cache[style] = get_font(bf)
    return None, cache


def font_for_style(style, font, font_cache):
    if font is not None:
        return font
    if font_cache and style in font_cache:
        return font_cache[style]
    return None


def main():
    if len(sys.argv) < 2:
        print("用法: extract_texts_stream.py <文件.dxf> [输出.txt] [--font 字体.shx]")
        sys.exit(1)
    src = sys.argv[1]
    out = None
    font_path = None
    table_mode = False
    by_layer = False
    layer_filter = None
    layer_alias = None
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--font" and i + 1 < len(sys.argv):
            font_path = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--table":
            table_mode = True
            i += 1
        elif sys.argv[i] == "--by-layer":
            by_layer = True
            i += 1
        elif sys.argv[i] == "--layer-filter" and i + 1 < len(sys.argv):
            layer_filter = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--layer-alias" and i + 1 < len(sys.argv):
            layer_alias = sys.argv[i + 1]
            i += 2
        else:
            out = sys.argv[i]
            i += 1

    # 解析字体：--font 显式指定 或 从 STYLE 表自动
    font = None
    font_cache = None  # {style_name: ShxBigFont}
    if font_path:
        font = load_font(font_path)
    else:
        font, font_cache = resolve_font(None, src)
        if font_cache:
            n = sum(1 for v in font_cache.values() if v is not None)
            print(f"自动匹配 {n} 个大字体", file=sys.stderr)

    # 图层过滤器：--layer-filter 接受逗号分隔的关键词/正则片段；
    # --layer-alias 是 JSON {"用户词": "实际图层片段"}，命中用户词时把其值并入过滤。
    layer_filter_re = None
    if layer_filter:
        patterns = [p.strip() for p in layer_filter.split(",") if p.strip()]
        if layer_alias:
            try:
                with open(layer_alias, "r", encoding="utf-8") as f:
                    alias = json.load(f)
                for k in patterns:
                    if k in alias:
                        patterns.append(str(alias[k]))
            except Exception as e:
                sys.stderr.write(f"[warn] 别名表读取失败: {e}\n")
        if patterns:
            layer_filter_re = re.compile("|".join(patterns), re.IGNORECASE)
            print(f"图层过滤器: {patterns}", file=sys.stderr)

    texts = []
    section = None  # 'BLOCKS' / 'ENTITIES' / None
    blocks = {}  # name -> {'base':(bx,by), 'layer':'', 'items':[{'type','x','y','style','seg':[],'layer':''}]}
    used_blocks = set()  # 被 INSERT 引用过的块名（已展开，无需再次直接提取）

    cur_entity = None
    cur_content = []
    cur_x = cur_y = 0.0
    cur_point_done = False  # 是否已捕获本实体的第一对 (10,20) 插入点
    cur_layer = ""
    cur_style = ""
    cur_type = ""
    cur_code = None
    cur_value = None

    # INSERT 参数累积
    cur_insert = None  # {'name','bx','by','sx','sy','rot'}

    # 块定义上下文
    cur_block = None  # {'name','base':(bx,by),'items':[]}
    cur_block_item = None  # 块内子实体临时收集

    def _layer_ok(layer):
        """图层过滤器：layer_filter_re 为空表示不过滤（全部保留）。"""
        if layer_filter_re is None:
            return True
        if not layer:
            return False
        return bool(layer_filter_re.search(layer))

    def flush_entity():
        nonlocal cur_entity, cur_content, cur_x, cur_y, cur_layer, cur_type, cur_style
        nonlocal cur_point_done, cur_insert, cur_dim_meas
        if cur_entity == "MTEXT":
            ef = font_for_style(cur_style, font, font_cache)
            content = clean_mtext_format(decode_bigfont("".join(cur_content), ef))
            if content.strip() and content.strip() != "<>" and _layer_ok(cur_layer):
                texts.append((cur_y, cur_x, cur_type, cur_layer, content))
        elif cur_entity in ("TEXT", "ATTRIB"):
            ef = font_for_style(cur_style, font, font_cache)
            content = decode_bigfont("".join(cur_content), ef)
            if content.strip() and content.strip() != "<>" and _layer_ok(cur_layer):
                texts.append((cur_y, cur_x, cur_type, cur_layer, content))
        elif cur_entity == "DIMENSION":
            # 尺寸文字：优先覆盖文字(code 1)，否则用测量值
            ef = font_for_style(cur_style, font, font_cache)
            dim_text = "".join(cur_content).strip()
            if not dim_text or dim_text == "<>":
                if cur_dim_meas is not None:
                    dim_text = f"{cur_dim_meas:.2f}"
                else:
                    dim_text = ""  # 无覆盖文字且无测量值（如 <> 占位符）→ 跳过
            content = decode_bigfont(dim_text, ef) if dim_text else ""
            if content.strip() and content.strip() != "<>" and _layer_ok(cur_layer):
                texts.append((cur_y, cur_x, cur_type, cur_layer, content))
        elif cur_entity == "INSERT" and cur_insert is not None:
            name = cur_insert["name"]
            used_blocks.add(name)  # 已被展开，文件末尾不再重复直接提取
            bx, by = cur_insert["bx"], cur_insert["by"]
            sx, sy, rot = cur_insert["sx"], cur_insert["sy"], cur_insert["rot"]
            block = blocks.get(name)
            if block:
                bbx, bby = block["base"]
                cos_a, sin_a = math.cos(rot), math.sin(rot)
                for it in block["items"]:
                    lx, ly = it["x"], it["y"]
                    rx = (lx - bbx) * sx
                    ry = (ly - bby) * sy
                    wx = bx + (rx * cos_a - ry * sin_a)
                    wy = by + (rx * sin_a + ry * cos_a)
                    ef = font_for_style(it["style"], font, font_cache)
                    raw = "".join(it["seg"])
                    if it["type"] == "MTEXT":
                        c = clean_mtext_format(decode_bigfont(raw, ef))
                    else:
                        c = decode_bigfont(raw, ef)
                    # 块内文字继承块的图层；按过滤器判断是否保留
                    it_layer = it.get("layer", "") or block.get("layer", "")
                    if c.strip() and _layer_ok(it_layer):
                        texts.append((wy, wx, it["type"], it_layer, c))
        # 块定义内子实体先暂存到 cur_block_item（在 BLOCKS 段处理flush时收集）
        cur_entity = None
        cur_content = []
        cur_type = ""
        cur_x = cur_y = 0.0
        cur_point_done = False
        cur_layer = ""
        cur_style = ""
        cur_insert = None
        cur_dim_meas = None

    def flush_block_item():
        nonlocal cur_block_item
        if cur_block_item is not None and cur_block is not None:
            if cur_block_item["type"] in ("TEXT", "MTEXT"):
                cur_block["items"].append(cur_block_item)
        cur_block_item = None

    cur_dim_meas = None

    text = _read_text(src)
    for line in text.splitlines():
        s = line.strip()
        if cur_code is None:
            if not s:
                # 还没读到 code 行，空行忽略（文件头/段间的空白）
                continue
            cur_code = s
            continue
        cur_value = s  # 空字符串也是合法的 DXF 值（如 code 1 后的空行）
        try:
            code = int(cur_code)
        except ValueError:
            cur_code = None
            continue
            cur_code = s
            continue
        cur_value = s
        try:
            code = int(cur_code)
        except ValueError:
            cur_code = None
            continue

        # 跟踪 section 边界
        if code == 0:
            # 结束上一个实体 / 块项
            if section == "BLOCKS" and cur_block is not None:
                # 处于 BLOCK 定义内部：按块子项处理
                if cur_entity == "ENDBLK":
                    blocks[cur_block["name"]] = cur_block
                    cur_block = None
                flush_block_item()
            else:
                # 普通实体（ENTITIES 段，或 BLOCKS 段内的游离实体）
                flush_entity()
            cur_entity = cur_value
            cur_type = cur_value
            cur_point_done = False
            # 段切换：仅在标准 SECTION 实体闭合后生效，忽略 BLOCKS 段内
            # 游离的 SECTION（损坏文件常见），避免 section 状态被错误重置
            if cur_entity == "SECTION":
                if cur_value == "BLOCKS":
                    section = "BLOCKS"
                    cur_block = None
                elif cur_value == "ENTITIES":
                    section = "ENTITIES"
                else:
                    section = None
            elif cur_value == "ENDSEC":
                section = None
            elif cur_value == "BLOCK" and section == "BLOCKS":
                cur_block = {"name": "", "base": (0.0, 0.0), "layer": "", "items": []}
                cur_block_item = None
            elif cur_value == "INSERT" and section == "ENTITIES":
                cur_insert = {"name": "", "bx": 0.0, "by": 0.0,
                              "sx": 1.0, "sy": 1.0, "rot": 0.0}
            elif cur_value in ("TEXT", "MTEXT", "ATTRIB", "DIMENSION"):
                cur_content = []
                cur_x = cur_y = 0.0
                cur_point_done = False
                cur_layer = ""
                cur_style = ""
                cur_dim_meas = None
                # 仅当处于 BLOCK 定义内部（cur_block 未结束）才收集为块子项；
                # BLOCKS 段内的游离实体（无 BLOCK 包裹，损坏/不规范文件常见）
                # 以及 ENTITIES 段实体，均按普通实体提取（flush_entity 路径）
                if section == "BLOCKS" and cur_block is not None:
                    cur_block_item = {"type": cur_value, "x": 0.0, "y": 0.0,
                                      "style": "", "seg": [], "layer": cur_block.get("layer", "")}
        elif code == 2:
            if cur_entity == "SECTION":
                # 标准 DXF：SECTION 由 0/SECTION + 2/<段名> 定义
                section = cur_value if cur_value in ("BLOCKS", "ENTITIES") else None
            elif section == "BLOCKS" and cur_block is not None and cur_entity == "BLOCK":
                cur_block["name"] = cur_value
            elif cur_entity == "INSERT" and cur_insert is not None:
                cur_insert["name"] = cur_value
            elif section == "BLOCKS" and cur_block_item is not None:
                cur_block_item["style"] = cur_value
        elif code == 8:
            if section == "BLOCKS" and cur_block_item is not None:
                pass
            elif section == "BLOCKS" and cur_entity == "BLOCK" and cur_block is not None:
                # 记录块定义自身图层，其后块内文字继承该图层
                cur_block["layer"] = cur_value
                if cur_block_item is not None:
                    cur_block_item["layer"] = cur_value
            else:
                cur_layer = cur_value
        elif code == 7:
            if cur_entity in ("TEXT", "MTEXT", "ATTRIB"):
                cur_style = cur_value
            elif section == "BLOCKS" and cur_block_item is not None:
                cur_block_item["style"] = cur_value
        elif code == 10 and not cur_point_done:
            try:
                v = float(cur_value)
            except ValueError:
                v = 0.0
            if cur_entity == "INSERT" and cur_insert is not None:
                cur_insert["bx"] = v
            elif section == "BLOCKS" and cur_block_item is not None:
                cur_block_item["x"] = v
            elif cur_entity == "BLOCK" and cur_block is not None:
                cur_block["base"] = (v, cur_block["base"][1])
            else:
                cur_x = v
        elif code == 20 and not cur_point_done:
            try:
                v = float(cur_value)
            except ValueError:
                v = 0.0
            if cur_entity == "INSERT" and cur_insert is not None:
                cur_insert["by"] = v
            elif section == "BLOCKS" and cur_block_item is not None:
                cur_block_item["y"] = v
            elif cur_entity == "BLOCK" and cur_block is not None:
                cur_block["base"] = (cur_block["base"][0], v)
            else:
                cur_y = v
            cur_point_done = True
        elif code == 41 and cur_entity == "INSERT" and cur_insert is not None:
            try:
                cur_insert["sx"] = float(cur_value)
            except ValueError:
                pass
        elif code == 42 and cur_entity == "INSERT" and cur_insert is not None:
            try:
                cur_insert["sy"] = float(cur_value)
            except ValueError:
                pass
        elif code == 50 and cur_entity == "INSERT" and cur_insert is not None:
            try:
                cur_insert["rot"] = math.radians(float(cur_value))
            except ValueError:
                pass
        elif code == 1:
            if section == "BLOCKS" and cur_block_item is not None:
                # 块定义内的子实体文字（优先于普通实体分支）
                cur_block_item["seg"] = [cur_value]
            elif cur_entity == "MTEXT":
                # MTEXT 首段(code 1)；后续段用 code 3 追加
                if not cur_content:
                    cur_content = [cur_value]
                else:
                    cur_content.append(cur_value)
            elif cur_entity in ("TEXT", "ATTRIB", "DIMENSION"):
                cur_content = [cur_value]
        elif code == 3:
            if section == "BLOCKS" and cur_block_item is not None and cur_block_item["seg"]:
                cur_block_item["seg"].append(cur_value)
            elif cur_entity in ("TEXT", "MTEXT") and cur_content:
                cur_content.append(cur_value)
        elif code == 42 and cur_entity == "DIMENSION":
            # DIMENSION 测量值（默认文字）
            try:
                cur_dim_meas = float(cur_value)
            except ValueError:
                pass
        cur_code = None

    # 文件末尾收尾
    if section == "BLOCKS":
        flush_block_item()
        if cur_block is not None:
            blocks[cur_block["name"]] = cur_block
    else:
        flush_entity()

    # 未被任何 INSERT 引用（或文件无 ENTITIES 段）的块，其定义内的文字直接
    # 按块内坐标提取，保证图纸主体文字（常置于 *Model_Space 或匿名块中）不丢失
    for name, blk in blocks.items():
        if name in used_blocks:
            continue
        bbx, bby = blk["base"]
        blk_layer = blk.get("layer", "")
        for it in blk["items"]:
            if it["type"] not in ("TEXT", "MTEXT"):
                continue
            it_layer = it.get("layer", "") or blk_layer
            if not _layer_ok(it_layer):
                continue
            ef = font_for_style(it["style"], font, font_cache)
            raw = "".join(it["seg"])
            c = (clean_mtext_format(decode_bigfont(raw, ef))
                 if it["type"] == "MTEXT" else decode_bigfont(raw, ef))
            if c.strip():
                texts.append((it["y"], it["x"], it["type"], it_layer, c))

    texts = reorder_by_columns(texts)
    print(f"共提取 {len(texts)} 条文字", file=sys.stderr)

    if table_mode:
        md = to_markdown_table(texts)
        if md:
            if out:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(md + "\n")
                print(f"已写入: {out}", file=sys.stderr)
            else:
                print(md)
            sys.exit(0)

    if by_layer:
        groups = {}
        for t in texts:
            groups.setdefault(t[3] or "0", []).append(t[4])
        lines = []
        for layer in sorted(groups):
            lines.append(f"\n### 图层: {layer}")
            lines.extend(f"  {c}" for c in groups[layer])
        blob = "\n".join(lines).strip("\n")
    else:
        blob = "\n".join(f"[{t[4]}]" for t in texts)

    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(blob + "\n")
        print(f"已写入: {out}", file=sys.stderr)
    else:
        print(blob)


def _cluster_index(vals, gap_factor=6.0, abs_tol=None):
    """对升序排列的数值做 1D 间隙聚类，返回 {值: 簇索引}。

    相邻值间距大于阈值时断开成新簇。阈值来源：
    - abs_tol 给定时直接用（适合表格列/行的精确对齐检测）；
    - 否则用 中位间距 * gap_factor（适合多栏文档的"大间隙"分栏）。
    """
    import statistics
    if not vals:
        return {}
    if len(vals) < 2:
        return {vals[0]: 0}
    if abs_tol is not None:
        thresh = abs_tol
    else:
        diffs = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
        med = statistics.median(diffs)
        thresh = max(med * gap_factor, 1e-9)
    idx = {}
    c = 0
    prev = vals[0]
    idx[prev] = 0
    for v in vals[1:]:
        if v - prev > thresh:
            c += 1
        idx[v] = c
        prev = v
    return idx


def reorder_by_columns(texts):
    """几何感知排序：先按栏（X 聚类）分组，栏内按 Y 降序、X 升序。

    比纯 (-Y, X) 更适合多栏图纸：避免左栏与右栏同 Y 行交错。
    texts 元素结构为 (y, x, type, layer, content)，返回新列表。
    """
    if len(texts) < 2:
        return texts
    xs = sorted(set(t[1] for t in texts))
    xidx = _cluster_index(xs, gap_factor=6.0)
    cols = {}
    for t in texts:
        cols.setdefault(xidx.get(t[1], 0), []).append(t)
    out = []
    for c in sorted(cols):
        out.extend(sorted(cols[c], key=lambda t: (-t[0], t[1])))
    return out


def to_markdown_table(texts, col_gap_factor=6.0, row_gap_factor=6.0):
    """尽力将文字按几何位置还原为 Markdown 表格。

    行：Y 相近聚类；列：X 相近聚类；顶部行在上。返回 Markdown 字符串；
    若无法形成 >=2 行 且 >=2 列，返回空串（调用方应回退平铺输出）。
    """
    if len(texts) < 4:
        return ""
    ys = sorted(set(t[0] for t in texts))
    xs = sorted(set(t[1] for t in texts))
    # 表格的列/行靠精确对齐，用绝对容差（默认 1.0 图纸单位）而非相对间距
    yidx = _cluster_index(ys, abs_tol=1.0)
    xidx = _cluster_index(xs, abs_tol=1.0)
    nrows = max(yidx.values()) + 1
    ncols = max(xidx.values()) + 1
    if nrows < 2 or ncols < 2:
        return ""
    maxr = nrows - 1
    grid = [["" for _ in range(ncols)] for _ in range(nrows)]
    for t in texts:
        r = maxr - yidx[t[0]]  # 顶部(y最大)→行0
        c = xidx[t[1]]
        cell = t[-1]  # 兼容 3 元组(y,x,content) 与 5 元组(...,content)
        grid[r][c] = (grid[r][c] + " " + cell).strip() if grid[r][c] else cell
    header = "| " + " | ".join(" " for _ in range(ncols)) + " |"
    sep = "|" + "|".join("---" for _ in range(ncols)) + "|"
    body = ["| " + " | ".join(grid[r]) + " |" for r in range(nrows)]
    return "\n".join([header, sep] + body)


if __name__ == "__main__":
    main()
