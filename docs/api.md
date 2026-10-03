# CLI / MCP 接口

时间一律整数微秒（1 秒 = 1000000），音量是有限线性系数。路径必须是实际本地文件，不在请求里写可执行命令。相对媒体路径按进程工作目录解析，建议先解析为完整路径；示例占位符必须替换。

## 六个操作

| 操作 | JSON 参数 | 副作用 |
| --- | --- | --- |
| `doctor` | `{}` | 诊断，无素材扫描 |
| `list_drafts` | `{"limit":100}` | 读取指定草稿索引 |
| `inspect_draft` | `{"name":"<draft-name>","limit":100}`，可加绑定指纹的 `cursor` 或 `audio_analysis` | 读取最新工程，必要时保存小型暂存解码快照／音频指标缓存，不改原稿 |
| `plan_draft` | CLI：完整请求；MCP：`{"request":<完整请求>}` | 在工具工作目录保存不可变计划／预览，不修改原稿 |
| `apply_plan` | `{"plan_id":"<id>","expected_plan_sha256":"<preview-hash>"}` | 仅创建／注册独立草稿或副本 |
| `verify_draft` | `{"name":"<draft-name>","plan_id":"<id>","validation":"exact"}` | 校验，可按最新保存状态生成必要解码缓存，不修改原稿 |

CLI 调用：

```powershell
py -3.12 -I -B -X utf8 .\scripts\jianying_local\cli.py plan_draft --config .\config.local.json --request-file .\request.local.json
```

也支持标准输入 JSON（Windows PowerShell 写入 stdin 时确保 UTF-8；非 ASCII 请求推荐 `--request-file`）。输出 `{"ok":true,"result":...}` 或 `{"ok":false,"error":{"code":...,"message":...}}`；结构／参数错误退出码 2，未预期错误 3。MCP 同样返回 JSON，并附带该次 worker 信息。

顶层请求必填 `request_id`（新操作 UUID）、`mode`、`name`（新草稿单目录名）。未知字段拒绝。`create` 填 `canvas` 和已审核 `clips`；`enrich/patch` 填 `source_draft`，且新名不能同原稿。示例见 [`examples`](../examples)。

## 操作参数

- 镜头：`path/in_us/out_us`；可选 `volume`、归一化矩形 `crop` 和 `transform`。范围不得超过 ffprobe 实测完整原片。新建不能用预裁切小视频充当原片。
- 局部修改：`type` 为 `insert_clip/move_clip/trim_clip`；显式 `track_id`、片段 ID／锚点。每次操作为**其他所有轨道**提供 `companion_tracks: {"<track-id>":"follow"或"fixed"}`，不存在其他轨道则 `{}`。后续操作基于前一步计算后的状态。跨不连续区域副轨、不明效果或原有转场冲突直接停止。
- 配乐：`path/start_us/duration_us/volume`，可选 `in_us/fade_in_us/fade_out_us/loop/loop_out_us/loop_crossfade/loop_crossfade_us/crossfade_previous_us`。显式循环衔接默认 250000 微秒，多曲交叉淡化须符合相邻重叠条件。末段精确结束；预览可能按原生时间线帧量化。
- 避让：`ducking` 数组元素含 `start_us/end_us/volume/attack_us/release_us`，使用最终时间轴时间。范围连同 attack/release 必须有序、不重叠且位于 BGM 内。无显式区间即未执行语音检测；SRT 也不自动推导区间。
- 转场：`after_segment_id/before_segment_id/resource_id/duration_us`（分别是切点左侧与右侧片段）；仅相邻切点、两端原片余量足够、无已有冲突且资源本机原生验收通过。以预览有效时长为准，不在所有切点自动添加。
- 字幕：`path/basis`；`basis=timeline` 无原片选择器；`basis=source` 必须有且只有 `material_id` 或 `media_path`。可填 `encoding/partial_policy/track_name`。默认 UTF-8/BOM，当前跨界策略仅 `preserve_text_clip_time`；保留整句文字、裁显示时间并列待审，不虚构逐字时间戳。
- 重链接：`relinks.files` 的 `old_path/new_path` 或 `relinks.directories` 的 `old_dir/new_dir`。证据不足仅在用户明确确认后填 `confirm_unverified:true`，不能当作已证明同一素材。未确认和同名不同内容拒绝。
- 响度：`audio_analysis` 元素提供 `path/in_us/duration_us`（见代码 `music.analyze`）；只测量／建议，不自动修改。

分页游标绑定源稿指纹，用户保存新版本后旧游标无效。inspect 将真实内容来源、缺失引用、支持状态与可用能力分开返回。

## 校验与执行边界

先审查 `plan_draft` 输出中的镜头前后范围、总时长、受影响轨道、联动映射、待审字幕、资源字节数、转场有效时长，再用**同一计划 ID 与哈希**执行。禁止手工编辑计划来绕过门禁。

应用前要求彻底退出剪映。原稿／索引／媒体／配置变化会使计划过期；目标存在时不覆盖。失败保留本次副本、诊断、索引备份，不强制回滚原稿。重复提交同一 request_id／计划返回原回执，不重建用户后来编辑的副本。

保存重开后使用 `validation:"after_save"` 验证语义。它允许锁定版本已观察的保存规范化，但不是“忽略全部未知字段”；实际裁剪、音量、文本或关联资源改变仍报差异。根密文字节不要求一致，剪辑内容及授权边界必须一致。

回执分别记录文件校验、注册、原稿指纹与人工验收状态。`editor_acceptance: not_inferred/pending` 不能改写成已经播放正常；本地音乐路径也不是发布授权凭证。
