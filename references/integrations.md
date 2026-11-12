# CAD 生态集成边界

本项目维持“DWG/DXF 规范化与理解”为核心，不内嵌其他 CAD 工程源码。第三方工具
通过可选本地适配器或独立 MCP 协作，以降低依赖和许可证耦合。

## OpenSCAD

适用于二维 DXF 的参数化拉伸、预览和 STL/3MF/OFF 导出。项目通过官方 CLI 调用，
临时 SCAD 只包含 `import()` 和 `linear_extrude()`。这不是完整 DWG 渲染器；带文字、
尺寸或开放轮廓的 DXF 不一定形成可拉伸实体。

## OpenCAD

“OpenCAD”存在多个同名项目。此处特指 `caid-technologies/OpenCAD` 的 Python/OCCT
参数化建模系统。它用于从约束草图生成 STEP/STL，与现有图纸读取能力并列；当前
只做安装探测，不对其快速演进的 API 建立硬依赖。浏览器 BIM 项目
`CariHQ/opencad` 和 GPL 的 OpenCADStudio 不被嵌入。

## CAD Skills 与 MCP

吸收的工作流原则是：保留源文件、先规范化并验证、输出 annotation-aware JSON、
按用途生成预览、再做图层/块/区域聚焦检查。本项目 MCP 提供：

- `cad_doctor`
- `extract_dxf_text`
- `inspect_dxf`
- `extrude_dxf_with_openscad`

MCP 默认 stdio。前三个工具只读，最后一个明确写入调用者指定路径。需要原生
AutoCAD 写回、事务、撤销或 COM/LISP 时，应连接专门的 AutoCAD MCP。

## 许可证

适配器只调用外部可执行文件，不复制第三方代码。各外部程序的许可证仍适用于该
程序及其产物；字体授权另见 `fonts/README.md`。
