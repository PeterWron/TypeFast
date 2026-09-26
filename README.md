# TypeFast 0.1.0

Windows 上的 AI 语音输入法（云优先）：按住热键说话，松开后文字已经整理好并落在光标处。

当前状态：**0.1.0 稳定可用**——云端模型已联调（硅基流动识别 + Qwen 润色），真实麦克风端到端跑通并修掉 5 个真实故障（采样率记账、时基换算、低电平漏字、短片段丢内容、偶发空流），支持中英混说；0.0.8 复查还原了 0.0.7 混进的手误、给「麦克风不出数据」加上退避与明确提示，0.0.9 按 Apple 级规范把 HUD 重做成毛玻璃胶囊浮窗，0.1.0 修掉收起后反复弹入弹出并换成思源黑体 + 18px 交互正文，67 项单测通过。

## 快速开始

```
cd E:/py/typefast
run_typefast.bat doctor      :: 检查 Python / 麦克风 / 密钥 / 配置
run_typefast.bat simulate    :: 不录音，用合成音频跑一遍完整流水线
run_typefast.bat run         :: 开始监听：按住 Right Ctrl 说话，松开出字
run_typefast.bat run --demo  :: 演示模式：不用麦克风，用合成音频演示完整交互
run_typefast.bat ui          :: 打开图形操作界面（模型 / 密钥 / 按键 / 服务）
run_typefast.bat start       :: 一键启动：服务（自动判断麦克风）+ 设置窗口
run_typefast.bat transcribe 音频文件 :: 转写音频文件（mp3 / wav / m4a），输出文字
```

建议顺序：`doctor` 通过后再 run。第一次 run 会弹系统麦克风权限。

## 麦克风（真实语音输入）

按住热键时程序自己挑输入设备：优先 WASAPI，其次 DirectSound / MME；WDM-KS 因为打不开会被跳过。系统默认输入设备为 -1（未配置）时也能正常工作。

自检两条命令：

```
python tools/mic_probe.py    :: 列出所有输入设备，并逐个试开 16k / 设备默认采样率
python tools/mic_check.py    :: 录 2 秒，打印选中设备、实际采样率与峰值，末尾给 RESULT: OK / FAIL
```

2026-09-26 本机实测（USB 麦克风 K11/K12）：

| 设备 | Host API | 结果 |
|---|---|---|
| 麦克风 (K11/K12) | MME | 16k 直接打开 OK |
| 麦克风 (K11/K12) | DirectSound | OK |
| 麦克风 (K11/K12) | WASAPI | 只支持 48000 Hz（用 16k 打开报 -9997），程序自动回落到设备默认率并重采样到 16k |
| 麦克风 (Realtek HD Audio Mic input) | WDM-KS | 打开即报 -9999，不可用（板载插孔状态为未插入） |

如果自动挑错了设备，在设置窗口「按键与音频 → 输入设备」里填设备序号（等同配置 audio.input_device，例如 15），保存后重启服务生效。

麦克风默认走常开流（audio.keep_warm）：服务启动就打开设备并保留 300ms 预缓冲，避免偶发「按了没反应」（本机实测约 20% 的按次会在打开后整段不出数据），也不丢按下瞬间的第一个音。不想让麦克风常驻占用，可在设置窗口「按键与音频」页关掉它。

设备真的掉线时（拔了、断电、驱动卡死）行为也明确了：常开流连续拿不到数据时前 3 次立即重开、之后按 5／10／20／30 秒退避，不再每 3 秒无脑开关设备；按下说话时如果发现流已经不出数据会先重开，重开失败 HUD 直接报「麦克风打不开：设备不交付音频」；如果整场都没收到任何音频，松手后会报「麦克风没有数据（可能被拔掉、断电或驱动卡死）」而不是「没听到内容」，不会再静默录到 0 秒。自检用 `python tools/mic_check.py`。



## 依赖

本机 Anaconda Python 3.11 只差一个音频库：

```
python -m pip install sounddevice
```

requests / numpy / psutil / keyring / pytest 当前环境已具备，完整清单见 requirements.txt。

## 目录结构

- src/typefast/core/ — 平台无关引擎：会话状态机、VAD、分段、路由、词库、润色编排
- src/typefast/providers/ — ASR 与润色 Provider（硅基流动 / 火山引擎 / OpenAI 兼容 / mock）
- src/typefast/platform/ — Windows 适配层：全局热键、WASAPI 采集、文本投递、浮窗
- tests/ — 离线单测（VAD / 分段 / 路由 / 词库 / 流水线）
- docs/ — 架构、路线图与延迟实测
- DEVELOPMENT.md — 开发文档：架构、关键技术决策、扩展指南与迭代计划

## 默认行为

| 项目 | 默认值 |
|---|---|
| 按住说话 | Right Ctrl（鼠标中键同样可用） |
| 取消本次 | 按住时按 Esc |
| 短句跳过润色 | 不超过 10 个有效字符 |
| 润色硬截止 | 1500 ms，超时回落到 ASR 原文 |
| 输入语言 | 中英混说（zh-en，设置界面可改）：识别端自动判语种，润色保留中英原样、不翻译，中英相邻处补空格 |
| 分段提交 | 目标 5.0s / 最短 3.0s / 活边缘留 1.2s / 最多 3 段并发 |
| 投递方式 | 短文本 SendInput Unicode，长文本剪贴板粘贴（不进剪贴板历史） |

## 模型与密钥

1）存密钥到 Windows 凭据管理器（注意先切到 src，或把项目 pip install -e .）：

```
cd E:/py/typefast/src
python -c "from typefast.settings import set_secret; set_secret("polish_api_key", "你的Key")"
python -c "from typefast.settings import set_secret; set_secret("asr_api_key", "你的Key")"
```

2）改项目根目录的 config.json（可从 config.example.json 抄；想换地方就设环境变量 TYPEFAST_CONFIG）：

```json
{"asr": {"provider": "volcengine_flash"}, "polish": {"provider": "openai_compat", "model": "qwen-flash"}}
```

或者用火山当识别、百炼当润色，两边各自配 key：火山用 volc_app_key + volc_access_key，百炼用 asr_api_key / polish_api_key。

3）
run_typefast.bat `doctor` 确认密钥与 provider 就位。

云端接口字段以官方文档为准（会变），核对点写在 src/typefast/providers/ 下两个文件顶部注释里。

## 已知限制（0.1.0）

- 只有按住说话，没有「双击常开」模式
- 没有 UIA 密码框识别，目前只按进程名排除（privacy.exclude_apps）
- 没有 TSF：独占 / 提权窗口无法注入，会退化成「已复制到剪贴板」
- 本地模型（llama.cpp / sherpa-onnx）尚未接入，目前只有云端路径（在路线图 M2）

- 设置界面还没有托盘图标与开机自启（在路线图 P2，见 DEVELOPMENT 第 9 节）
- 板载 Realtek 麦克风插孔未接入时不出现录音端点（本机长期如此：端点状态 UNPLUGGED，只有 WDM-KS 层可见且打不开），此时用 USB 麦克风，或先修好 Windows 录音设备

## 与 typefree 的关系

架构参考了 typefree（macOS 版，GPL-3.0）的公开设计：会话流水线、录音中分段提交、模型路由与词库分层。
本项目是独立实现，未复制其源码；将来若移植其任何代码，必须遵守 GPL-3.0 并开源。

## 图形界面（run_typefast.bat `ui`）

一个窗口管完所有设置，改完点「保存配置」：

- 模型：转写服务（mock / 火山极速版 / OpenAI 兼容）、模型名、接口地址、识别语言；润色服务、润色模型（下拉里有 auto 自动路由）、截止时间、短句阈值、temperature、max_tokens
- API Key：四个凭据位（火山 App Key / Access Key，兼容接口的识别 Key / 润色 Key），只写入 Windows 凭据管理器，界面只显示「已配置 / 未配置」，不回显明文
- 按键与音频：按住键、取消键、鼠标中键开关、输入设备（可点「列出可用设备」）、后台服务的启动 / 停止 / 打开日志
- 投递与隐私：短文本直接键入的阈值、剪贴板还原、聊天类去句末句号、排除的应用清单
- 连通性测试：测试润色（打一次真实接口）、测试识别（合成音频或麦克风 3 秒）

## 交互效果与演示模式

按住热键时屏幕下方浮出一条 Apple 风格的毛玻璃胶囊 HUD（不抢焦点、也不挡点击）：左侧是带彩色光晕的麦克风与电平波形，右侧是状态行（聆听中 / 识别中 / 整理中 / 已输入 / 出错）和实时转录文本；出现与收起走弹簧曲线，转录文本逐字淡入，松手后 1.4 秒自动收起。

HUD 用 Pillow 渲染（`pip install pillow`，或 `pip install .[hud]`），没装会自动禁用、不影响语音输入（服务进程不再需要 tkinter）。默认是暗色玻璃；想用浅色玻璃或跟随 Windows 应用主题，设环境变量 `TYPEFAST_HUD_THEME=light` 或 `auto`。

字体按优雅程度自动挑：中文优先思源黑体家族（Noto Sans SC，装了就用），拉丁字符单独走 Segoe UI（中英混排比雅黑的拉丁字形精致），都没有再回落微软雅黑 UI / 微軟正黑體 UI / 等线；中英混排共用一条基线。交互正文（转录文本）18px，状态行 13px，提示 12px。设置窗口用同一套字体。

不想占用麦克风（或临时没有可用输入设备）时可以直接用演示模式：合成音频走同一条流水线，交互与真实录音一致：

```
run_typefast.bat run --demo
```

演示模式下按住 Right Ctrl：HUD 显示电平起伏与识别预览，松开后文字落到光标处。想先看交互又不希望文字真的落进窗口，加 --no-deliver；想跑一次自动自检（不装钩子、不注入，只打印状态流转）用 `run --demo --selftest`。

## 许可

内部项目，许可待定。

## 默认模型：硅基流动（SiliconFlow）

| 用途 | 默认配置 | 说明 |
|---|---|---|
| 语音转写 | provider=siliconflow，model=XingChenAGI/XingChenASR-V3.2-Ultra | POST /v1/audio/transcriptions，multipart 传 16k 单声道 wav |
| 文字润色 | provider=openai_compat，model=Qwen/Qwen2.5-7B-Instruct | POST /v1/chat/completions（OpenAI 兼容） |

实测（2026-09-25 本机）：5.44s 音频识别 2.2s（含网络往返），润色 0.6-1.0s，识别结果正确。

密钥只进 Windows 凭据管理器，命令行也能设：

```
cd E:/py/typefast/src
python -m typefast key set siliconflow_api_key "###替换成你的api"   :: 转写用
python -m typefast key set polish_api_key "###替换成你的api"        :: 润色用（同一个 Key 也行）
python -m typefast key status                            :: 看哪些已配置
```

换模型不用改代码：转写与润色的 provider / model / endpoint 都在设置窗口里改，保存后重启服务生效。想换回火山引擎或别家的 OpenAI 兼容接口，只改 endpoint 与 model 即可。

## 转写音频文件

```
cd E:/py/typefast/src
python -m typefast transcribe E:\py\typefast\voice_test\testvoice.mp3
```

任意格式（mp3 / m4a / wav / flac）先经 ffmpeg 解码成 16k 单声道，再用与实时链路**同一套**分段策略切成若干段并行识别，最后润色，结果同时写到 数据目录/records/last_transcript.txt（数据目录默认是项目内的 data/，可用 TYPEFAST_DATA 改）。

离线批量没有实时延迟压力，所以这条路径给润色的预算放宽到至少 8 秒（实时路径仍是 1500ms 硬截止）。实测 25 秒音频：4 段并行识别 3.6s + 润色 2.0s。
