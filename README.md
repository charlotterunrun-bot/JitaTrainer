# JitaTrainer 吉他训练器

> 一个常驻 Windows 桌面的吉他指板训练器：屏幕用六线谱给出一个音符，你在吉他上把它弹出来，程序通过麦克风听音判断对错，答对即出下一题，并按记忆曲线安排复习与易错强化。

**English**: JitaTrainer is a Windows desktop ear-and-fretboard trainer for guitar. It shows a note in guitar TAB, listens through your microphone, judges whether the note you played is correct, then moves on — while scheduling reviews of your weak notes with a spaced-repetition model.

**当前状态**：需求阶段完成（需求文档 v1.0 已审核通过），即将进入 M0 技术方案与 M1 地基开发。

---

## 它解决什么问题

练琴时最常见的问题是：不知道自己弹得对不对，也不知道自己哪个音、哪个把位最不熟。JitaTrainer 把「看谱 → 找到音 → 弹出来」做成一个即时闭环，并长期记录你的薄弱点，反复喂给你练。

## 核心特性

| 特性 | 说明 |
|---|---|
| 六线谱出题 | 屏幕以吉他通用 TAB（6 线 + 品格数字）给出目标音符，全屏大字，1 米外可读 |
| 麦克风判定 | 实时采集 → YIN 基频检测 + 谐波校验 → 判定音高是否准确 |
| 难度分级 | 按把位：0–3 品 / 0–5 品 / 0–7 品 / 全指板，用户自选 |
| 易错强化 | 记录每个「音名 × 把位区间」的错误率与反应时间，薄弱项加权复现 |
| 记忆曲线 | 简化 SM-2 间隔重复（0/1/3/7/16/35/75/160 天），跨天到期复习 |
| 会话控制 | 固定时长 / 固定题量 / 自由练习；暂停、跳过、超时、快捷键 |
| 统计报告 | 正确率、毫秒级反应时间、错音热力图、逐日曲线、CSV 导出 |
| 多档案 | 多人共用一台电脑，各自独立进度 |
| 中英双语 | 界面文案中英文可切换 |
| 绿色便携 | 单文件夹免安装，数据存程序目录，拷走即带走全部进度 |

## 一条重要的产品事实

麦克风**只能听出音高**，无法知道你按的是哪根弦、哪个品。因此：

> 当题目标注「第 5 弦第 2 品」时，你在别的弦上弹出同样的音**也算通过**。标注位置的作用是帮你建立指板地图，而不是限制你必须按在那里。

这条原则决定了判定引擎与数据结构的设计（判定只认音名）。

## 运行环境

- Windows 10 1809+ / Windows 11（x64）
- 免安装、免管理员权限、运行时不联网
- 推荐：木吉他 + 外接 USB 麦克风

## 开发路线图

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 技术方案（架构细化、算法与参数、打包验证计划） | ✅ 已完成，待评审 |
| M1 | 地基：项目骨架、SQLite、i18n、音频采集 + YIN 检测 + 校准向导 | 待开始 |
| M2 | 模块一「看谱找音」：指板模型、TAB 谱面、判定引擎、练习屏、三种会话模式 | 待开始 |
| M3 | 学习机制：训练项、SM-2 调度、出题配比、错题回炉、难度建议 | 待开始 |
| M4 | 数据与报告：统计页、热力图、曲线、CSV 导出、备份与导入导出 | 待开始 |
| M5 | 打磨与交付：调音器、双语完善、性能优化、单元测试、绿色版打包 | 待开始 |

**后续扩展（本期不实现）**：音阶与调式练习、和弦与和弦转换、节奏与节拍训练，以及听音训练、五线谱视奏、判定细化、多调弦等。

## 文档

- 需求文档（PRD）：[docs/需求文档.md](docs/需求文档.md) — v1.0，已审核通过
- 技术方案：M0 技术方案 → [docs/技术方案.md](docs/技术方案.md)
- 对话记录归档：[docs/对话记录/](docs/对话记录/) — 人机问答与决策溯源（脱敏）
- 开发记录：[docs/开发记录/](docs/开发记录/) — 每个里程碑的过程档案

## 已验证的技术指标（M0 实测）

音高检测采用自实现 YIN + 保守型谐波校验，参数全部由基准测试选定（[tools/pitch_bench.py](tools/pitch_bench.py)）：

| 指标 | 实测结果 |
|---|---|
| 单帧识别精度（吉他音域） | 误差 ≤ 0.4 音分 |
| 端到端判定延迟 | 282.7 ms（窗口 2048 帧 + 稳定 200ms） |
| **判错次数（112 个样本，含 10dB 噪声）** | **0 次**（噪声下只会"拒绝判定"，不会误判） |
| 单帧检测耗时 | 0.5–0.7 ms（10ms 帧预算的 5–7%） |
| 分析窗口硬约束 | ≥ 1371 帧（E2 周期 582.5 帧 ×2，fmin=70Hz） |

## 开发环境

```powershell
# 依赖（开发期需要联网一次；运行时完全离线）
python -m pip install PySide6 sounddevice numpy pyinstaller pytest

# 运行（开发模式）
python run.py

# 离屏自检（验证数据库、语言包、音频设备枚举）
python run.py --selftest

# 测试
python -m pytest

# 构建绿色版（自动运行产物自检）
python tools/build.py --clean
```

### 本机环境的两个坑（已内置兼容处理）

1. **pip 不可用**：本机 Windows 权限模型下，`os.mkdir(path, 0o700)` 与
   `tempfile.mkdtemp()` 创建的目录 ACL 损坏、无法读写（`icacls` 也无法处理），
   而 pip 内部正是用 `mkdtemp` 解包，因此必然失败。
   因此依赖改为**解包官方 wheel 到 `.vendor/`**（已被 `.gitignore` 排除），
   `run.py` 与 `conftest.py` 会把 `.vendor` 与 `src` 加入 `sys.path`。
2. **pytest 临时目录**：pytest 自带 basetemp 机制同样会创建 0o700 目录并在
   会话结束时对其清理，在本机会导致整个测试会话崩溃。
   已在 `conftest.py` 中接管 `tmp_path` 夹具，把临时目录固定在工作区内的
   `.tmp/tests/`，完全绕开 pytest 的临时目录回收逻辑。

`src/jitatrainer/compat.py` 在运行期自动探测并修补 `tempfile.mkdtemp`，
使第三方库（含 PyInstaller）也能正常工作。

## 仓库同步

```powershell
python tools/sync_github.py ensure            # 确保远程仓库存在
python tools/sync_github.py push              # 推送当前分支
python tools/sync_github.py tag m1-foundation # 打里程碑标签并推送
```

访问令牌读取自 `.secrets/github_token.txt`（已被 `.gitignore` 排除，**永不提交**）。

## 第三方依赖与许可

PySide6 (LGPLv3) · sounddevice (MIT) · numpy (BSD) · scipy (BSD)。完整声明见 `THIRD_PARTY_LICENSES.txt`（随绿色版发布）。

## 许可

本项目为个人学习与自用项目，暂未指定开源许可证。
