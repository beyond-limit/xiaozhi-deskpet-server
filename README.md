# 小智AI桌宠 · 服务端

## 1. 项目是什么

一只常驻 Windows 桌面的 Live2D 语音桌宠：说一句话，就能让它开关本机软件、找文件、调音量、点歌、设定时提醒。
**本仓库是它的语音服务端**——VAD → ASR → LLM → TTS 全链路、23 个本机操作工具、旁路 HTTP 接口、唤醒词服务。
桌宠客户端（Electron + Live2D）单独存放，见第 6 节。

> 基于 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)（MIT）二次开发：
> 上游提供流水线骨架与插件机制，本仓库在其上加了本机操作工具、旁路 HTTP、配置外置（`.env`）与唤醒词服务。

## 2. 环境与版本

| 项 | 版本 / 取值 | 说明 |
|---|---|---|
| 操作系统 | Windows 10 / 11 x64 | **必须 Windows**：SAPI、pycaw、Office COM、Win32 窗口操作都是系统专有 |
| Python | **3.10.22**（conda-forge） | 3.11+ 装不上 `torch==2.2.2`，别升 |
| 固件 | 不适用 | 本项目是 PC 端应用，不烧写单片机、无 OTA 流程 |
| ROS2 发行版 | 不适用 | 不依赖 ROS / ROS2，没有相关节点 |
| 端口 | 8000 / 8003 / 8006 | 8000 语音 WebSocket、8003 旁路 HTTP（仅回环）、8006 唤醒词调试页 |
| 显卡 | 不需要 | ASR 走 CPU（sherpa-onnx int8），首次启动加载模型 20~60 s |

依赖清单见 `main/xiaozhi-server/requirements.txt`，关键几项：

| 包 | 版本 | 用途 |
|---|---|---|
| torch / torchaudio | 2.2.2 | 上游默认 ASR 运行时（本项目已切 sherpa-onnx，但依赖仍需安装） |
| sherpa-onnx | 1.12.29 | ASR（SenseVoice int8）与唤醒词 KWS |
| openai | 2.8.1 | DeepSeek（OpenAI 兼容接口） |
| pywin32 | 312 | SAPI 兜底 TTS、Word / Excel COM |
| pycaw | ≥20240210 | Windows 主音量 |
| sounddevice / pydub | 0.5.6 / 0.25.1 | 服务端音乐播放 |
| imageio-ffmpeg | ≥0.5.1 | 自带 ffmpeg 二进制，免装系统 ffmpeg |
| yt-dlp | ≥2024.8.6 | 点歌的 B 站兜底音源 |

## 3. 如何启动（从零开始）

> 路径不要含中文与空格（例：`D:\xiaozhi`）。以下命令在**仓库根目录**逐条执行，不需要"先配好环境"。

### 3.1 建 Python 环境（一次性，约 10~20 分钟）

```powershell
conda create -n xiaozhi-esp32-server python=3.10 -y
conda activate xiaozhi-esp32-server
conda install -c conda-forge opus -y          # 提供 opus.dll，音频编解码必需

cd main\xiaozhi-server
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

把 `imageio-ffmpeg` 自带的 ffmpeg 复制进 conda 的 `Library\bin`（`opus.dll` 也在同一目录，程序靠 PATH 找它）：

```powershell
python -c "import imageio_ffmpeg,os,shutil; src=imageio_ffmpeg.get_ffmpeg_exe(); dst=os.path.join(os.environ['CONDA_PREFIX'],'Library','bin','ffmpeg.exe'); shutil.copyfile(src,dst); print('ffmpeg ->',dst)"
ffmpeg -version
```

> 不要用 `conda install ffmpeg`：360 之类的安全软件会把这类静态二进制当可疑文件直接删掉，表现为"昨天能跑今天起不来"。

### 3.2 填 Key 与配置（1 分钟）

```powershell
# 1) 密钥：仓库根目录的 .env（git 忽略，不会被提交）
Copy-Item ..\..\.env.example ..\..\.env
notepad ..\..\.env            # 填 DEEPSEEK_API_KEY；默认 TTS 是火山，VOLC_APPID / VOLC_ACCESS_TOKEN 也要填

# 2) 配置：从脱敏模板复制一份（data/ 下只提交模板，真实配置不入库）
Copy-Item data\.config.example.yaml data\.config.yaml
```

配置里全是 `${DEEPSEEK_API_KEY}` 这种写法，由 `config/env_expand.py` 在启动时从 `.env` 展开，**代码里没有任何硬编码 Key**。
漏填会打印 `[WARN] 配置里的 ${XXX} 没有对应的环境变量`，不会静默变成空串。

### 3.3 下载语音识别模型（一次性，约 890 MB）

```powershell
python -c "from modelscope import snapshot_download as d; d('iic/SenseVoiceSmall', local_dir='models/SenseVoiceSmall')"
```

已带 `models/SenseVoiceSmall/model.pt` 的跳过这步。模型目录在 `.gitignore` 里，不进仓库。

### 3.4 启动服务端

```powershell
$env:PYTHONUTF8=1             # 中文日志不串码
python app.py
```

看到 `Websocket Server is running on 0.0.0.0:8000` 即启动成功（首次加载模型 20~60 s 属正常）。

常见报错：

| 报错 | 原因 | 处理 |
|---|---|---|
| `检测到 ffmpeg 无法正常运行` | 3.1 的复制命令没执行，或被杀软删了 | 重跑那条 `imageio_ffmpeg` 复制命令 |
| `Port 8000 is occupied` | 上一次 `app.py` 没退干净 | `Stop-Process -Id <pid> -Force`（PID 见端口占用提示） |
| 合成阶段鉴权失败 | `.env` 里火山两项没填 | 填上，或把 `selected_module.TTS` 改回 `SapiLocal` |

### 3.5 验证（不用麦克风、不用客户端）

另开一个 PowerShell（保持 3.4 的窗口在跑）：

```powershell
conda activate xiaozhi-esp32-server
cd main\xiaozhi-server
python tools\smoke_ws.py
```

把一句话直接投进链路，期望输出 `==== 冒烟结论: 通过（收到服务端音频） ====`。这一轮会真实调用 LLM 与 TTS（按量计费）。

### 3.6 可选：唤醒词调试页

```powershell
cd main\digital-human
python start.py
```

打开它打印的 `http://localhost:8006/index.html`，可以在浏览器里试唤醒词（KWS 模型在 `wakeword_runtime/models/`）。
桌宠客户端正常情况下会自己拉起这个服务，不需要手动起。

## 4. 核心目录说明

```
xiaozhi-esp32-server/
├── .env.example                 密钥模板（复制成仓库根 .env；真实 .env 已被 git 忽略）
├── .gitignore                   排除模型权重、data/ 运行态、虚拟环境、日志等
└── main/
    ├── xiaozhi-server/          语音服务端
    │   ├── app.py               ★ 入口：读配置 → 起 WS(8000) + 旁路 HTTP(8003) → 恢复持久化提醒
    │   ├── config/
    │   │   ├── config.yaml      上游默认配置（不要改这个）
    │   │   ├── config_loader.py 默认配置 + data/.config.yaml 递归合并，再展开 ${VAR}
    │   │   └── env_expand.py    逐级向上找 .env，把 ${VAR} 替换成真实值
    │   ├── core/
    │   │   ├── connection.py    ★ 主链路：VAD→ASR→LLM→工具调用→TTS，含历史与工具标记拦截
    │   │   ├── handle/          hello / 意图 / 音频接收 / 各类文本消息
    │   │   ├── api/             ★ 旁路 HTTP：软件清单 / 文件查找 / 提醒 / 保存位置 / 音乐缓存 / 播放器
    │   │   └── providers/       ASR(sherpa_onnx) / TTS(火山豆包、sapi_local 兜底) / LLM / 工具执行器
    │   ├── plugins_func/functions/   ★ 12 个插件模块、展开成 23 个语音工具
    │   ├── data/
    │   │   ├── .config.example.yaml  ★ 可提交的配置模板（不改它，复制成 .config.yaml）
    │   │   ├── .config.yaml          真实配置（git 忽略）
    │   │   └── assistant-prompt.txt  人格与说话规则
    │   ├── models/SenseVoiceSmall/   ASR 模型（git 忽略，3.3 步下载）
    │   ├── tools/smoke_ws.py    ★ 冒烟测试（第 3.5 步）
    │   └── requirements.txt     依赖清单
    └── digital-human/           唤醒词服务（sherpa-onnx KWS）
        ├── start.py             入口，默认在 8006 提供调试页
        ├── index.html           浏览器调试页
        └── wakeword_runtime/    检测器、参数(config.json)、模型(models/)、词表(keywords.txt)
```

改行为只需要动两处：`data/.config.yaml`（模块选择、工具清单、插件参数）和 `.env`（密钥）。代码结构照上游即可。

## 5. 已知问题

| # | 现象 | 原因 | 怎么办 |
|---|---|---|---|
| 1 | 说"帮我找 K 盘里的 xx"没结果 | `data/.config.yaml → plugins.file_search.extra_dirs` 默认是注释状态，只允许桌面/文档/下载等已知目录 | 把自己的目录加进 `extra_dirs`，或说"在桌面/文档里找" |
| 2 | 说一句却只得到"好的，我这就去" | 模型的"该调工具却不调"（已知遗留项），不是工具实现的问题 | 再说一遍通常会调；根治要给 LLM 加 `tool_choice="required"` 重试 |
| 3 | 服务端日志全绿，客户端没声音 | ① 火山凭据没填/欠费 ② SAPI 空产物（历史 bug，已修，`MIN_WAV_BYTES=1024` 护栏） | 先看 `tmp/` 下生成的 wav 是否只有几十字节；切 `SapiLocal` 后仍异常看 pywin32 是否装全 |
| 4 | 启动即报 ffmpeg 不可用 | 3.1 的复制步骤没做，或被杀软删 | 重跑 `imageio_ffmpeg` 复制命令，`ffmpeg -version` 要能输出 |
| 5 | 端口 8000/8003 被占 | 上次 `app.py` 没退干净 | 按报错里的 PID `Stop-Process -Id <pid> -Force` |
| 6 | 首次启动很慢（20~60 s） | 加载 SenseVoice int8 模型与 VAD | 正常现象；日志出现 `Websocket Server is running` 后再连 |
| 7 | 提醒到点不响 | 服务端只在启动时读 `data/timers.json`，运行中改文件不热加载 | 用语音设置提醒，或重启服务端 |
| 8 | 外放时自己打断自己 | TTS 被麦克风回采 | 戴耳机，或调小播报音量 |
| 9 | `pip install` 卡在 torch（约 2 GB） | 默认源慢 | 用 3.1 里的清华源；别把 Python 升到 3.11+ |
| 10 | 上游自带 pytest 有 7 项失败 | 上游测试桩问题（`openai+httpx` 的 `is_closed`、`test_stt` 缺模块），与功能无关 | 忽略即可；本项目用 `tools/smoke_ws.py` 与本机功能自检验收 |

## 6. 演示入口

**最小路径（不依赖客户端，可用来验证部署）：**

```powershell
# 窗口 1：服务端
conda activate xiaozhi-esp32-server
cd main\xiaozhi-server
$env:PYTHONUTF8=1
python app.py

# 窗口 2：投一句话进去，看是否拿到识别文本 + 回复音频
cd main\xiaozhi-server
python tools\smoke_ws.py "你好，请用一句话介绍你自己"
```

**完整演示（桌宠形态）**：桌宠客户端不在本仓库，放在同一工程的 `deskpet\` 目录（Electron + Live2D），
与服务端通过 `ws://127.0.0.1:8000/xiaozhi/v1/` 通信：

```powershell
cd deskpet
npm install
cd ..
powershell -ExecutionPolicy Bypass -File .\start_deskpet.ps1
```

桌宠出现在屏幕右下角后，**单击它**或说"你好小智"即可开始对话；右键菜单有唤醒 / 打断 / 休眠 / 退出、
播报音量与音乐音量。

## 附：这个仓库不包含什么

| 没有 | 原因 | 怎么补 |
|---|---|---|
| `models/`（ASR 模型，~890 MB） | 体积大且可重新下载 | 第 3.3 步 |
| `data/.config.yaml` | 含本机路径与偏好，只提交脱敏模板 | 第 3.2 步复制模板 |
| `.env` | 含真实 API Key | 复制 `.env.example` 自己填 |
| `data/` 下运行态（软件清单 / 提醒 / 搜索缓存） | 随使用变化、含本机信息 | 首次使用时自动生成 |
| 桌宠客户端（`deskpet/`） | 独立工程 | 见第 6 节 |
| 上游的 `manager-api` / `manager-web` / `manager-mobile` / `docs` | 本项目的桌宠形态不需要网页管理后台 | 需要时回上游仓库取 |

## 许可

上游为 MIT（`Copyright (c) 2025 xinnan-tech`），本仓库的修改同样以 MIT 发布，保留原 `LICENSE` 与版权声明。
