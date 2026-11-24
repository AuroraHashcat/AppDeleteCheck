# DeleteMemory · Android AI 陪伴应用「记忆管理」调研工具

用「真机 + 多模态大模型 GUI Agent」自动调研 Android 拟人化情感陪伴 / AI 聊天类 App 的**记忆管理方式**：
它们把 AI 的"记忆"以什么形态存起来、用户能以什么粒度查看 / 编辑 / 删除。

整个流程全自动：**从应用市场搜索安装 → 手机号验证码登录 → 探索记忆管理 → 卸载 → 汇总统计**。

> ⚠️ 仅用于研究与竞品分析。请自行确认目标 App 的用户协议，并遵守所在地法律法规。

---

## 工作原理

```
parse/android_ai_apps.json  (App 名单)
        │
        ▼
   main.py  ── 对名单中的每个 App 依次执行 ──┐
        │                                    │
        ├─ 1. install.py   截图+UI树 → LLM 决策 → 在应用宝里搜索并安装
        ├─ 2. login.py     优先「本机号码一键登录」, 否则手机号 → 短信验证码 → 登录
        ├─ 3. check_delete.py  探索记忆管理, 输出结构化结论
        └─ 4. 卸载 App, 记录耗时与 token 用量
        │
        ▼
   operate/steps/  截图、动作日志、result.json、pipeline_results.json
```

- **GUI Agent**：每一步把「截图 + UI 树」发给多模态大模型，模型返回一个 JSON 动作（tap / swipe / input_text / …），程序执行后再进入下一步。
- **短信验证码**：通过 `adb shell content query --uri content://sms/inbox` 直接读短信库（部分 ROM 默认给 shell 授予 `READ_SMS`，无需 root），正则提取验证码后回填。
- **登录态校验**：不依赖模型"我觉得已登录"的判断，而是用界面文字标志做确定性核对（见 `login.detect_login_state`）。

---

## 目录结构

| 文件 | 说明 |
|---|---|
| `operate/main.py` | 批处理入口：安装 → 登录 → 探索 → 卸载 → 统计 |
| `operate/install.py` | GUI Agent：在应用宝搜索并安装指定 App |
| `operate/login.py` | GUI Agent：一键登录 / 手机号 + 短信验证码登录 |
| `operate/check_delete.py` | GUI Agent：调研记忆管理，输出结构化结论并归一化 |
| `operate/sms.py` | 读取手机短信验证码 |
| `operate/ui_utils.py` | 文本输入（自动聚焦 + 写入校验）、登录态文字判定 |
| `operate/llm_api.py` | 多模态 LLM 统一入口（Responses API / chat.completions 自动降级） |
| `operate/connect.md` | 真机无线调试 / USB 调试连接指南 |
| `operate/fix_adb_winusb.ps1` | Windows 下 adb 看不到设备时的修复脚本（见下） |
| `parse/android_ai_apps.json` | App 名单（`name` / `pkg` 等字段） |

---

## 环境准备

```powershell
pip install uiautomator2 openai pillow
python -m uiautomator2 init        # 给手机装 atx-agent 与输入法
```

- 手机开启「开发者选项 → USB 调试」，插线后点「允许」
- `adb devices` 能看到设备且状态为 `device`
- 应用宝已安装在手机上（`install.py` 通过它搜索下载）

环境变量（见 `.env.example`）：

| 变量 | 必填 | 说明 |
|---|---|---|
| `LLM_API_KEY` | ✅ | 大模型 key |
| `LLM_BASE_URL` | | 接口地址，默认 `https://sub.fancybag.cn/v1` |
| `LLM_MODEL` | | 需支持图片输入，默认 `gpt-6-luna` |
| `LLM_API_STYLE` | | `responses` / `chat`，留空自动选择并降级 |
| `LOGIN_PHONE` | ✅ | 登录用手机号（**不要写进代码**） |

```powershell
$env:LLM_API_KEY  = "sk-xxxx"
$env:LLM_BASE_URL = "https://your-relay/v1"
$env:LLM_MODEL    = "gpt-4o-mini"
$env:LOGIN_PHONE  = "13800000000"
```

---

## 用法

```powershell
cd operate

python main.py                       # 只处理名单第 1 个(默认 --limit 1)
python main.py --limit 5             # 处理前 5 个
python main.py --start 3 --limit 5   # 从第 4 个开始处理 5 个
python main.py --skip-install        # App 已装好, 跳过安装
python main.py --skip-login          # 跳过登录
python main.py --skip-uninstall      # 跑完不卸载
python main.py --device 序列号        # 多设备时指定
python main.py --phone 13800000000   # 覆盖环境变量里的手机号
```

单独调试某个阶段：

```powershell
python install.py --app 猫箱
python login.py --pkg com.example.app
python check_delete.py --app 猫箱 --max-steps 40
python sms.py --tail 3               # 只测短信读取
```

---

## 输出

| 路径 | 内容 |
|---|---|
| `operate/steps/install/<idx>_<pkg>/` | 安装过程逐步截图 + `actions.log` |
| `operate/steps/login/<idx>_<pkg>/` | 登录过程截图 + `actions.log` + `login_result.json` |
| `operate/steps/check_delete/<idx>_<pkg>/` | 探索过程截图 + `actions.log` + `result.json` |
| `operate/steps/pipeline_results.json` | 所有 App 的结论列表（每条含 `stats` 耗时/token） |
| `operate/steps/pipeline_stats.json` | 总体耗时、token、分阶段统计 |

运行中每个 App 会打印：

```
[猫箱] 耗时 238.7s (安装 61.2s / 登录 45.0s / 检查 130.4s / 卸载 2.1s)
[猫箱] token 合计 127,300 (输入 122,000 / 输出 5,300), LLM 调用 43 次
```

---

## 结论数据格式（`result.json`, schema_version=2）

记忆能力用**两个正交维度**描述，避免笼统的"有没有删除功能"：

**`storage_form` — 记忆以什么形态存在**

| 值 | 含义 |
|---|---|
| `atomic_item` | 单条元记忆：多条独立条目，可单独查看/删除 |
| `profile_doc` | 用户画像：由对话总结出的一整份画像/人设文档 |
| `conversation_summary` | 会话摘要：按会话或时间段生成的记忆片段 |
| `message_level` | 只到消息级：没有独立记忆，只能回溯某句话或删聊天记录 |
| `none` | 没找到任何记忆形态 |
| `other` | 其他（必须用 `storage_form_raw` 描述） |

**`granularity` — 用户能操作到什么粒度**

`single_item`（单条精确） / `range`（区间、批量） / `rollback`（回溯到某条消息） /
`full_wipe`（只能全量清空） / `none` / `other`

除上述枚举外，还会输出 `unknown_forms`（无法归类的新形态，自由文本）、`not_found`（确认不存在的形态）、
`checked_entries`（实际检查过的入口）和 `confidence`（0~1），便于人工复核与后续收敛分类。

同时保留旧版兼容字段 `has_feature` / `delete_type`(1|2|3|0) / `is_memory_delete`。

---

## Windows 下 `adb devices` 看不到设备？

典型症状：设备管理器 / 资源管理器能看到手机，但 `adb devices` 为空。

原因通常是 ADB 接口（USB class `ff/42/01`）被 Windows 自动绑定到了通用 **WinUSB** 驱动，
只注册了 WinUSB 默认 GUID，没有注册 adb 专用接口 GUID
`{F72FE0D4-CBCB-407D-8814-9ED673D0DD6B}`，于是 adb 枚举不到。

```powershell
# 管理员 PowerShell
powershell -ExecutionPolicy Bypass -File .\fix_adb_winusb.ps1
powershell -ExecutionPolicy Bypass -File .\fix_adb_winusb.ps1 -VerifyOnly   # 只检查
powershell -ExecutionPolicy Bypass -File .\fix_adb_winusb.ps1 -Rollback     # 回滚
```

> 该修复写入的是设备实例级的 `Device Parameters`，**换 USB 口或系统重装驱动后需要重跑**。
> 想要一劳永逸，可安装厂商官方 ADB 驱动（如 Google USB Driver / 手机厂商助手）。

---

## 说明

- 脚本会**避免真正执行删除操作**：只浏览到确认弹窗、读取其中的文字作为证据。
- 卸载步骤会移除目标 App 及其数据，介意请加 `--skip-uninstall`。
- `--skip-install` 与卸载同时启用时，会卸载手机上原有安装的该 App，请留意。
