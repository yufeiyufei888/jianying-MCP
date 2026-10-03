# 第三方来源与许可边界

本仓库只发布自制工具、调用 skill、文档与合成测试；以下依赖需用户另行获取或复用，**均不捆绑第三方代码、程序或 DLL**。

## 低层草稿后端

- 来源：[`luoluoluo22/jianying-editor-skill`](https://github.com/luoluoluo22/jianying-editor-skill)。参考适配基线：1.5.0，提交 `8e39b266fac12687dbe1db8c9632c1229a84f0dc`。
- 使用位置：`<skill_root>/scripts/vendor/pyJianYingDraft`，只加载低层材料、片段和轨道模块，不调用 GUI 控制器或可能重建原轨道的高层工程加载器。
- 基线还需要：完整路径身份哈希（不只 basename）、材料 ID 与导出 ID 一致、媒体原位引用，以及云音频失败不产生占位片段的修复。这些行为由原有 **11 项引用回归**约束。当前 MCP 不暴露云音乐接口；回归仅测试后端失败路径，不执行下载。
- 基线提交的根许可证情况不足以据此重新分发整包，因此只提供来源和版本要求，不重新授权、不复制完整 skill/vendor。较新上游可能改动复制策略，不能仅因版本新就认为兼容。
- 本地 `profile` 将后端相关源文件／模板内容、工具源码、配置、路径及解释器指纹绑定进计划与验收。更换后端要重新测试，不用当前使用者的通过记录。

## 保存格式转换程序

- 来源：[`wenshui330/jy-draftc`](https://github.com/wenshui330/jy-draftc)，固定 **v0.1.1**，MIT（本地显式安装时保留原许可证）。
- 固定发布文件：[jy-draftc-amd64-windows.zip](https://github.com/wenshui330/jy-draftc/releases/download/v0.1.1/jy-draftc-amd64-windows.zip)。
- SHA-256：`63546a8f6013e860e825954fdf755a29751963cd56eca8f83e26771485c53dba`。
- 本工具版本范围仅 **11.5.0.14471**；即使上游声明更宽范围，也要本机原生闭环。安装器核对下载、可执行文件与本机安装目录，保留 provenance；调用前重新校验。
- 仅处理工具生成的暂存自有草稿元数据；不向 MCP 暴露任意解密入口，不上传，不修改剪映，不绕过会员或账号。

## 官方 Python MCP SDK

- [`modelcontextprotocol/python-sdk`](https://github.com/modelcontextprotocol/python-sdk)，MIT；固定 [`mcp==1.26.0`](https://pypi.org/project/mcp/1.26.0/)。
- 只放入独立 adapter 环境，不改变既有视频环境，不附带额外 CLI 套件。

## FFmpeg / 剪映 / 旅行规则

- 复用用户安装的 FFmpeg/FFprobe，不分发二进制。不同 FFmpeg 构建适用其自身许可。响度测量参考 [官方 ebur128 文档](https://ffmpeg.org/ffmpeg-filters.html#ebur128)，测量不等于试听。
- 剪映由用户安装及授权使用，应用、字体、缓存资源和 DLL 不属于本仓库许可范围；不分发这些文件。
- 旅行审查、叙事、渲染及其锁定上游来源见 [VlogForge AI](https://github.com/yufeiyufei888/vlogforge-ai-video-skill)。两仓库仅互链，不在本仓库重新发布完整旅行 skill。

本仓库的 Apache-2.0 不授予任何第三方商标、媒体、音乐或源码的新权利。使用本地 BGM 不自动意味着获准发布到哔哩哔哩／抖音。
