# jianying-MCP

[![Windows CI](https://github.com/yufeiyufei888/jianying-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/yufeiyufei888/jianying-MCP/actions/workflows/ci.yml)
[English](README.en.md) · [安装](docs/install.md) · [接口与示例](docs/api.md) · [原生验收](docs/native-acceptance.md) · [兼容范围](docs/compatibility.md)

剪映本地草稿工具：可独立使用的 Python CLI、轻量 stdio MCP，以及自制的 [`jianying-local` 调用 skill](skills/jianying-local/SKILL.md)。**社区工具，非剪映官方接口**，不是云剪辑服务，也不是自动导出器。

只引用完整原视频和本地配乐，不复制、不转码、不删除原媒体。所有工程修改只产生新草稿或独立副本，保留手工精剪及未知 JSON 字段；不通过全部重铺来修复少数缺失镜头。

## 做什么，不做什么

| 能力 | 边界 |
| --- | --- |
| `create` | 已审核镜头顺序、入出点、裁切；完整原片引用 |
| `enrich` | 在副本上追加 BGM、淡入淡出、显式口播避让、指定转场 |
| `patch` | 普通连续主轨局部插入／调序／裁短／延长，明确副轨联动或固定 |
| 配乐 | 显式循环、接缝／多曲交叉淡化、流式响度与峰值检查；不自动归一化 |
| 字幕 | 成片 SRT 或完整原片 SRT 按每次使用位置映射，独立可编辑底部字幕轨 |
| 重链接 | 指定逐文件或目录映射，检查内容证据；不扫描整盘，不移动视频 |
| 保存状态 | 旧平面明文及锁定版本的单时间线嵌套工程；不把陈旧根 JSON 当最新状态 |

不提供语音识别、云音乐下载、任意命令／任意文件解密、上传、自动导出或开机服务。多时间线、复合片段、倒放和复杂变速不承诺兼容。必要小资源每份副本上限 **20 MiB**；依赖不明就停止。

旅行镜头审查、叙事和取舍在伙伴仓库 [VlogForge AI / travel-vlog-pipeline](https://github.com/yufeiyufei888/vlogforge-ai-video-skill) 维护。本仓库只做机械执行，不擅自压缩时长，不复制完整旅行 skill 或上游 `jianying-editor`。

## 六个接口

CLI 与 MCP 共享 `doctor`、`list_drafts`、`inspect_draft`、`plan_draft`、`apply_plan`、`verify_draft`。输入输出是 UTF-8 JSON，时间是整数微秒，音量是线性系数（不是分贝）。

流程：读取最新状态 → 重链接 → 局部修改与联动 → 字幕映射 → 配乐／转场 → 变更预览 → 独立副本 → 全量差异校验 → 备份索引并注册。计划绑定代码、配置、源稿、媒体及格式版本；过期、重名、未知依赖或剪映运行中均拒绝写入。重复请求返回原回执，不重复创建。

## 从只读诊断开始

Windows 10/11、Python **3.12** 为测试基线；复用已有 Python 与 FFmpeg/FFprobe。外部后端和格式转换依赖不捆绑，见 [THIRD_PARTY.md](THIRD_PARTY.md)。不需要 WSL，也不需要安装语音模型。

```powershell
git clone https://github.com/yufeiyufei888/jianying-MCP.git
Set-Location .\jianying-MCP
# 模板尚不存在时复制；按实际环境修改，不要覆盖已有配置。
Copy-Item .\config.example.json .\config.local.json
py -3.12 -I -B -X utf8 .\scripts\jianying_local\cli.py doctor --config .\config.local.json
```

新安装默认 `native_acceptance: pending`。诊断可以运行，但生产写入与全局注册需完成本机格式闭环及原生测试。仓库**不携带任何人的验收记录**；CI 通过不等于你的剪映版本兼容。请依次阅读 [安装](docs/install.md) 和 [原生验收](docs/native-acceptance.md)，再显式安装固定 `mcp==1.26.0` 的独立小型环境、做真实调用和注册。

安装不会自动改 Codex 配置。只有明确执行 `setup_mcp.py register` 才备份配置并追加 `jianying-local`；已有同名服务冲突时停止。回滚只移除本次服务注册，保留其他设置、素材、草稿及后续手工修改，详见 [rollback.md](docs/rollback.md)。

## 测试与公开边界

```powershell
py -3.12 -B -X utf8 .\scripts\validate_release.py
py -3.12 -B -X utf8 -m unittest discover -s .\scripts\tests -v
# 仅使用装有固定 SDK 的解释器；临时合成元数据，不打开剪映、不注册、不写原生工程。
& '<SDK Python>' -I -B -X utf8 .\scripts\jianying_local\smoke_readonly.py
```

原有 **108** 项测试（含 **11** 项原片引用回归）保留，并增加路径配置、新安装门禁、公开文件与传输检查。没有外部后端时，其 11 项回归及 3 项真实后端集成测试显式跳过；Windows 无符号链接权限时再跳过 1 项。CI 只使用合成夹具，不下载私人素材、不启动剪映，也不推定原生写入兼容性。结果边界见 [compatibility.md](docs/compatibility.md)。

发布按 [`release-files.json`](release-files.json) 白名单检查。无原片、BGM、真实草稿、台词、截图、日志、回执、密钥、虚拟环境和二进制依赖。**文件校验、界面观察、实际试听、音乐发布授权是四件不同的事**；音轨或波形可见不是听感已确认。

## 许可

自制部分 [Apache-2.0](LICENSE)，第三方保持原许可，不统一重新授权。剪映名称及商标归其所有者；仅在你的自有或获授权工程上使用本地格式适配，不修改剪映程序、不绕过会员或账号限制。
