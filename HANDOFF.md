# 项目交接说明（HANDOFF）

最后审查：2026-09-01
当前 Windows 客户端发布版本：**0.9.12**

> 本文以当前代码为准，不包含任何密码、访问令牌或私钥。文中只记录安全配置原则和风险。

## 1. 当前状态

云祺 AI 直播是一套 Windows 桌面直播话术播放客户端与云端 Qwen3-TTS 服务。客户端按话术逐句请求语音、维持播放缓存、输出到指定声卡，并支持参考音频、插播、本地批量合成和版本更新。

本次审查确认自动化测试通过，并已建立 `v0.9.12-baseline` Git 基线。但项目尚不宜直接视为“生产发布完成”：版本号存在多处不一致、静态检查仍有 16 项问题，真实 GPU、声卡和升级回退场景也需要发布前人工验收。

## 2. 功能范围

### 客户端已实现

- TXT 话术管理、编辑、保存与逐句播放。
- `#500#` 等毫秒停顿标记（最大 60,000 ms）。
- `[甲|乙]`、`【甲|乙】` 随机文案选择。
- 每句话独立指定参考音频和语速；未指定时可回退到默认音色。
- 递归读取参考音频文件夹、自然排序、悬浮文案、相似度达到约 90% 时自动匹配。
- 与云端同步当前实际使用的本地参考音频，并缓存远端音色映射。
- 正常、降低随机性、关闭随机性三种生成模式；“关闭”使用稳定种子和保守采样，并非完全贪心解码。
- 启动前最低缓存 1–10 句（默认 3），运行时目标缓存 20 句。
- 暂停/继续、停止后重新启动、从选中句开始、选中多行禁播。
- 当前句播放完成后插播预设话术。
- 本地批量合成：不播放、保存 WAV、单句最多尝试 3 次、停止后保留成功文件；上批结果归档并保留 7 天。
- 播放设备选择、当前话术与生成/连接/播放日志、RTF 显示、窗口布局和连接设置持久化。
- 在线检查更新、离线升级、完整安装、固定启动器与版本目录切换相关实现。

### 服务端已实现

- FastAPI 健康检查、音色列表、音色上传和 WebSocket 流式合成。
- SQLite 保存音色元数据，本地目录保存参考音频。
- Qwen3-TTS 后端、模拟后端和音频格式转换。
- 全局异步推理锁，避免同一进程内并发推理争用。
- 可选 Bearer/令牌校验；默认占位符未修改时认证关闭。
- 独立 TTS 启动器，可在 7860 端口启动/停止 API 并查看日志。

### 当前不在范围内

弹幕/礼物接入、LLM 自动改写、数字人驱动、自动开播、麦克风录音与平台账号运营均未实现。

## 3. 架构与数据流

```text
TXT 话术 + 逐句设置 + 本地参考音频
              │
              ▼
Windows 客户端（PySide6）
  ├─ 文本解析/队列调度
  ├─ 参考音频匹配与同步
  ├─ WebSocket 请求云端 TTS
  └─ WAV 缓存 → 音频设备播放
              │ HTTP / WebSocket
              ▼
FastAPI 服务 → 后端适配层 → Qwen3-TTS / Mock
              ├─ SQLite 音色元数据
              └─ 本地音色文件
```

客户端目前是单进程、多线程/异步混合结构。界面主逻辑集中在 `client/app.py`，网络请求、播放、存储与本地合成拆分为独立模块。云端 API 与模型后端通过 `backends.py` 解耦。

规划中的固定安装结构是：固定启动器读取 `current.json`，从 `versions/<版本>` 启动程序；话术、参考音频和设置放在独立“用户数据”目录，升级不得覆盖。相关代码已经存在，但完整安装、跨版本迁移、断电回退仍需人工验收。

## 4. 主要文件

| 路径 | 作用 |
|---|---|
| `src/ailive/client/app.py` | 主窗口、话术、队列、播放状态和界面交互；约 4,782 行，是当前最大风险点 |
| `src/ailive/client/audio.py` | WAV 转换、播放、停顿、队列控制 |
| `src/ailive/client/network.py` | HTTP/WebSocket 通信与音色同步 |
| `src/ailive/client/storage.py` | 话术、设置和逐句配置持久化 |
| `src/ailive/client/local_synthesis.py` | 本地批量合成、归档和清理 |
| `src/ailive/client/update_service.py` | 在线更新检查；客户端发布版本定义在此 |
| `src/ailive/client/version_layout.py` | 固定安装目录和用户数据目录约定 |
| `src/ailive/client/updater.py` | 在线升级安装逻辑 |
| `src/ailive/client/offline_updater.py` | 离线升级逻辑 |
| `src/ailive/client/full_installer.py` | 完整安装程序逻辑 |
| `src/ailive/client/client_launcher.py` | 固定启动器 |
| `src/ailive/server/app.py` | FastAPI 路由、认证、上传与 WebSocket |
| `src/ailive/server/backends.py` | Mock/Qwen3-TTS 后端和推理参数 |
| `src/ailive/server/repository.py` | SQLite 音色仓库 |
| `src/ailive/server/config.py` | 服务端环境配置 |
| `src/ailive/launcher.py` | 云端 TTS Web 启动器（7860） |
| `scripts/` | 构建、诊断、部署和发布脚本 |
| `tests/` | 自动化测试 |
| `docs/PROTOCOL.md` | HTTP/WebSocket 协议说明 |
| `docs/DEPLOYMENT.md` | 云端部署说明 |

## 5. 运行、测试与构建

### 开发安装

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[client,server,dev]"
```

GPU 服务器另装 GPU 依赖：

```powershell
.venv\Scripts\python -m pip install -e ".[server,gpu]"
```

### 启动

```powershell
# Mock 服务（不需要 GPU）
$env:TTS_BACKEND = "mock"
.venv\Scripts\python -m uvicorn ailive.server.app:app --host 127.0.0.1 --port 8000

# 客户端
.venv\Scripts\python -m ailive.client.app

# 云端 TTS 启动器
.venv\Scripts\python -m ailive.launcher
```

真实 Qwen3-TTS 的模型路径、设备和鉴权应通过环境变量或受控部署配置提供，不能写入仓库。

### 自动化测试

本次实际执行：

```cmd
set QT_QPA_PLATFORM=offscreen&& .venv\Scripts\python.exe -m pytest tests -p no:cacheprovider --basetemp C:\Users\Administrator\Documents\ChatGPT\云祺网络\.pytest-handoff -q
```

结果：**97 passed，3 warnings，1.51s**。

警告包括 Starlette TestClient/httpx 兼容弃用提示，以及 FastAPI `on_event("startup")` 弃用提示。

静态检查：

```powershell
.venv\Scripts\python -m ruff check src scripts tests
```

结果：**未通过，共 16 项**。分类为 6 项导入排序、5 项时区感知 datetime、3 项测试未使用变量、1 项无效 `noqa`、1 项字符串构造建议。

### 构建

构建入口位于 `scripts/`，包括客户端、启动器、在线/离线更新器和完整安装包。发布前必须在干净 Windows 环境验证生成物，不应仅根据脚本退出码判定可发布。

## 6. 本次未覆盖的验收

- 真实 GPU/Qwen3-TTS 推理、显存占用、RTF 和长时间稳定性。
- 真实 Windows 声卡、Voicemeeter 和设备断连恢复。
- 公网弱网、超时、WebSocket 中断和服务重启。
- 完整安装、在线更新、离线更新、断电失败与一键回退的手工流程。
- Windows 代码签名、SmartScreen 和杀毒软件误报。

## 7. 已知问题

1. `update_service.py` 为 0.9.12，但 `pyproject.toml`、包 `__init__` 和 FastAPI 元数据仍为 0.1.0。
2. 已建立本地 Git 基线，但尚未配置远程仓库、分支保护和自动备份；本机磁盘故障时仍可能丢失历史。
3. Ruff 仍有 16 项错误，CI 质量门禁尚未建立。
4. FastAPI 仍使用已弃用的 `on_event("startup")`。
5. 直播生成单句失败会重置整次播放任务，缺少单句重试、跳过和断点恢复；本地批量合成已有重试。
6. WebSocket 没有指数退避重连策略。
7. `client/app.py` 过大，状态机、UI、队列与业务逻辑高度耦合。
8. 音色仓库缺少删除、配额、重复文件清理和数据库维护功能。
9. Qwen prompt cache 未设置明确容量上限。
10. 上传只限制大小（50 MB），缺少可靠的媒体解码验证和 MIME 校验。
11. 完整安装默认指向固定 D 盘路径且缺少路径选择；便携 ZIP 更适合当前分发方式，但尚未形成单一标准构建入口。
12. 更新包有 SHA256 校验但没有数字签名。
13. `releases/` 和历史构建产物较多，缺少自动保留策略。

## 8. 技术债

- 统一版本来源，构建时生成包版本、API 版本、更新清单和界面版本。
- 拆分 `app.py`：播放状态机、缓存调度、话术模型、参考音频选择和 UI 分层。
- 把服务生命周期迁移到 FastAPI lifespan。
- 建立 CI：pytest、ruff、构建烟雾测试、Markdown 链接检查和机密扫描。
- 为实时播放增加故障注入测试、可取消请求和有界重试。
- 统一用户数据 schema 并提供显式迁移版本。
- 为发布产物增加签名、可复现构建和清单。
- 清理临时目录、历史发行物和重复脚本，确定唯一发布流程。

## 9. 安全注意事项

- 项目根目录存在本地 SSH 私钥文件名；本次没有读取或复制其内容。应将私钥移出项目目录，确认未被提交，并在怀疑外泄时立即轮换。
- `.env.example` 中的鉴权值是占位符；如果不替换，8000 API 实际处于无认证状态。
- 7860 启动器可远程启动、停止服务并读取日志，目前没有独立认证，不能直接暴露到公网。
- HTTP/WS 默认明文传输；公网部署必须使用 TLS、反向代理、防火墙和最小开放端口。
- 启用认证时，WebSocket 令牌放在查询参数可能进入代理日志，应改为短期票据或安全的首帧认证。
- 参考音频可能包含个人声音和隐私信息，应明确授权、访问控制、保留期限和删除机制。
- 更新包只有哈希完整性校验，没有发布者身份签名；下载源或更新清单被替换时仍有风险。
- 日志和错误弹窗不得记录完整令牌、密码、私钥或不必要的个人数据。

## 10. 下次代码审查重点（按优先级）

1. 为本地 Git 仓库配置受控远程备份并推送基线标签；推送前再次确认机密文件不在索引或历史中。
2. 默认关闭公网入口或强制安全认证，重点审查 7860 启动器与 WebSocket 鉴权。
3. 复测“暂停→继续→停止→再次启动”和关闭随机性时维持 20 句缓存的完整状态机。
4. 对直播单句生成失败实现有界重试、跳过和队列恢复，避免整场停止。
5. 在两台全新 Windows 电脑验证 0.9.12 完整便携包、用户数据保留、升级与回退。
6. 统一版本号并清零 Ruff 错误，再建立 CI 门禁。
7. 拆分 `client/app.py`，为缓存调度与播放状态机补充基于事件序列的测试。
8. 做真实 GPU 2–4 小时压力测试，记录 RTF、失败率、显存和缓存水位。

## 11. 交接约定

- 不要删除或覆盖用户的话术、参考音频、插播话术、窗口布局和连接设置。
- 不要把“关闭随机性”理解成必须字节级完全一致；当前语义是尽量稳定。
- 不要把 Mock 测试通过等同于真实云端模型、声卡或升级流程通过。
- 任何发布前变更都应更新本文件、`README.md` 和 `CHANGELOG.md`。
