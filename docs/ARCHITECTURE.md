# TypeFast 架构（0.0.1）

## 分层

```
platform/   ← 只有这里碰 Windows API
  hotkey.py    低级键鼠钩子（按住说话）
  audio.py     WASAPI 采集 + 16k 回落重采样 + snapshot()
  delivery.py  SendInput / 剪贴板粘贴 / 剪贴板保护 / 前台进程
  overlay.py   不抢焦点的状态浮窗
  winapi.py    集中声明 Win32 函数原型（避免 64 位句柄被截断）

core/       ← 平台无关，可单测，可整体替换实现
  contracts.py  数据契约（SessionState / Utterance / ErrorKind / Timings）
  vad.py        能量 VAD：底噪 P2 + 峰值比例兜底 → 停顿候选
  segmenter.py  CommitPlanner：录音中切段策略（纯计算）
  pipeline.py   ASR → 术语纠正 → 润色（硬截止）→ 形态整理
  router.py     模型候选链 + 额度熔断
  vocabulary.py 热词 / 术语纠正 / 从编辑学习
  session.py    会话编排（tick 线程 + 工作线程池）

providers/  ← 模型接入，按注册名创建
  base.py           AsrProvider / PolishProvider 协议
  mock.py           离线 mock（自检与单测）
  openai_compat.py  百炼 / 方舟 / OpenAI 兼容（识别 + 润色）
  volcengine.py     火山极速版 flash 识别
```

依赖方向单向：platform → core → providers 协议。providers 实现只依赖 core.contracts 与 settings。

## 一次会话的数据流

```
按下 Right Ctrl
  └─ 钩子回调（只置事件）→ session._on_press → recorder.start() + 新建 Utterance

录音中（tick 每 300ms）
  ├─ recorder.snapshot() → CommitPlanner.find_cut()
  ├─ 命中停顿 → 提交该段识别（线程池），浮窗显示预览
  └─ 该段为空/失败 → 不推进 committed_index（音频并入下一段，绝不丢内容）

松开
  ├─ recorder.stop() → 尾部样本
  ├─ 等在途分段（最多 6s）→ 识别尾巴
  ├─ joined_raw() → 术语纠正 + 润色（硬截止 1500ms，超时/失败回落原文）
  ├─ shape_for_delivery()（聊天类应用去掉句末句号）
  └─ TextDelivery.deliver()：短文本 SendInput；长文本 剪贴板 + Ctrl+V + 还原
```

## 延迟设计（为什么松手不用等很久）

| 机制 | 作用 |
|---|---|
| 录音中按停顿切段提交 | 松手时只剩尾巴（目标不超过 3.5s 音频）要识别 |
| 短句跳过润色 | 不超过 10 个有效字符直接出字，省一次 LLM 往返 |
| 润色硬截止 + 回落 | 最坏情况也直接出 ASR 原文，不阻塞出字 |
| 润色在后台线程 | 不同段落可并行、尾巴识别不被润色阻塞 |

SLO（建议进 CI 卡回归）：松手 → 出字 P50 不超过 700ms，P95 不超过 1500ms。

## 扩展点

- 换模型：实现 AsrProvider / PolishProvider，在 cli.build_asr / build_polisher 注册
- 换 VAD：只改 core/vad.py（保持 pause_candidates 接口）
- 加投递通道（TSF / UIA）：在 platform 新增实现，TextDelivery 按场景选择
- 引擎拆进程：contracts.py 的结构可直接换成 protobuf，边界已清晰
- 提示词版本化：SYSTEM_PROMPT 只在 core/pipeline.py 定义一处，便于加版本号与灰度

## 线程模型

| 线程 | 职责 | 约束 |
|---|---|---|
| 钩子线程 | 键盘 / 鼠标事件 | 回调内只做最小工作并快速返回，否则会被系统摘掉钩子 |
| tick 线程 | 切段判定、在途回收 | 300ms 周期，纯计算 |
| 线程池（4 线程） | 分段识别、润色 | 网络 IO；润色用 future.result(timeout) 实现硬截止 |
| 浮窗线程 | tkinter 消息循环 | 只读队列，不被其它线程直接操作 |

## 0.0.1 明确取舍

- 没有 TSF：注入靠剪贴板 / SendInput，提权窗口与部分游戏不可用
- 没有 UIA：密码框只能靠进程名排除
- provider 字段未联调：mock 闭环，真实接口需核对官方文档
- 无历史存储与加密：只写本地日志文件

## 图形界面与 HUD（0.0.2）

```
ui/settings_window.py   设置窗口（tkinter）：模型 / Key / 按键 / 音频 / 投递 / 隐私 / 服务控制
platform/overlay.py     语音输入 HUD：状态点 + 电平条 + 预览文本（WS_EX_NOACTIVATE 不抢焦点）
platform/audio.py       Recorder（真实麦克风）与 DemoRecorder（合成音频）接口一致，on_level 驱动 HUD
core/session.py         把状态、预览文本、电平实时喂给 HUD；dry_run 只打印不注入
cli.py                  run [--demo|--selftest|--no-deliver|--no-overlay] / ui / simulate / doctor / config
```

设计要点：

- 界面与服务是两个进程：关掉界面服务照常跑，服务用 psutil 按命令行找 PID 实现启停
- 密钥只进 Windows 凭据管理器，配置文件里不落明文
- HUD 与设置窗口各自跑在自己的 tkinter 消息循环线程里，其它线程只往队列投递，不直接碰控件
- DemoRecorder 与 Recorder 接口完全一致（start/snapshot/stop/cancel + on_level），演示模式走的是同一条流水线，不是另一条假路径
