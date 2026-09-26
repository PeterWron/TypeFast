# TypeFast 开发文档

> 面向**下一次迭代**的开发者手册：架构、功能、关键技术决策、已知坑与迭代计划。
> 使用者文档看 README.md；延迟实测数据看 docs/LATENCY.md。

| 项 | 值 |
|---|---|
| 版本 | 0.0.6 |
| 最后更新 | 2026-09-26 |
| 运行环境 | Windows 10/11 + Python 3.11（Anaconda base 已验证） |
| 音频输入 | USB 麦克风 K11/K12（WASAPI 48 kHz 采集，程序内重采样到 16 kHz）；板载 Realtek 插孔未接入 |
| 语音识别 | 硅基流动 XingChenAGI/XingChenASR-V3.2-Ultra |
| 文字润色 | 硅基流动 Qwen/Qwen2.5-7B-Instruct（OpenAI 兼容接口） |

## 0. 文档地图

| 文档 | 面向 | 内容 |
|---|---|---|
| README.md | 使用者 | 安装、命令、默认行为、常见问题 |
| DEVELOPMENT.md（本文） | 开发者 | 架构、模块、决策、扩展、坑、迭代计划 |
| docs/LATENCY.md | 性能优化 | 逐环节延时实测、优化项与预期收益 |
| docs/ARCHITECTURE.md | 历史参考 | 初版架构说明，部分内容已被本文覆盖 |
| docs/ROADMAP.md | 规划 | 里程碑草案（本文第 9 节是可执行版本） |
| CHANGELOG.md | 变更记录 | 每个版本做了什么 |

## 1. 项目概览

**定位**：Windows 上的 AI 语音输入法。按住热键说话，松手后文字经「识别 → 润色 → 投递」落到光标处。

**形态**：后台常驻服务（无界面）+ 独立设置窗口 + 屏幕底部语音 HUD。**不是系统输入法**（没有 TSF 文本服务），属于「辅助输入工具」，靠剪贴板/SendInput 把文字送进目标应用。

**当前能力**（0.0.6）：

| 能力 | 状态 |
|---|---|
| 按住热键说话 → 识别 → 润色 → 投递 | 可用 |
| 录音中按停顿切段、边录边发（松手只等尾巴） | 可用（切段默认 6 s，待调优） |
| 短句跳过润色、润色硬截止回落原文 | 可用 |
| 模型/端点/密钥自由更换（界面或配置） | 可用 |
| 术语纠正 + 词库三层（热词/纠正/学习） | 部分可用（学习未接投递后校验） |
| 音频文件批量转写（mp3/wav/m4a/flac） | 可用 |
| 演示模式（系统 TTS 合成语音走真实模型） | 可用 |
| 真实麦克风输入（按住热键说真实语音） | 可用（本机 USB 麦克风 K11/K12 实测通过，见 2.1 与第 14 节） |
| 图形设置界面（模型/密钥/按键/音频/隐私/服务控制） | 可用 |
| 语音 HUD（状态点 + 电平条 + 预览文本，不抢焦点） | 可用 |
| 流式识别（边说边出字） | 未实现（服务端接口为整段式） |
| 密码框识别（UIA）、TSF 内联合成、托盘图标、开机自启 | 未实现 |

**三条不变量**（改代码时不要破坏）：

1. 润色绝不阻塞出字：超时或失败必须回落 ASR 原文。
2. 主链路不因单点失败中断：分段失败向前合并、投递失败降级提示。
3. 密钥只进 Windows 凭据管理器，绝不写进代码、配置或日志。

## 2. 开发环境与上手

```
cd E:/py/typefast
python -m pip install -r requirements.txt      # requests numpy psutil sounddevice keyring pytest
python -m pytest -q                            # 17 项离线单测，应全绿
```

常用命令（等价于 `run_typefast.bat <子命令>`，在 src 目录下用 python -m typefast）：

| 命令 | 作用 |
|---|---|
| `doctor` | 检查 Python / 麦克风设备与后端 / 密钥配置 / 当前模型 |
| simulate | 不开麦不联网，用合成音频跑一遍流水线 |
| run [--demo] [--selftest] [--no-deliver] [--no-overlay] | 启动服务；--demo 用系统 TTS 语音替代麦克风 |
| `start` [--demo] [--no-ui] | 一键启动：自动判断麦克风可用性 + 打开设置窗口 |
| `ui` | 打开设置窗口 |
| `transcribe` <文件> [--no-polish] | 批量转写音频文件，结果写入 数据目录/records/last_transcript.txt |
| key set/status | 写入或查看 API Key（Windows 凭据管理器） |
| `config` | 打印配置路径与生效配置 |

运行时产物位置：
- 配置：<项目根>/config.json（不存在则用代码默认值；装到 site-packages 时回落 %APPDATA%/TypeFast/config.json）
- 数据：<项目根>/data/（源码树内优先；装到 site-packages 时回落 %LOCALAPPDATA%/TypeFast；可用 TYPEFAST_DATA 覆盖）
  - data/logs/  运行日志（typefast.log + 轮转备份）
  - data/records/  文字记录（last_transcript.txt、bench_latency.json）
  - data/state/  运行状态（router_state.json、vocabulary.json）
  - data/audio/  语音（demo_speech.wav，将来 keep_audio 的录音）

### 2.1 本机音频输入环境（2026-09-26 复测）

板载音频没有任何可用输入：Realtek 麦克风端点长期是未插入（UNPLUGGED）状态，PortAudio 只在 WDM-KS 层枚举到它，而 WDM-KS 在当前构建里打不开（PaErrorCode -9999）。插上 USB 麦克风 K11/K12 之后，MME / DirectSound / WASAPI / DirectShow 四条通道都能看到它。

| 设备 | Host API | 16k 直开 | 设备默认率 | 备注 |
|---|---|---|---|---|
| 麦克风 (K11/K12) | MME | OK | 44100 | 备选 |
| 麦克风 (K11/K12) | DirectSound | OK | 44100 | 备选 |
| 麦克风 (K11/K12) | WASAPI | 失败 -9997 | 48000 | **自动挑选选中它**；Recorder 回落设备默认率再重采样到 16k |
| 麦克风 (Realtek HD Audio Mic input) | WDM-KS | 失败 -9999 | 44100 | 板载插孔未接入，不可用 |
| 立体声混音 (Realtek HD Audio Stereo input) | WDM-KS | 失败 -9999 | 44100 | 录的是系统播放声，不是麦克风，同样不可用 |

不联网自检两条命令：

```
python tools/mic_probe.py    :: 列设备 + 逐个试开 16k / 设备默认率
python tools/mic_check.py    :: 录 2 秒，打印自动选中设备、实际采样率、峰值
```

2026-09-26 实测：自动选中 #15 [Windows WASAPI] 麦克风 (K11/K12)，16k 打开失败后回落 48000 Hz，录到 25120 样本，peak 0.0239，RESULT: OK。

结论：真实录音可行。input_device 留空即自动挑选；要固定就填 WASAPI 那条的序号（本机为 15），不要写死 MME 序号（会随插拔漂移）。

## 3. 架构

### 3.1 分层与依赖方向

```
ui/          设置窗口（tkinter）          ← 只依赖 settings 与 service 辅助
platform/    Windows 适配层              ← 只有这里碰 Win32 / WASAPI / tkinter
  hotkey.py    低级键鼠钩子（按住说话）
  audio.py     WASAPI 采集 + 演示录音器 + 电平回调（snapshot()/stop() 对外统一 16k）
  audiofile.py 音频文件解码（ffmpeg）
  tts.py       系统 TTS 合成（演示语音 / 接口自测）
  delivery.py  SendInput / 剪贴板投递 / 前台进程
  overlay.py   语音 HUD（不抢焦点）
  winapi.py    集中声明 Win32 函数原型
core/        平台无关引擎                ← 可单测、可整体替换实现
  contracts.py  数据契约（状态机、Utterance、ErrorKind、Timings）
  session.py    会话编排（热键 → 采集 → 切段 → 识别 → 润色 → 投递）
  segmenter.py  切段规划（纯计算，可单测）
  vad.py        能量 VAD（底噪 P2 + 峰值兜底）
  pipeline.py   识别 → 术语纠正 → 润色 → 形态整理
  router.py     模型候选链 + 额度熔断
  vocabulary.py 词库三层
providers/    模型接入（按注册名创建）
  base.py           AsrProvider / PolishProvider 协议
  siliconflow.py    硅基流动识别（默认）
  volcengine.py     火山引擎极速版识别
  openai_compat.py  OpenAI 兼容识别 + 润色（默认润色走这里）
  mock.py           离线 mock
cli.py / service 辅助：命令入口、服务启停
tools/        测量与诊断脚本（不属于运行时）
```

依赖方向单向：`ui` → settings；platform → core → providers 协议。**core 不得 import platform**，这条是硬约束。

### 3.2 线程模型

| 线程 | 职责 | 约束 |
|---|---|---|
| 钩子线程 | 键盘 / 鼠标低级钩子 | 回调内只做最小工作，必须快速返回，否则被系统摘钩子 |
| tick 线程 | 每 300 ms 判断能否切段、回收在途分段 | 纯计算，不阻塞 |
| 工作线程池 | 分段识别与润色（默认 4 线程） | 润色用 future.result(timeout) 实现硬截止 |
| 音频回调线程 | 采集 + 计算 RMS 电平 | 零分配、只入队 |
| HUD 线程 | Win32 消息泵 + 渲染循环（Pillow 逐帧合成） | 其它线程只往队列投递，不直接碰窗口/图像 |
| 投递线程 | 松开后收尾（finalize） | 与 tick 线程共享状态用 RLock 保护 |

### 3.3 数据契约（core/contracts.py）

| 类型 | 作用 |
|---|---|
| SessionState | idle / arming / recording / finalizing / delivering / done / empty / error，HUD 状态映射依据 |
| ErrorKind | 错误分类：not_configured / network / timeout / server_busy / server_failed / no_speech / audio / blocked，决定是否原地重试 |
| ProviderError | Provider 层统一异常，带 kind 与 detail |
| Segment / Utterance | 一次说话的段落集合、原文与润色结果、时间戳 |
| Timings | 逐阶段耗时：release2text / asr / asr_encode / asr_ttfb / asr_download / polish / delivery |

新增或修改字段时，同步检查：HUD 状态映射（platform/overlay.py 的 STYLE）、会话状态流转（core/session.py）、以及 `tools/bench_latency.py` 的字段引用。

### 3.4 一次会话的完整时序（核心链路）

```
按下 Right Ctrl
  → 钩子线程回调（只置事件，快速返回）
  → session._on_press：启动采集（演示模式换 DemoRecorder）+ 新建 Utterance
  → HUD：recording（蓝色，电平条开始跳动）

说话中（tick 线程每 300 ms）
  → recorder.snapshot() 取当前全部样本
  → CommitPlanner.find_cut() 找停顿切点（目标 6s / 最短 3.5s / 活边缘 1.5s / 搜索半径 4.5s）
  → 命中则提交该段识别（线程池），HUD 显示已提交文本
  → 该段为空或失败：不推进 committed_index，音频并入下一段（绝不丢内容）

松开
  → recorder.stop() 拿完整样本，HUD：finalizing（橙色）
  → 等在途分段完成（上限 6 s；超时则把它的音频并回尾巴重发）
  → 尾巴（committed_index 之后）送识别
  → joined_raw() → 术语纠正 → polish_text()（硬截止 1500 ms，超时/失败回落原文）
  → shape_for_delivery()（聊天类应用去掉句末句号）
  → TextDelivery.deliver()：短文本 SendInput，长文本剪贴板 + Ctrl+V + 还原剪贴板
  → HUD：done（绿色，1.4 s 后自动收起）；异常路径 HUD：error（红色，3.6 s）
```

### 3.5 Provider 协议与注册

```python
class AsrProvider(Protocol):
    name: str
    def transcribe(self, req: AsrRequest) -> AsrResult: ...

class PolishProvider(Protocol):
    name: str
    def polish(self, req: PolishRequest) -> str: ...
```

| provider 名 | 用途 | 模型 | 端点 | 备注 |
|---|---|---|---|---|
| siliconflow | 识别（默认） | XingChenAGI/XingChenASR-V3.2-Ultra | /v1/audio/transcriptions | multipart 传 16k 单声道 wav；实测不接受 language 参数 |
| openai_compat | 识别 | 任意，如 qwen3-asr-flash | 任意 OpenAI 兼容 | 音频走 input_audio base64 |
| openai_compat | 润色（默认） | Qwen/Qwen2.5-7B-Instruct | /v1/chat/completions | 支持按请求覆盖 model（auto 路由用） |
| volcengine_flash | 识别 | 火山极速版 bigmodel | openspeech.bytedance.com | 字段待联调 |
| mock | 识别 / 润色 | 无 | 无 | 返回固定文本，离线自检与单测用 |

**新增一个 Provider 的标准步骤**：

1. 在 providers/ 下新建模块，实现 name 与对应方法，错误一律抛 ProviderError(ErrorKind, 说明, 原始信息)。
2. 识别要在 AsrResult 里填 encode_ms / ttfb_ms / download_ms（逐阶段计时，性能回归要靠它）。
3. 在 cli.build_asr 或 cli.build_polisher 里按 provider 字符串注册。
4. 在 ui/settings_window.py 的下拉选项里加上；需要新密钥槽位就加进 KEY_FIELDS。

### 3.6 配置系统

优先级：代码默认值（settings.py 的 dataclass）→ config.json 覆盖 → 环境变量 TYPEFAST_CONFIG 指定配置文件路径。
配置文件查找顺序与数据目录一致：TYPEFAST_CONFIG > 源码树根 config.json > %APPDATA%/TypeFast/config.json。

| 分组 | 关键字段 | 说明 |
|---|---|---|
| asr | provider / model / endpoint / language | endpoint 留空则用 provider 内置默认；language 默认 zh-en（中英混说），可选 zh / en / ja / auto，只有火山等少数 provider 会真的发送该字段 |
| polish | provider / model / endpoint / enabled / temperature / max_tokens / deadline_ms / short_text_chars | model 填 auto 或 auto-speed 触发候选链路由 |
| router | mode / quality_chain / speed_chain | 链留空用代码内置默认 |
| hotkey | hold / cancel / mouse_middle | 键名映射见 platform/hotkey.py 的 VK 表 |
| audio | sample_rate / input_device / preroll_ms / keep_warm / normalize | 设备留空自动挑（WASAPI > DirectSound > MME）；keep_warm 常开流（默认开，消除约 20% 的「打开后空流」）；preroll_ms 按下前预缓冲；normalize 送识别前按峰值归一 |
| segment | target_commit_s=5.0 / min_commit_s=5.0 / live_margin_s=1.2 / search_radius_s=2.0 / min_pause_s=0.30 / max_inflight=3 / live_commit=true | 延迟调优主旋钮；短于 5 秒的片段会整句丢内容（见第 8 节问题 11），本机已设 live_commit=false：整段识别、准确率优先 |
| delivery | paste_threshold_chars / restore_clipboard / strip_chat_trailing_period | 投递策略 |
| privacy | keep_audio / exclude_apps / block_password_fields | exclude_apps 命中的前台应用不录音不投递 |

**新增配置项要同步改四处**：settings.py 默认值、from_dict 的分组列表（若新增分组）、config.example.json、ui/settings_window.py（_build_* 加控件 + _collect 加读取）。漏掉 _collect 的话界面保存会把它重置。

### 3.7 凭据管理

Windows 凭据管理器，服务名 typefast：

| 槽位 | 谁读它 |
|---|---|
| siliconflow_api_key | providers/siliconflow.py |
| polish_api_key | providers/openai_compat.py（润色默认槽） |
| asr_api_key | providers/openai_compat.py（识别用） |
| volc_app_key / volc_access_key | providers/volcengine.py |

**为什么识别与润色分成两个槽**：为了换厂商时只改一个。当前两个槽填的是**同一把硅基流动 Key**（该 Key 是账号级的，同一把可访问 /v1/audio/transcriptions 与 /v1/chat/completions）。

**硬规则**：密钥只进凭据管理器；代码、config.json、日志、报错信息里一律不得出现明文 Key（代码里需要人填 Key 的位置统一用注释 `###替换成你的api` 标注）。tools/ 下的脚本要看 Key 就用 get_secret，不要从命令行传。

### 3.8 文本投递

| 通道 | 触发条件 | 实现要点 |
|---|---|---|
| SendInput Unicode | 文本长度不超过 paste_threshold_chars（默认 30） | 逐字符注入，避开剪贴板；长文本会慢且部分应用丢字 |
| 剪贴板 + Ctrl+V | 长文本 | 先快照用户剪贴板，写入时打两个排除标记（CanIncludeInClipboardHistory、ExcludeClipboardContentFromMonitorProcessing），粘贴后按 changeCount 判断再还原 |

失败降级：剪贴板写入失败则退回 SendInput；两者都不可用时提示用户手动粘贴。

**已知缺口**：没有 UIA 焦点上下文，所以无法识别密码框（只能按进程名排除）；没有 TSF，提权窗口（UIPI）与部分独占程序注入会被拒绝。

### 3.9 HUD 与演示模式

- 形态：Apple 风格毛玻璃胶囊（左侧麦克风 + 彩色光晕 + 电平波形，右侧状态行 + 逐字淡入的转录文本）。实现要点见 platform/overlay.py 顶部注释：分层窗口（WS_EX_LAYERED + UpdateLayeredWindow）逐像素 alpha、抓屏模糊 σ=12px + 饱和度 x1.5、1px 内发光边 + 顶部亮边 + 两层柔光阴影、圆角 34px（静态几何 4 倍超采样）、cubic-bezier(0.34,1.56,0.64,1) 弹簧入场（0.4s / translateY 15px / scale 0.8→1）。
- 不抢焦点：WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW + WS_EX_TRANSPARENT（完全穿透点击）；终态自动收起（done 1.4 s / empty 0.9 s / error 3.6 s）。
- 主题：默认暗色玻璃（TYPEFAST_HUD_THEME=light|auto 覆盖）。
- 字体栈（按优雅程度排序）：中文优先思源黑体家族（NotoSansSC-VF.ttf，可变字重，正文 400 / 加粗 600），拉丁字符单独用 Segoe UI / Segoe UI Semibold，回落微软雅黑 UI（msyh.ttc index=1）→ 微軟正黑體 UI → 等线；中英混排按共用基线盒对齐，避免两种字体 ascent 不同导致整行跳动。字号常量：SIZE_STATUS=13 / SIZE_TEXT=18 / SIZE_HINT=12（正文比规范 16px 大一档，改字号时注意 tests/test_overlay.py 的版式约束测试）。设置窗口通过 hud_font_family() 共用同一家族。
- 依赖与成本：需要 Pillow（可选依赖组 hud），没装则 HUD 自动禁用；可见时约 5ms/帧（50fps ≈ 27% 单核），隐藏时不渲染；模糊背景每次弹出抓一次（避免抓到浮窗自己，因此弹出期间不跟随背景变化）。
- 电平来源：真实录音由音频回调算 RMS；演示模式按播放位置取真实样本包络；0.25 秒没有新电平就按静音衰减。
- 演示模式：DemoRecorder 与 Recorder **接口完全一致**（start/snapshot/stop/cancel + on_level），所以走的是同一条流水线，不是假路径。
- 演示语音由系统 TTS 合成（platform/tts.py），落在 数据目录/audio/demo_speech.wav；删掉文件会按内置文本重新合成。
- 演示模式 + 真实模型 = 无需麦克风即可验证端到端效果，这条路径也用于接口联调。

### 3.10 语言与中英混说

| 环节 | 行为 |
|---|---|
| 识别 | 硅基流动端点实测不接受 language 参数，语种由模型自动判断，中英夹说本来就能识别；火山极速版会发送 language，遇到 zh-en / auto 时按 zh 提交（该接口没有混说标记） |
| 配置 | config.asr.language 默认 zh-en，可选 zh / en / ja / zh-en / auto；设置界面「识别语言」下拉用同一套取值 |
| 润色 | core/pipeline.py 的 bilingual_language() 判定混说配置后，把 PolishRequest.output_language 置为 mix；openai_compat 的 LANG_MIX 提示词要求保持原语种搭配、不翻译、中英相邻处按中文排版加空格 |
| 显式指令 | 口述以「用英文 / 用日文 / 用中文」开头时，语言指令优先于混说配置（detect_language_command），该前缀不会出现在最终文本里 |
| 实测 | 2026-09-26 用系统 TTS 合成一句中英夹话走真实链路：raw 里的英文片段原样保留，润色把「API T」按上下文修成「API Key」，并补上中英之间的空格 |

## 4. 功能清单与实现位置

| 功能 | 实现位置 | 关键参数 / 备注 |
|---|---|---|
| 按住说话（键盘 / 鼠标中键） | platform/hotkey.py | VK 映射表在文件顶部；低位钩子必须快速返回 |
| 麦克风采集 | platform/audio.py Recorder | 优先 16k，失败回落设备默认率并线性重采样；snapshot()/stop() 统一输出 16k，按标称率换算（实测交付率只写日志作诊断：变速对照表明按墙钟拉伸会拖慢 1.33 倍、识别变差）；keep_warm 常开流 + 看门狗自愈（消除约 20% 的整段空流） |
| 送识别前电平归一 | core/pipeline.py _prepare_audio | 峰值在 0.02-0.85 之间时按峰值提到 0.85（最大 4 倍，只提升不衰减）；实测 RMS −29 dBFS 会导致整句漏字，归一后恢复正常；audio.normalize 可关 |
| 演示录音（无麦克风） | platform/audio.py DemoRecorder + platform/tts.py | 接口与 Recorder 一致 |
| 音频文件解码 | platform/audiofile.py | ffmpeg（PATH 优先，imageio-ffmpeg 兜底） |
| 能量 VAD | core/vad.py | 底噪取 P2、峰值 6% 兜底、停顿不短于 0.25s |
| 切段规划 | core/segmenter.py | 目标 6s / 最短 3.5s / 活边缘 1.5s / 半径 4.5s（**延迟主旋钮**） |
| 会话编排 | core/session.py | tick 300ms；同一时刻只允许 1 段在途 |
| 识别编排 | core/pipeline.py | 短句跳过润色、逐阶段计时、Provider 异常分类 |
| 润色编排 | core/pipeline.py | 硬截止（默认 1500ms）、auto 候选链、术语纠正 |
| 模型路由与熔断 | core/router.py | 额度类失败熔断 20 小时，状态落盘 state/router_state.json |
| 词库三层 | core/vocabulary.py | 热词 / 整词纠正 / 从编辑学习（学习尚未接到投递后校验） |
| 文本投递 | platform/delivery.py | 短文本 SendInput、长文本剪贴板、剪贴板历史排除 |
| 语音 HUD | platform/overlay.py | 毛玻璃胶囊、彩色光晕 + 电平波形、逐字淡入、弹簧入场、终态自动收起、不抢焦点 |
| 设置界面 | ui/settings_window.py | 5 个标签页 + 服务启停 + 连通性测试 |
| 命令行 | cli.py | run / `start` / `ui` / simulate / `transcribe` / `doctor` / `config` / key |
| 服务启停 | ui/settings_window.py（service_pid / start_service） | cli.cmd_start 复用同一份实现，不重复造 |

## 5. 关键技术决策（ADR 摘要）

| # | 决策 | 理由 | 代价 |
|---|---|---|---|
| 1 | core 与 platform 严格分离，core 不 import platform | 引擎可单测、可整体替换实现（将来换 Rust/原生只换 platform） | 多一层接口，简单功能也要绕 |
| 2 | 润色有硬截止，超时/失败回落 ASR 原文 | 输入法不能为质量牺牲出字时间；这是产品底线 | 长文本可能拿不到润色（见第 8 节问题 2） |
| 3 | 短句（不超过 10 有效字符）跳过润色 | 省一次 LLM 往返，收益最大的一条延迟优化 | 少数短句缺标点 |
| 4 | 录音中按停顿切段并即时提交 | 松手时只剩尾巴要识别，等待与录音总时长解耦 | 请求数变多；切点选错会丢半句（靠空段前向合并兜底） |
| 5 | 不做载荷压缩（不上 Opus/MP3） | 实测上传 782KB 仅 196ms，压缩最多省 0.1s，淹没在服务端抖动里 | 无 |
| 6 | 低位钩子传 NULL 模块句柄 + 集中声明 Win32 原型 | ctypes 默认按 32 位解析句柄会截断，曾导致 SetWindowsHookEx 报 126 | 需要一个 winapi.py 集中维护 |
| 7 | VAD 底噪用低分位 P2 而非 P10 | 语音占比常超 90%，P10 会把语音当底噪，整段判成静音 | 极安静环境下需靠绝对下限兜底 |
| 8 | 演示模式与真实路径共用流水线 | 没有麦克风也能验证真实模型与交互，且不会出现假路径与真路径行为分叉 | DemoRecorder 要维护两套音频来源 |
| 9 | 识别与润色分两个密钥槽 | 换厂商时只改一个槽 + endpoint | 需要用户理解两个槽的含义 |
| 10 | 界面与服务分进程，服务启停放在 `ui` 模块 | 关掉界面服务照常跑；`start` 与界面复用同一份启停逻辑 | cli 依赖 `ui` 模块（可日后抽到 service.py） |

## 6. 性能与延迟基线（2026-09-25 实测）

样本：voice_test/testvoice.mp3（25.03s）。完整拆解与依据见 docs/LATENCY.md。

| 环节 | 实测 | 占比 |
|---|---|---|
| 本地解码 + 编码 | 约 40 ms | 不到 2% |
| 上传（782 KB） | 196 ms（约 4 MB/s） | 5% |
| **服务端排队 + 识别** | **约 3.3-3.4 s** | **88%** |
| 回程 | 小于 1 ms | 0% |
| 润色（125 字） | 约 1.5-2.0 s | 单独一项 |

关键规律：识别耗时约等于 300-850 ms 固定开销 + 100-165 ms × 音频秒数；同一输入重复测有正负 30% 抖动。

SLO：松手 → 出字 P50 不超过 1.0 s，P95 不超过 1.5 s。

0.0.5 实测（18 s 演示语音、3 段并发）：release2text 2.6-3.2 s，其中尾巴识别约 1.1 s、润色 1.2-1.5 s——**润色已成为交互延迟的主要瓶颈**，对应 P0 第 3 项。
任何改动后用 `python tools/bench_latency.py` 复测，与本基线对比。

## 7. 测试与验证

### 7.1 单测（67 项，离线，不碰网络 / 麦克风 / 窗口）

```
python -m pytest -q
```

| 文件 | 覆盖 |
|---|---|
| tests/test_core.py | VAD 停顿检测与语音占比、切段（命中停顿 / 最短长度 / 跳过上次空切点）、路由熔断与兜底、词库纠正与编辑学习、短句跳过模型润色但仍走词库纠正、长文本润色、术语润色前置纠正与命中计数、润色失败保留纠正、润色超时回落原文、ASR 错误传播、语言口令与有效字数 |
| tests/test_interaction.py | 演示录音器（样本长度与电平回调）、RMS 电平缩放、HUD 状态覆盖完整性、配置往返（含候选链）、auto 路由换模型并熔断、单模型不重试、并发分段提交与顺序回收、分段原地重试、常开流预缓冲与裁剪、live_commit、401/403 错误分类、坏 Key 不熔断、尾巴失败回落已提交分段、失败分段松手重发、不出数据时的退避序列、静音时长跨重开、按下即重开、空结果区分设备无数据 |
| tests/test_overlay.py | HUD 状态色/光晕覆盖、弹簧入场过冲与消失曲线单调、逐字淡入前缀复用、超长文本保留尾巴、玻璃胶囊 alpha 与阴影、光晕随状态配色、预乘 BGRA 契约、分层窗口 blit 冒烟、收起后不再重弹（序号制）、收起期间来新一轮要能再弹、真窗口端到端保持隐藏、字体栈优先思源黑体、拉丁走 Segoe UI、中英共用基线、正文档位与两行版式约束、折行行宽不越界 |

### 7.2 测量与诊断脚本（tools/，不属于运行时）

| 脚本 | 用途 |
|---|---|
| bench_latency.py | 多轮 P50/P95 基准，批量路径 + 切段粒度对比（**发版前必跑**） |
| latency_probe2.py | 分离首字节与回程，拟合「固定开销 + 每秒音频」 |
| upload_probe.py | 手工构造 multipart，分离上传与服务端耗时 |
| live_sim.py | 模拟边录边发，比较不同切段粒度的松手等待 |
| e2e_check.py | 端到端：系统 TTS → 识别 → 润色 |
| polish_check.py | 润色改写效果与耗时 |
| `verify_usage.py` | 核对模型 ID、账户接口、响应头 trace id |
| key_check.py | 核对两个密钥槽指纹（不打印明文） |
| ui_check.py | 构建设置窗口做冒烟（不进消息循环） |
| mic_probe.py / mic_probe2.py | 枚举音频设备与 PortAudio 后端 |
| mic_check.py | 录 2 秒自检：自动选中哪台设备、实际采样率、峰值，末尾给 RESULT |

### 7.3 提交前手工验收清单

1. `doctor` 五项正常（Python、音频后端、密钥、provider、热键）
2. `pytest -q` 全绿（67 项）
3. `run --demo --selftest` 状态流转为 recording → finalizing → done 且文本非空
4. `transcribe <音频>` 文本正确，结果落到 records/last_transcript.txt
5. `ui` 能打开、服务状态显示运行中、改一项配置保存后 `config` 能看到变化
6. 麦克风可用时：按住热键 → HUD 出现（电平条随音量起伏）→ 文字落进记事本（2026-09-26 已通过：USB 麦克风 K11/K12）

## 8. 已知问题与限制

| # | 问题 | 影响 | 现状与规避 | 建议修法 |
|---|---|---|---|---|
| 1 | 板载 Realtek 麦克风插孔未接入（端点状态 UNPLUGGED），本机 PortAudio 只在 WDM-KS 层枚举到它，而 WDM-KS 打不开（PaErrorCode -9999） | 板载麦克风无法录音 | 已改用 USB 麦克风（K11/K12），自动选中 WASAPI，见 2.1 与第 14 节 | 若必须用板载：修好 Windows 录音设备与插孔检测，或换采集后端（WASAPI 直连 / soundcard） |
| 2 | 润色 1500 ms 硬截止与长文本不匹配（实测 125 字三轮全部回落原文） | 长句几乎拿不到润色 | 批量 `transcribe` 路径已放宽到 8 s | 截止随字数缩放，或渐进式修正 |
| 3 | 服务端不提供流式识别（整段接口） | 首字延迟 = 整段识别时间 | 用切段提交近似流式 | 切更细 + 并发提交（P0） |
| 4 | 服务端耗时抖动正负 30% | P95 难保证 | 无法从客户端消除 | 单段越短，抖动绝对值越小 |
| 5 | 无 UIA 焦点上下文 | 无法识别密码框 / 选区，只能按进程名排除 | privacy.exclude_apps 兜底 | 加 UIA 查询（P2） |
| 6 | 无 TSF 文本服务 | 提权窗口（UIPI）与独占程序无法注入 | 降级为剪贴板 + 提示手动粘贴 | TSF（长期） |
| 7 | 无托盘图标 / 开机自启 | 每次需手动 `start` | `run_typefast.bat start` | 托盘 + 注册表自启（P2） |
| 8 | 无历史记录与录音留存（privacy.keep_audio 目前只是开关占位） | 无法回看与重新转写 | 无 | SQLite + DPAPI 加密（P2） |
| 9 | 词库学习未接投递后校验 | 用户改错不会自动学习 | 只能手工加词 | 接 learn_from_edit |
| 10 | 超时的润色请求照样计费 | 白花钱 | 无 | 见问题 2 |
| 11 | 切段削弱音频上下文：2.5-3.5 s 切段出现同音字错误（相似度 0.9912），5.0/6.0 s 与整段一致（1.0000）；2026-09-26 复测更严重——3.05 s 的尾巴片段把「举头望明月」整句丢掉，而同一段音频整段识别完整 | 短片段会整句丢内容，不只是同音字 | 新增 segment.live_commit（false = 不边录边切段，整段送识别）；min_commit_s 默认提到 5.0 | 要用更小粒度得靠接口的 prompt 参数回灌上文（该接口接受 prompt 字段，效果待验证） |

| 12 | 本机 USB 麦克风（K11/K12）时基偏差：声明 48000 Hz，host 侧实测只交付约 36.2k 样本/秒（0.755 倍；MME / DirectSound / WASAPI / ffmpeg DirectShow 四条通路一致，连续 6 秒无回调状态标志）；另有偶发整段 0 数据流（5 次开关循环里 1 次） | 音频被压快约 1.35 倍送识别；偶发「按了没反应」 | 交付率只作诊断，换算仍按标称率（变速对照证明拉伸方向错误）；0 数据流已由常开流 + 看门狗规避（实测连续 4 次按键 4/4 有音频） | 实现 audio.keep_warm / preroll_ms 常开流 + 预缓冲；或更换 USB 麦克风 |
| 13 | 2026-09-26 16:52:06 起 USB 耳麦 K11/K12 整条端点卡死：MME / DirectSound / WASAPI 三路采集全部 0 回调，WASAPI 播放端点报 `Invalid device`（板载 Realtek 输出正常）。停掉 typefast 后用独立进程（sounddevice 直连、ffmpeg DirectShow、我自己的探针）复现同样结果，且发生时跑的还是 0.0.6，故判定为设备/驱动层卡死而非代码问题 | 语音输入完全不可用：按下只录到 0 秒，HUD 最终显示「没听到内容」；后台日志每 3 秒刷一次 `warm stream stalled` | 0.0.8 起：退避重开 + 按下即探活 + 重开失败明确报错（不再静默）。恢复设备三步：① 拔插一次 USB 耳麦 / 关掉再打开耳麦电源；② 设备管理器里禁用再启用「K11/K12」；③ 重启 Windows 音频服务（`Restart-Service Audiosrv -Force`，需管理员） | 自检 `python tools/mic_check.py`（末尾 RESULT: OK/FAIL，0 样本即 FAIL）。本机复测：板载 Realtek WDM-KS 输入**能**交付数据（插孔未接入，peak≈0），K11/K12 三路全 0 回调，说明音频栈本身正常、只有这一台 USB 设备卡死 |

## 9. 下一版本迭代计划

### P0：延迟优化（预计 2-4 天，收益最大）

| 任务 | 预期收益 | 验收标准 |
|---|---|---|
| 切段与并发（**0.0.5 已完成**）：最终采用 5.0 s 目标 + 0.30 s 停顿门限 + 最多 3 段并发，未采用 2.5 s | 长语音墙钟 4.2 s → 3.1 s；松手等待取决于尾巴 | 端到端自检：18 s 语音 release2text 2.6-3.2 s（尾巴识别约 1.1 s + 润色 1.2-1.5 s） |
| 润色预算随文本长度缩放（如 max(1200, 25 ms × 字数)） | 长文本润色成功率 0% → 100% | 125 字文本 P95 落在预算内且润色成功 |
| 边录边润色：已提交段落后台先润色，松手只润尾巴 | 松手 → 出字 P50 不超过 1.0 s | `run --demo --selftest` 的 release2text P50 不超过 1.0 s |

### P1：可观测与体验（约 1 周）

| 任务 | 说明 |
|---|---|
| 逐阶段计时写进日志与本地 JSONL | 每次运行都能看到 encode / TTFB / 回程 / 润色，字段已在 Timings 里 |
| bench_latency 做成回归门禁 | 固定样本 + 阈值，超了就别发版 |
| 渐进式修正 | 先落原文，润色回来后若焦点未变则原地替换（需 UIA 校验插入点） |
| 首次启动引导 | 检测麦克风可用性、自动降级演示模式、引导填密钥 |

### P2：能力补齐（约 2-3 周）

| 任务 | 说明 |
|---|---|
| 流式识别 | 厂商若提供 WebSocket 增量识别就用；否则把切段做到 1-1.5 s + 增量拼接 |
| 托盘图标 + 开机自启 + 双击常开模式 | 目前只有按住说话，且必须手动 `start` |
| UIA 焦点上下文 | 密码框拦截、按应用排除、选区替换 |
| 本地模型兜底 | sherpa-onnx 识别 / llama.cpp 润色，作为离线模式 |
| 历史记录 | SQLite + DPAPI 加密，支持重新转写与导出 |
| TSF 文本服务（长期） | 真正的输入法级体验：内联合成、任意应用可用 |

### 每次改动都要做的三件事

1. 跑 `pytest -q` 与 `python tools/bench_latency.py`，把新数字写回 docs/LATENCY.md
2. 更新 CHANGELOG.md，补丁号加一（如 0.0.5 → 0.0.6）
3. 若改了配置项，同步 config.example.json 与设置界面

## 10. 扩展指南

### 10.1 加一个识别模型

1. 在 providers/ 新建模块，实现 name 与 `transcribe`(AsrRequest) -> AsrResult
2. 错误统一抛 ProviderError(ErrorKind.X, 人话说明, 原始信息)；无语音要抛 NO_SPEECH（上层会静默收起）
3. 在 AsrResult 填 encode_ms / ttfb_ms / download_ms（性能回归依赖它）
4. cli.build_asr 注册；`ui` 的 ASR_PROVIDERS 与 ASR_MODELS 加选项；需要新密钥就到 KEY_FIELDS 加槽位

### 10.2 加一个润色模型或换厂商

优先复用 providers/openai_compat.py：只改 `config` 的 polish.endpoint 与 polish.model 即可。
若厂商不是 OpenAI 兼容，再新建 provider 实现 polish(PolishRequest) -> str，并在 cli.build_polisher 注册。

### 10.3 加一条投递通道

platform/delivery.py 里加实现，TextDelivery.deliver 按「前台应用 + 文本长度 + 能力探测」选择通道；新通道要有失败降级路径。

### 10.4 加一个配置项（务必四处同步）

1. settings.py 的对应 dataclass 默认值（新分组还要加进 from_dict 的分组列表）
2. config.example.json
3. ui/settings_window.py 的 _build_*（加控件）
4. ui/settings_window.py 的 _collect（读回值）——漏掉这步界面一保存就会把该值重置

### 10.5 调延迟

只动 segment 分组（target_commit_s 最有效）与 polish.deadline_ms，然后用 bench_latency 验证；不要动投递与采集链路，它们占比不到 2%。

## 11. 编码规范与协作约定

- 全部公开函数带类型注解；数据用 dataclass；不要用可变对象做默认值
- 错误分层：Provider 抛 ProviderError，core 决定重试/降级，UI 只展示人话
- 日志不打印密钥；正文内容仅在显式需要时打印
- 注释与文档用中文，面向用户文案用中文
- 硬性禁止：把 Key 写进代码或配置；在 core 里 import platform
- 新增纯逻辑（切段、解析、路由、配置）必须有单测
- 一次提交一个功能，同步 CHANGELOG

## 12. 故障排查速查

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 按住热键没反应 | 钩子未装上 / 键名不对 | 日志看 hooks installed；核对 hotkey.hold |
| HUD 不出现 | Pillow 没装 / HUD 初始化失败 | 日志看 `HUD ready` 或 `HUD disabled`；`pip install pillow` |
| HUD 收起后又反复弹入弹出 | 显示/收起状态机把终态当成「该显示」 | 0.1.0 已修（序号制：收起后要等新的状态/文本才再弹），见 CHANGELOG 0.0.9 的「HUD 收起后反复弹入弹出」 |
| 麦克风打不开 | 无 WASAPI/MME 设备（只有 WDM-KS） | 换 USB 麦克风；`python tools/mic_check.py` 看能不能采到样本 |
| 按住热键 HUD 电平条不动 | 选错设备 / 麦克风静音 / 系统隐私拦截 | `python tools/mic_check.py` 自检；设置窗口换输入设备 |
| 日志出现 16k open failed ... -9997 | WASAPI 设备原生只支持 48k | 正常：自动回落设备默认率再重采样，不是故障 |
| 报 not_configured | 对应密钥槽为空 | `typefast key status` / `key set` |
| 400 Model does not exist | 模型名写错 | `verify_usage.py` 查 /v1/models 列表 |
| 403 Model disabled / 额度不足 | 模型下线或余额不足 | 换模型；auto 路由会自动熔断切换 |
| 润色总是回落原文 | 截止太短（长文本） | 日志看 deadline；调 polish.deadline_ms |
| 文字没进目标窗口 | UIPI / 非文本控件 | 手动 Ctrl+V（内容已在剪贴板） |
| 剪贴板被清空 | 还原逻辑异常 | 检查 delivery.restore_clipboard |
| 日志每 3 秒刷 `warm stream stalled`，按下去录到 0 秒 | 麦克风整条端点卡死（USB 掉线 / 断电 / 驱动异常），不是代码问题 | `python tools/mic_check.py` 确认是不是 0 样本；拔插麦克风，或设备管理器禁用再启用该设备，或重启 Windows 音频服务；恢复前可先用 `run --demo` 演示模式 |
| 按下后 HUD 报「麦克风打不开：设备不交付音频」 | 常开流已不出数据且重开失败 | 依上一条恢复设备；插好后服务会自动重连 |
| 松手后 HUD 报「麦克风没有数据（可能被拔掉、断电或驱动卡死）」 | 整场录音一个回调都没来：设备在枚举表中但不再交付数据（第 13 条那种卡死） | 依上一条恢复设备；这句提示本身就说明问题在设备侧，不是识别或网络 |

## 附录 A：API 调用对账

开发期间累计约 89 次识别调用（约 9 分钟音频）与约 20 次润色调用。明细与 trace id 见 docs/LATENCY.md 附录。

## 附录 B：许可与来源

- 架构参考了 typefree（macOS 版，GPL-3.0）的公开设计：会话流水线、录音中切段提交、模型路由、词库分层
- 本项目是独立实现，未复制其源码；将来若移植其任何代码，必须遵守 GPL-3.0 并开源
- Typefree 名称与图标不在开源许可范围内，不得用于本项目发布
- 本项目自身许可待定

## 13. 0.0.5 迭代记录：切段粒度与并发提交

### 13.1 为什么最终没把切段降到 2.5 s

用 voice_test/testvoice.mp3（25.03 s）对比「整段识别」与不同切段粒度的结果。整段重复跑两次得到逐字相同的文本（相似度 1.0000），说明该模型对同一输入是确定性的，差异可归因于切段。

| 切段粒度 | 段数 | 与整段参考的相似度 | 差异 | 标点（原样 → 清理后） |
|---|---|---|---|---|
| 整段（参考） | 1 | — | — | 2 句号 / 8 逗号 |
| 6.0 s（旧默认） | 4 | 1.0000 | 0 | 4 / 8 |
| 5.0 s + 0.30 s 门限（新默认） | 5 | 1.0000 | 0 | 5 / 7 → 1 / 11 |
| 3.5 s + 0.35 s 门限 | 8 | 0.9912 | 1 处同音字 | 8 / 4 → 1 / 11 |
| 2.5 s + 0.22 s 门限 | 12 | 0.9912 | 1 处同音字 | 11 / 1 → 1 / 12 |

结论：这个模型需要约 5 秒音频上下文；切到 3.5 s 以下在样本上出现同音字错误（雨 → 如）。标点碎片化是结构性问题（每段都以句末标点收尾），与粒度无关，已在拼接阶段修掉。

### 13.2 并发提交的实现要点

- 两个游标：committed_index（已确认识别并写入 utterance）与 planning_index（已提交识别）。规划用后者，回收推进前者。
- 只有队首分段完成才推进 committed_index，否则乱序完成会导致音频被跳过。
- 分段失败：可原地重试的错误（server_busy / timeout）重试一次；仍失败则记空并推进，日志留痕。
- 松手时若仍有分段在途：把它们之后的音频并回尾巴重发（保守，绝不丢内容）。
- 拼接用 clean_join：非末段去掉句末标点并接逗号，末尾统一一个句号。

### 13.3 待验证的下一步

- 用接口的 prompt 字段回灌上一段文本，看能否在小粒度切段下保住准确率（接口接受该字段，但效果尚未验证）。
- 演示语音已换成三句话（约 18.7 s），可用 run --demo --selftest --seconds 18 复现本节数据。

## 14. 真实麦克风接入验证（2026-09-26）

### 14.1 背景

0.0.5 及以前，本机只有 WDM-KS 层能看到输入设备，而 WDM-KS 在当前构建里打不开，所以真实录音一直不可用，`start` 会自动降级为演示模式（系统 TTS 合成语音）。

### 14.2 复测结果

接入 USB 麦克风 K11/K12 后，四条互相独立的通道全部能看到它：

| 检查通道 | 结果 |
|---|---|
| PortAudio MME | 麦克风 (K11/K12)，16k 直接打开 OK |
| PortAudio DirectSound | 麦克风 (K11/K12)，16k 直接打开 OK |
| PortAudio WASAPI | 麦克风 (K11/K12)，只支持 48000 Hz（16k 报 -9997） |
| ffmpeg DirectShow | 麦克风 (K11/K12)（0.0.5 之前这条通道枚举不到任何音频输入） |
| 板载 Realtek 麦克风 | 端点 UNPLUGGED，WDM-KS 打开报 -9999，仍不可用 |

自动选设备走 pick_input_device 的 WASAPI > DirectSound > MME 优先级，选中 #15；Recorder 先试 16k 失败，再回落设备默认率 48000 Hz，最后重采样到 16k。

### 14.3 实测数据

```
auto-selected input device #15 (麦克风 (K11/K12    ))
16k open failed (Error opening InputStream: Invalid sample rate [PaErrorCode -9997]), fallback to device default
device rate: 48000 Hz
samples: 25120 (expect about 32000)
peak 0.0239  rms 0.0047
RESULT: OK
```

2 秒录音在 48 kHz 下采到 25120 个样本（16k 直开的话约为 32000），重采样后交给流水线的仍是 16k 单声道。

### 14.4 切换与复现步骤

1. 停掉正在跑的演示模式服务（设置窗口「停止服务」，或结束命令行里带 --demo 的那个 python 进程）。
2. 重新跑 `run_typefast.bat start`：输入设备名里出现 wasapi 关键字后就不再降级演示模式。
3. 确认新服务的命令行是 `python -m typefast run`（不带 --demo），日志里没有 demo mode 行。
4. 按住右 Ctrl 说话：HUD 电平条随音量起伏，松手后文字落到光标处。

### 14.5 待办

- 真实麦克风链路的 release2text 基准还没测：docs/LATENCY.md 现有数字来自演示语音与音频文件，需要用真实语音补一次 bench_latency。
- 板载 Realtek 插孔若修好，需要在同一台机器上复测设备序号（板载序号会比 USB 麦克风小，但自动挑选仍优先 WASAPI）。