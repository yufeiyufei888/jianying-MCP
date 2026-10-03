# 安装与共享配置 / Installation

## 1. 先诊断，不批量安装

复用已有 Python 3.12、FFmpeg 和 FFprobe；CLI 使用标准库，不需要先装 MCP SDK。确认本机剪映构建是 **11.5.0.14471**，并阅读 [第三方来源](../THIRD_PARTY.md)。其他版本只诊断，不宣称可写。

独立克隆本仓库。仅在目标配置不存在时从 `config.example.json` 复制为 `config.local.json`，用文本编辑器填写。路径支持系统环境变量和相对配置文件目录的路径，未知字段／未展开变量拒绝。配置不可公开提交。

| 键 | 含义 |
| --- | --- |
| `drafts_root` | 剪映原生草稿根目录，默认解析本用户 LocalAppData |
| `work_root` | 工具计划、快照、验收、回执；默认仓库 `.local/work`，不得与草稿目录嵌套 |
| `skill_root` | 单独安装且经过引用修复的第三方 `jianying-editor` 根目录 |
| `worker_python` | 只读诊断时可省略；验收／SDK 启动前必须明确固定已有 Python 的完整路径，不能填命令或参数数组 |
| `app_root` | 锁定剪映构建的安装目录，不自动选择任意最新版 |
| `codec_root` | 本地固定 v0.1.1 格式转换程序目录 |
| `codex_config` | Codex 配置路径；仅显式注册／回滚时才会写 |

CLI 和 MCP 共用 `--config <file>`，也可用受信任的启动环境变量 `JIANYING_LOCAL_CONFIG`。CLI 保留 `--drafts-root`、`--work-root`、`--skill-root` 并补齐其他路径参数；显式参数优先。MCP 工具请求不能修改这些位置或指定命令。注册会冻结所有解析后的路径及 worker Python。

`worker_python` 省略时使用当前解释器；CLI 与独立 SDK 环境不是同一个解释器。因此在原生验收前，用 `py -3.12 -c "import sys; print(sys.executable)"` 获取选定已有解释器的路径，将它写入本地配置的 `worker_python`，之后不换。否则 SDK 启动时指纹变化会使验收失效并停止，不能靠复制通过记录绕过。模板故意不猜用户的 Python 安装位置。

```powershell
py -3.12 -I -B -X utf8 .\scripts\jianying_local\cli.py doctor --config .\config.local.json
```

## 2. 外部后端与格式适配

本仓库没有 backend 源码、剪映或 DLL。按 [THIRD_PARTY.md](../THIRD_PARTY.md) 获取你有权使用的后端，核对版本与本地源码，运行 11 项引用回归；不能满足时停止，不盲目升级整包。

需要读取本人的非明文保存状态时，可显式运行固定下载／校验脚本。它只访问文档指定的公开 v0.1.1 发布文件，保留 MIT 许可证，只调用本机剪映 DLL，不能改变应用程序：

```powershell
py -3.12 -I -B -X utf8 .\scripts\jianying_local\install_codec.py --config .\config.local.json
```

此步骤会新增便携依赖文件，不是启动时自动下载。校验值不符、构建不符或文件已存在时停止；不安装 GUI 或编译工具链。

## 3. 本机原生验收

执行 [native-acceptance.md](native-acceptance.md)。普通 `apply_plan` 与注册必须通过全部五组测试；只读 SDK 握手无需原生写入许可，也不能解除门禁。更新代码／配置／路径／后端／应用／格式转换程序后，旧计划及验收可能失效，必须重新预览并补验收。

## 4. 单独安装 SDK、验证并显式注册

仅在本机原生验收通过后执行：

```powershell
py -3.12 -I -B -X utf8 .\scripts\jianying_local\setup_mcp.py install --config .\config.local.json
& .\.venv-jianying-mcp\Scripts\python.exe -I -B -X utf8 .\scripts\jianying_local\smoke_readonly.py
& .\.venv-jianying-mcp\Scripts\python.exe -I -B -X utf8 .\scripts\jianying_local\smoke_mcp.py --config .\config.local.json
```

`install` 建立专用 `.venv-jianying-mcp`，只安装固定 `mcp==1.26.0` 及其依赖，不修改已有视频环境。该 SDK 仍有实际体积和内存消耗，不承诺“零占用”。已有专用目录则停止，不覆盖它。

`smoke_readonly` 仅操作临时合成元数据。`smoke_mcp` 在已验收环境执行六工具握手、真实读取和**独立测试副本写入**，产生本地回执。它不是批量修改正式 Vlog。手动打开其新测试副本检查播放、保存、重开，再彻底退出剪映。

最后由用户明确执行全局注册：

```powershell
py -3.12 -I -B -X utf8 .\scripts\jianying_local\setup_mcp.py register --config .\config.local.json
```

仅追加 `jianying-local`，先备份，保留其他配置；已有同名服务、配置竞争或验收失效就停止，不自动操作窗口。重新加载客户端以发现服务，按客户端官方 [MCP 配置说明](https://developers.openai.com/codex/mcp/) 核对。

## 5. 安装调用 skill（可选、显式）

将 `skills/jianying-local` 这一份自制调用 skill 复制到所选工作区或用户级 skill 目录，先检查目标同名目录是否存在，存在则审查合并，不覆盖。不要复制整个仓库到 skill 目录，也不要重新安装完整上游 `jianying-editor`。调用 skill 里的 `<tool-root>` 指向本仓库；旅行 skill 从伙伴仓库使用。

普通启动不扫描素材、不加载 DLL、不打开剪映；格式转换只在具体调用需要时启动短命子进程。
