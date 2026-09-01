# 云祺 AI 直播

云祺 AI 直播由 Windows 桌面客户端和云端 Qwen3-TTS 服务组成。客户端管理直播话术、参考音频、逐句缓存和声卡播放；服务端负责音色上传和流式语音合成。

当前 Windows 客户端发布版本：**0.9.12**。Python 包元数据仍显示 0.1.0，这是已知的版本统一技术债，不代表客户端发布版本。

## 主要能力

- 逐句直播话术播放，支持毫秒停顿、随机选项、从指定位置开始和选中行禁播。
- 每句话独立选择参考音频和语速；支持默认音色回退、嵌套音色文件夹和约 90% 文本自动匹配。
- 启动前最低缓存可配置，运行时目标缓存 20 句；支持暂停、继续、停止和重启。
- 正常、降低随机性、关闭随机性三种生成模式。
- 当前句结束后插播预设话术。
- 本地批量合成 WAV、失败重试、停止保留、上批归档与 7 天清理。
- 输出设备选择、RTF 日志、窗口布局和连接配置持久化。
- 在线/离线升级、完整安装和固定启动器相关实现。

不包含弹幕礼物、数字人、自动开播、麦克风录音或 LLM 自动改写。

## 目录

```text
src/ailive/client/      Windows 客户端、播放、存储、更新与本地合成
src/ailive/server/      FastAPI 服务、后端适配和音色仓库
src/ailive/launcher.py  云端 TTS 启动器
scripts/                构建、诊断和部署脚本
tests/                  自动化测试
deploy/                 Linux/云端启动文件
docs/                   协议、部署和历史 MVP 文档
```

完整交接信息见 [HANDOFF.md](HANDOFF.md)，协议见 [docs/PROTOCOL.md](docs/PROTOCOL.md)，部署见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)，版本记录见 [CHANGELOG.md](CHANGELOG.md)。

## 开发环境

要求 Python 3.10 或更高版本。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[client,server,dev]"
```

GPU 服务器使用：

```powershell
.venv\Scripts\python -m pip install -e ".[server,gpu]"
```

## 本地运行

```powershell
# Mock 服务（不依赖 GPU）
$env:TTS_BACKEND = "mock"
.venv\Scripts\python -m uvicorn ailive.server.app:app --host 127.0.0.1 --port 8000

# 客户端
.venv\Scripts\python -m ailive.client.app

# 云端 TTS 启动器
.venv\Scripts\python -m ailive.launcher
```

真实模型路径、设备和认证通过环境变量或安全的部署配置提供，不要写入代码或文档。

## 测试与质量检查

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python -m pytest tests -p no:cacheprovider -q
.venv\Scripts\python -m ruff check src scripts tests
```

2026-09-01 审查结果：pytest **97 passed，3 warnings**；Ruff **未通过，16 项**。真实 GPU、Windows 声卡和安装/升级回退仍需人工验收，详情见 `HANDOFF.md`。

## 配置、数据与安全

服务端配置示例位于 `.env.example`。默认占位认证值不提供保护，公网部署前必须启用 TLS、鉴权、防火墙和最小端口策略。

安装版应把程序版本和用户数据分离。升级时禁止覆盖话术、参考音频、插播话术、窗口布局与连接设置。

- 禁止提交 `.env`、私钥、令牌、真实密码、用户参考音频和运行数据库。
- 云端 8000 API 与 7860 启动器不得在无认证条件下直接暴露公网。
- 发布包目前只有 SHA256 完整性校验，正式分发前应增加代码签名和发布签名。
- 如发现私钥曾进入仓库、压缩包或聊天记录，应立即轮换，而不是只删除文件。
