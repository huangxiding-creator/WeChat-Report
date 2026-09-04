# 🖨 WeChat-Report — 微信聊天记录智能分析报告生成器

> **把任意微信好友 / 群聊的聊天记录，变成一份公文级专业 Word 分析报告。**
> 纯视觉只读采集 · 智谱免费大模型 · 零成本 · 绿色单文件

<p align="center">
  <b>中文</b> ·
  <a href="#-english">English</a>
</p>

---

## ✨ 这是什么

一个面向**中国办公场景**的 Windows 桌面工具：

选择任意**微信群 / 好友**聊天（可多个合并）、指定**任意时间段**，
工具自动采集屏幕上的**文字 + 图片 + 语音标记**，调用**智谱免费大模型**
做 Map-Reduce 智能分析，产出一份**排班级别的 Word 专业报告**——
含封面、目录、统计表、专题分析、结论建议、大事记、关键图片附录，
全程通过**企业微信机器人**推送进度与最终报告文件。

```
微信 PC 端（只读浏览）
      │ 滚动 + 截图 + OCR + 气泡检测
      ▼
统一消息流（去重/时间归属/说话人归属）
      │ 统计数字化（消息量/活跃度/关键词）
      ▼
智谱免费模型 Map-Reduce
  ├─ glm-4-flash    主力：分块提取 + 全局综合（限流更宽松）
  ├─ glm-4.5-flash  轮换备用（质量更强，限流更紧）
  └─ glm-4v-flash   聊天图片内容理解（免费视觉）
      ▼
公文级 Word 报告（封面/目录/表格/大事记/落款）
      ▼
企业微信机器人：里程碑通知 + 报告文件推送
```

## 🎯 解决什么痛点

| 传统做法 | WeChat-Report |
|---|---|
| 翻几百屏聊天记录手动摘抄 | 选好聊天和时间窗，一键 |
| 截图拼 Word，格式杂乱 | 公文级排版：封面/目录/表格/落款 |
| 自己从聊天里提炼结论 | AI 聚焦最有价值的观点/成果/风险 |
| 图片内容无人整理 | 免费视觉模型逐图理解并写入报告 |
| 结果只能本机看 | 企业微信推送进度 + 报告文件直达群里 |
| 换台电脑就跑不了 | 绿色单文件 exe，拷走即用 |

## 🔒 安全设计（重要）

**对微信严格只读**——内置 `SafetyGuard` 硬约束，违规操作直接 `raise`：

| 动作 | 是否允许 | 机制 |
|---|---|---|
| 滚轮浏览聊天 | ✅ | 仅 `pyautogui.scroll()` |
| 截图 / OCR | ✅ | 只读屏幕像素，零写入微信进程 |
| 点击会话列表切换聊天 | ✅ | 导航白名单区（左上区域）+ 几何校验 |
| 点击窗口底部输入/发送区 | ❌ 禁止 | 底部 20% 为禁区，越界即 `raise` |
| 回车 / Ctrl+V 等按键 | ❌ 禁止 | 键盘全程禁用（零键盘设计） |
| 右键 / 转发 / 删除 / 撤回 | ❌ 禁止 | 无任何此类代码路径 |
| 数据库解密 / 内存读取 | ❌ 不做 | 纯视觉路线，零侵入零破解 |

> 工具不包含、也不依赖任何数据库解密或密钥提取代码。

## 🚀 快速开始

### 方式 A：绿色 exe（推荐）

1. 下载 `dist/WeChat-Report.exe`（或用 `build.bat` 自行打包）
2. 同目录放一份 `config.ini`（从 `config.example.ini` 复制，填入密钥）
3. 双击运行，GUI 里选聊天 → 点「🚀 生成报告」

### 方式 B：源码运行

```bash
git clone https://github.com/huangxiding-creator/WeChat-Report.git
cd WeChat-Report
pip install -r requirements.txt
# 国内加速：pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python main.py            # GUI
python main.py --cli --chats "群名" --window 7d --template work

# 批量导出：枚举全部会话，每个聊天一份"聊天记录整理"word（无 AI、零成本）
python main.py --cli --all --window 365d
python main.py --cli --all --limit 3 --window 365d   # 先试点 3 个
```

> 批量模式会长时间占用鼠标（逐聊天滚动采集），期间请勿操作电脑；
> 支持断点续跑（`output/批量导出/_batch_progress.json`），中断后原命令重跑即续。

### 运行前提

- Windows 10/11 + 微信 PC 端（4.x 已实测）**已登录、窗口未关闭**
- 智谱 BigModel API Key（免费注册：[bigmodel.cn](https://bigmodel.cn)）
- 企业微信群机器人 Webhook（可选，用于通知）

### 使用步骤

1. **配置**（首次）：GUI 右上「⚙ 设置」填智谱 API Key、企业微信 Webhook
2. **选聊天**：输入群名/好友名（每行一个，多个自动合并成一份报告）
3. **选时间窗**：`7d`（近 7 天）/ `2026-03`（整月）/ `2026-01-01~2026-08-27`
4. **选模板**：`work` 工作汇报 / `progress` 项目进展 / `general` 通用总结
5. **点「🚀 生成报告」**，之后全自动；完成后报告自动推送企业微信

## ⚙ 配置（config.ini）

一切与使用相关的参数都在 INI 里，改完即生效（无需改代码）：

| 段 | 关键项 | 说明 |
|---|---|---|
| `[extract]` | `navigate_mode` | `auto`（自动点击会话列表）/ `manual`（倒计时人工切换） |
| | `time_window` | 默认时间窗（GUI 可覆盖） |
| | `scroll_pause` / `scroll_step` | 滚动节奏（卡顿/漏消息时调） |
| | `scrollup_time_budget` | 向上找时间窗起点的墙钟预算（秒）；超大群想覆盖更久历史就调大 |
| | `checkpoint_enabled` | 断点续采（中断后重跑从上次位置继续） |
| `[ai]` | `api_key` | 智谱 Key（或环境变量 `WCR_ZHIPU_API_KEY`） |
| | `text_models` | 免费模型轮换池（默认 glm-4-flash-250414,glm-4.5-flash） |
| | `vision_images` | 每次报告送审图片数（0=关闭图片理解） |
| | `chunk_chars` / `max_chunks` | Map-Reduce 分块预算 |
| `[report]` | `template` / `org_name` | 模板与落款单位 |
| `[notify]` | `webhook` | 企业微信机器人（或环境变量 `WCR_WECOM_WEBHOOK`） |
| | `milestones` | 哪些节点要通知：`start,extract,analyze,report,done` |
| | `send_report_file` | 是否把最终 docx 推送到企业微信（≤20MB） |
| `[batch]` | `skip_names` | 批量模式跳过的系统会话（微信团队/订阅号消息等） |
| | `max_images_embed` | 每份聊天记录 word 最多嵌入图片数（控制文件大小） |
| | `report_every` | 每完成 N 个聊天企业微信汇报一次进度 |
| `[safety]` | `input_zone_ratio` | 底部输入禁区比例（默认 0.80，勿轻易改） |

完整默认值见 [config.example.ini](config.example.ini)。

## 📊 报告长什么样

生成的 Word（A4 公文版式：上 3.7cm 下 3.5cm 左 2.8cm 右 2.6cm）：

- **封面**：红色标宋大标题 + 编制单位 / 资料来源 / 涵盖时段 / 编制日期
- **目录**：Word 域自动目录（打开后 F9 刷新页码）
- **第一部分 总体概述**：AI 综述 + 数据概况表 + 参与者活跃度表 + 关键词表
- **第二部分 专题分析**：按模板风格组织的章节正文
- **第三部分 结论与建议**：结构化结论 + 可执行建议
- **第四部分 大事记**：按月分组的关键时间线
- **附录 关键图片资料**：视觉模型理解过的聊天截图配图说明
- **落款**：单位 + 年月

## 🧠 技术要点

| 设计 | 说明 |
|---|---|
| 纯视觉只读路线 | 不碰数据库/进程内存/控件树；微信 4.x 兼容性好，法律零风险 |
| 精确时间窗采集 | 向上滚动直到见到早于起始日期的时间标签即停，大群提速 10~100 倍 |
| 跨屏去重 | 文本 md5 指纹 + 图片 dHash 指纹，位置变化不影响判重 |
| 说话人归属 | 气泡 x 坐标启发式（左=对方带昵称行，右=我方） |
| 语音标记 | OCR 识别 `12''` 时长样式，纳入时间线与统计（内容见下方边界说明） |
| 断点续采 | 每采集写 checkpoint，中断重跑自动续 |
| 中文路径安全 | `cv2.imwrite` 在 Windows 会把中文路径写成乱码文件名，统一走 `imencode + write_bytes` |
| 模型轮换 | 免费池 round-robin；429 限流自动等 65s 跨分钟窗 + 冷却切换；主力放限流更宽松的 glm-4-flash |
| 混合推理适配 | glm-4.5+ 自动关闭深度思考，防思考链耗尽 token |
| Map-Reduce | 分块提取（事件/决定/待办/观点/问题/事实）→ 全局综合成稿 |
| 免费视觉理解 | glm-4v-flash 对聊天图片逐张生成说明，写入报告并参与分析 |

## ⚠️ 边界与已知限制（诚实说明）

1. **语音内容**：视觉路线下语音消息只记录「发送者 / 时长 / 时间」，
   无法获取语音的文字内容（获取内容需数据库路线，本项目不做）。
2. **OCR 误差**：极端字体/表情包密集时文字可能有少量识别误差，
   AI 分析阶段已要求模型智能纠错。
3. **采集期间**：微信窗口需保持前台且不被遮挡；期间请勿操作鼠标键盘。
4. **好友单聊**：自动导航依赖目标出现在最近会话列表；
   冷门聊天请先用 `manual` 模式（工具倒计时等你手动点开）。
5. **合规提示**：请仅用于整理**自己的**聊天记录；对外披露前注意隐私合规。

## 🛠 项目结构

```
wcr/
├── config.py            INI 配置（默认值内置 + 环境变量注入）
├── models.py            Message/Chat/ReportSpec 统一模型
├── pipeline.py          全流程编排 + 企业微信里程碑
├── gui.py               CustomTkinter 界面
├── extractor/
│   ├── safety.py        只读安全护栏（硬约束）
│   ├── window.py        微信窗口定位与几何
│   ├── capture.py       mss 高速截图
│   ├── ocr.py           RapidOCR
│   ├── bubbles.py       图片气泡检测 + 语音标记
│   ├── timelabels.py    时间标签/时间窗解析（纯函数）
│   ├── scroller.py      时间窗感知滚动
│   ├── composer.py      去重/归属/排序/断点
│   ├── navigator.py     受控导航（会话列表/手动）
│   ├── visual.py        视觉提取器（主流程）
│   └── importer_json.py JSON 离线导入（测试/回放）
├── ai/
│   ├── zhipu_client.py  免费模型轮换客户端
│   └── analyzer.py      Map-Reduce 分析
├── report/
│   ├── stats.py         统计数字化
│   └── docx_builder.py  公文级 Word 生成
└── notify/
    └── wecom.py         企业微信（文本+文件）
tests/                   单元测试（时间解析/去重/安全护栏/客户端/统计）
main.py                  入口（GUI / CLI）
build.bat                PyInstaller 绿色打包
```

## 🧪 测试

```bash
python -m unittest discover -s tests -v   # 109 个用例，无需微信/AI即可跑
```

### 真机实测（微信 4.1，Windows 10）

在活跃度极高的 423 人群聊上的完整链路实测：

| 阶段 | 结果 |
|---|---|
| 自动导航（点击会话列表 + 标题头 OCR 验证） | ✅ 第 2 次尝试即打开目标群 |
| 时间窗采集（上滚探测 + 46 屏逐屏采集） | ✅ 293 条消息（文字 289 / 图片 4） |
| Map-Reduce 分析（2~3 分块 → 综合 → 大事记） | ✅ 事件/观点/问题/事实全结构化 |
| 免费视觉理解 | ✅ 4 张聊天图片全部生成说明并嵌入附录 |
| 公文级 Word 报告 | ✅ 封面/目录/统计表/专题/大事记/图片附录 |
| 企业微信推送 | ✅ 里程碑文本 + 报告文件（154KB docx） |
| 全程只读（SafetyGuard 零违规） | ✅ 仅滚轮 + 1 次会话列表左键 |

## ❓ FAQ

**Q：为什么不用数据库解密（PyWxDump 那种）？**
A：微信 4.0.3.36+ 密钥不再驻留内存，公开工具（PyWxDump/chatlog/wx_key/wechat-dump-rs）
已全部失效或停更/删库/被 DMCA。纯视觉路线是当前唯一稳定可行且法律安全的方式。

**Q：大群几万条消息要跑多久？**
A：配合时间窗（如 `30d`）每千条约 1~2 分钟；全量采集请耐心（断点续采兜底）。

**Q：收费吗？**
A：工具免费开源；AI 全部走智谱免费模型（glm-4.5-flash / glm-4-flash / glm-4v-flash），
注意免费档有分钟级限流，客户端已内置等待重试。

**Q：能换其他 OpenAI 兼容 API 吗？**
A：改 `[ai] base_url` + `text_models` 即可（需自行评估费用与模型能力）。

## 🤝 贡献

欢迎 Issue / PR：更多报告模板（周报/纪要/舆情）、方言纠错表、
导出格式（PDF/HTML）、Linux/macOS 支持。

## 📄 License

MIT © 2026 huangxiding

---

## 🌍 English

A Windows desktop tool that turns **any WeChat group / friend chat history**
(visual read-only capture — no database decryption, no memory hacking) into a
**presentation-grade Word report**, powered by **Zhipu's free LLM lineup**
(glm-4.5-flash / glm-4-flash / glm-4v-flash for image understanding), with
Map-Reduce analysis, statistics tables, timeline, and **WeCom webhook**
notifications including the final report file. Ships as a portable single exe.

```bash
pip install -r requirements.txt
python main.py --cli --chats "GroupName" --window 7d --template work
```

MIT © 2026 huangxiding
