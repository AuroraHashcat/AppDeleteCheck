# -*- coding: utf-8 -*-
"""GUI Agent 调研已打开的拟人化情感陪伴 AI app 的「记忆管理」方式。

调研两个正交维度:
  - storage_form  记忆的存储形态: 单条元记忆 / 用户画像 / 会话摘要 / 仅消息级 / 其他(自由描述)
  - granularity   用户可操作的粒度: 单条 / 区间批量 / 回溯到某句话 / 全量清空 / 不可删除
另外记录 rollback(回溯)、full_wipe(全量清空)、unknown_forms(无法归类的新形态)。

原理:
  截图 + UI 树 -> 多模态 LLM(gpt-6-luna) 决策 -> adb/uiautomator2 执行 -> 循环
  每一步截图和动作日志都存到 steps/check_delete/ 目录, 最终结论写入 result.json。

前提:
  目标 app 已经打开并处于前台(本脚本不负责启动 app, 也不负责安装)。

依赖与初始化:
  pip install uiautomator2 openai pillow
  python -m uiautomator2 init      # 给手机装 atx-agent 与 fastinput(用于中文输入)

环境变量(与 gui_agent_install_app.py 相同):
  LLM_API_KEY    大模型 key(必填)
  LLM_BASE_URL   接口地址(如 https://sub.fancybag.cn/v1)
  LLM_MODEL      模型名(如 gpt-6-luna, 需多模态/视觉)
  LLM_API_STYLE  接口风格: "responses"(新版 Responses API, 中转站/Codex 常用, 默认)
                 或 "chat"(传统 chat/completions)

用法:
  python check_delete.py                    # 检查当前前台的情感陪伴 app
  python check_delete.py --app 猫箱         # 给任务里标注 app 名字(仅用于描述)
  python check_delete.py --device 序列号     # 多设备时指定
  python check_delete.py --max-steps 40     # 调大探索步数
"""
import argparse
import base64
import datetime
import io
import json
import os
import re
import time

import uiautomator2 as u2
from openai import OpenAI

from llm_api import call_llm    # 统一的多模态 LLM 调用(自动在 responses / chat 间降级)
from ui_utils import enable_input_ime    # 输入法启用(兼容 set_input_ime / set_fastinput_ime)

# ---------------- 可调参数 ----------------
MAX_STEPS = 40          # 最多动作步数
STEP_INTERVAL = 1.5     # 每步之间等待秒数
OUT_DIR = "steps/check_delete"
# ----------------------------------------

# ---------------- 默认模型配置(环境变量同名可覆盖) ----------------
DEFAULT_BASE_URL = "https://sub.fancybag.cn/v1"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_STYLE = "responses"
# ---------------------------------------------------------------

SYSTEM_PROMPT = """你是手机 GUI 操作智能体, 正在调研一款拟人化情感陪伴 AI 应用的「记忆管理 / 删除记忆」能力。
调研目标不是"有没有删除按钮", 而是搞清楚: 这个产品把 AI 的"记忆"以什么【形态】存起来, 又以什么【粒度】让用户查看和管理。

根据截图和 UI 树决定下一步动作, 只输出一个 JSON, 不要输出其它文字:
{"thought": "简短说明", "action": "tap|swipe|input_text|note|back|home|done", "args": {...}}
- tap:        {"x": 整数, "y": 整数}   像素坐标(以截图分辨率为准)
- swipe:      {"x1":..,"y1":..,"x2":..,"y2":..}  向上翻页可用 y 从大到小
- input_text: {"text": "要输入的中文"}
- note:       {"text": "关键发现"}   随时记录线索(尤其是无法归类的记忆形态), 最后汇总时要用
- back / home: {}
- done:       {"args": {见下方结论模板}}

【必须逐个确认的入口】(有就点进去看, 没有记进 not_found 或 checked_entries)
1. 我的 / 个人中心 / 设置 里的「记忆 / 记忆库 / 长期记忆 / 回忆 / 用户画像 / 关于我 / 个人档案 / 它记得我」;
2. AI 角色 / 伴侣的详情页与其设置;
3. 聊天界面右上角菜单, 以及【长按某条消息】弹出的菜单(常见「回到这里 / 从这里继续 / 删除此消息」);
4. 会话列表项【长按 / 左滑】弹出的菜单(删除会话、清空聊天);
5. 任何带「记忆 / 遗忘 / 忘记 / 抹除 / 重置 / 清除 / 人设」字样的页面。

【两个维度必须分开记录】
A. 存储形态 storage_form:
   - atomic_item            单条元记忆: 多条独立条目, 每条是一条可单独查看/删除的记忆
   - profile_doc            用户画像: 由对话总结出的一整份画像/人设文档(通常是一段文字或若干标签)
   - conversation_summary   会话摘要: 按会话或时间段生成的记忆片段
   - message_level          只到消息级: 没有独立记忆, 只能回溯到某句话或删聊天记录
   - none                   没找到任何记忆形态
   - other                  以上都不符合 → 必须用 storage_form_raw 详细描述
B. 操作粒度 granularity:
   - single_item  可以精确删除/编辑单条记忆
   - range        可以选中一段/多条(区间、批量)
   - rollback     可以回溯到某条消息, 让 AI 忘掉它之后的内容
   - full_wipe    只能一次性全部清空 / 重置角色
   - none         不能删除
   - other        其他 → 用 granularity_raw 描述

【最重要的一条】如果你发现了不属于上面任何一类的形态(例如"短期/长期记忆分层"、"按人物或事件分组的记忆图谱"、
"情绪标签式记忆"、"记忆有置信度/可被 AI 自行更新"等), 一律用 storage_form="other" + storage_form_raw 写清楚,
并同时放进 unknown_forms 列表。**不要硬套已有分类** —— 这部分是本任务最有价值的产出。

【安全】不要真的点最终确认删除/清空; 只读到确认弹窗的文字即可(弹窗说明就是最好的证据)。
【防重复】同一位置点 2 次界面都没变化就不要点了, 换入口或换策略; 实在无路可走就输出 done。"""

SUMMARY_PROMPT = """你是手机 GUI 操作记录的分析员。根据下面的探索记录(含 note 笔记), 只输出一个 JSON 结论, 不要输出其它文字:
{"has_memory_management": true或false,
 "memory_features": [{"name": "", "path": "", "storage_form": "", "storage_form_raw": "",
                      "granularity": "", "granularity_raw": "", "can_view": true, "can_edit": true,
                      "can_delete": true, "auto_generated": true, "evidence": "", "screenshot": ""}],
 "memory_forms": [""], "best_granularity": "", "rollback": {"available": false, "entry": "", "can_pick_message": false},
 "full_wipe": {"available": false, "entry": ""}, "unknown_forms": [""], "not_found": [""],
 "checked_entries": [""], "evidence": "", "conclusion": "", "confidence": 0.0}
定义:
- storage_form 取值: atomic_item(单条元记忆) / profile_doc(用户画像) / conversation_summary(会话摘要) /
  message_level(只到消息级) / none / other(必须在 storage_form_raw 里描述)。
- granularity 取值: single_item(单条精确) / range(区间或批量) / rollback(回溯到某句话) / full_wipe(全量清空) / none / other。
- memory_forms: 本次出现过的 storage_form 去重列表; best_granularity: 最细的可操作粒度。
- unknown_forms: 无法归入上述分类的记忆形态(自由描述), 这是关键产出。
- not_found: 明确确认不存在的形态。
- confidence: 0~1, 你对结论的把握。"""


def build_task(app):
    return (
        f"目标: 调研当前已打开(前台)的拟人化情感陪伴 AI 应用「{app}」的记忆管理方式。\n"
        "要回答的问题:\n"
        "  1) AI 的『记忆』在这个产品里以什么形态存在? (单条元记忆 / 用户画像 / 会话摘要 / 只有消息级 / 其他新形态)\n"
        "  2) 用户能以什么粒度查看、编辑、删除这些记忆? (单条 / 区间批量 / 回溯到某句话 / 只能全量清空 / 都不能)\n"
        "  3) 这些记忆是【自动从对话生成】的还是用户手动写的?\n"
        "  4) 有没有不在上述分类里的新形态? 如果有, 一定要用 note 记下来并用 other + 文字描述。\n"
        "探索顺序建议:\n"
        "  ① 我的 / 个人中心 / 设置 → 找「记忆 / 记忆库 / 长期记忆 / 回忆 / 用户画像 / 个人档案」;\n"
        "  ② AI 角色或伴侣的详情页与设置;\n"
        "  ③ 聊天界面右上角菜单; 【长按一条消息】看有没有「回到这里 / 删除此消息」;\n"
        "  ④ 会话列表【长按 / 左滑】看有没有删除会话、清空聊天;\n"
        "  ⑤ 任何带「记忆 / 遗忘 / 忘记 / 抹除 / 重置 / 清除 / 人设」的页面。\n"
        "要求:\n"
        "  - 找到疑似入口要点进去, 读页面标题、功能说明、确认弹窗的文字(这是最好的 evidence);\n"
        "  - 每个入口无论找到没找到都要记进 checked_entries; 确认没有的形态记进 not_found;\n"
        "  - 遇到无法归类的形态, 立刻 note 记录, 不要等到最后凭记忆写;\n"
        "  - **不要真的执行删除/清空**, 只浏览到确认弹窗即可;\n"
        "  - 探索充分后输出 done, 并按结论模板给出结构化结果。"
    )


# ---------------- 结论归一化 ----------------
STORAGE_FORMS = ("atomic_item", "profile_doc", "conversation_summary", "message_level", "none", "other")
GRANULARITIES = ("single_item", "range", "rollback", "full_wipe", "none", "other")
# 粒度从细到粗排序(用于取"最细粒度")
GRAN_ORDER = {"single_item": 0, "range": 1, "rollback": 2, "full_wipe": 3, "none": 4, "other": 5}
# 旧版 delete_type 兼容映射(main.py 与历史数据仍在用)
LEGACY_DELETE_TYPE = {"single_item": 1, "range": 2, "rollback": 2, "full_wipe": 3, "none": 0, "other": 0}


def _norm_enum(value, allowed, default="other"):
    if isinstance(value, str):
        v = value.strip().lower()
        if v in allowed:
            return v
    return default


def _norm_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "是", "有")
    return bool(value)


def _norm_list(value):
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if x is not None and str(x).strip()]
    return []


def normalize_result(raw):
    """把 agent 给出的结论补齐 + 规范化, 并派生兼容旧版的字段。

    这样即使模型漏字段/字段大小写不一致/给了列表字符串, 落盘结果也始终稳定。
    """
    res = dict(raw) if isinstance(raw, dict) else {}

    feats = []
    for f in (res.get("memory_features") or []):
        if not isinstance(f, dict):
            continue
        feats.append({
            "name": str(f.get("name") or ""),
            "path": str(f.get("path") or ""),
            "storage_form": _norm_enum(f.get("storage_form"), STORAGE_FORMS),
            "storage_form_raw": str(f.get("storage_form_raw") or ""),
            "granularity": _norm_enum(f.get("granularity"), GRANULARITIES),
            "granularity_raw": str(f.get("granularity_raw") or ""),
            "can_view": _norm_bool(f.get("can_view")),
            "can_edit": _norm_bool(f.get("can_edit")),
            "can_delete": _norm_bool(f.get("can_delete")),
            "auto_generated": _norm_bool(f.get("auto_generated")),
            "evidence": str(f.get("evidence") or ""),
            "screenshot": str(f.get("screenshot") or ""),
        })

    # 存储形态汇总: 优先用结构化 features 推导, 再并入模型自报的 memory_forms。
    # 模型自报里不在枚举内的字符串会被移到 unknown_forms, 保证 memory_forms 可统计。
    forms = []
    extra_unknown = []
    for f in feats:
        sf = f["storage_form"]
        if sf and sf != "none" and sf not in forms:
            forms.append(sf)
    for x in _norm_list(res.get("memory_forms")):
        xl = x.strip().lower()
        if xl in STORAGE_FORMS:
            if xl != "none" and xl not in forms:
                forms.append(xl)
        elif x not in extra_unknown:
            extra_unknown.append(x)

    # 最细粒度: 只接受合法枚举值, 垃圾值直接忽略(避免凭空造出分类)
    grans = [f["granularity"] for f in feats if f["granularity"]]
    raw_best = res.get("best_granularity")
    if isinstance(raw_best, str) and raw_best.strip().lower() in GRANULARITIES:
        grans.append(raw_best.strip().lower())
    best = sorted(grans, key=lambda x: GRAN_ORDER.get(x, 9))[0] if grans else "none"

    has = res.get("has_memory_management")
    if has is None:
        has = bool(feats) or bool(forms)
    has = _norm_bool(has)

    rollback = res.get("rollback") if isinstance(res.get("rollback"), dict) else {}
    full_wipe = res.get("full_wipe") if isinstance(res.get("full_wipe"), dict) else {}

    res.update({
        "schema_version": 2,
        "has_memory_management": has,
        "memory_features": feats,
        "memory_forms": forms,
        "best_granularity": best,
        "rollback": {
            "available": _norm_bool(rollback.get("available")),
            "entry": str(rollback.get("entry") or ""),
            "can_pick_message": _norm_bool(rollback.get("can_pick_message")),
        },
        "full_wipe": {
            "available": _norm_bool(full_wipe.get("available")),
            "entry": str(full_wipe.get("entry") or ""),
        },
        "unknown_forms": list(dict.fromkeys(
            _norm_list(res.get("unknown_forms")) + extra_unknown)),
        "not_found": _norm_list(res.get("not_found")),
        "checked_entries": _norm_list(res.get("checked_entries")),
        "evidence": str(res.get("evidence") or ""),
        "conclusion": str(res.get("conclusion") or ""),
    })

    try:
        conf = float(res.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    res["confidence"] = max(0.0, min(1.0, conf))

    # ---- 兼容旧版字段 ----
    res["has_feature"] = has
    res["delete_type"] = LEGACY_DELETE_TYPE.get(best, 0) if has else 0
    mem_forms = {f["storage_form"] for f in feats}
    res["is_memory_delete"] = has and bool(
        mem_forms & {"atomic_item", "profile_doc", "conversation_summary", "other"})
    if feats:
        res.setdefault("feature", feats[0].get("name") or "")
        res.setdefault("path", feats[0].get("path") or "")
    res.setdefault("feature", "")
    res.setdefault("path", "")
    return res


def _first_json_object(text):
    """括号配对提取第一个完整 JSON 对象(兼容嵌套与字符串内大括号)。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def extract_json(text):
    """从 LLM 输出里提取并解析 JSON, 兼容 markdown 包裹 / 多对象 / 前后杂字。"""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    obj = _first_json_object(text)
    if obj is not None:
        try:
            return json.loads(obj)
        except Exception:
            pass
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    raise ValueError(f"LLM 未返回合法 JSON: {text[:200]}")


def screenshot_to_url(screenshot):
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def llm_decide(client, model, task, screenshot, xml, step, history, style):
    """截图 + UI 树 -> 多模态 LLM -> 动作 JSON。"""
    user_text = (
        f"{task}\n当前第 {step} 步。最近历史动作: {history[-3:]}\n"
        f"UI树(节选):\n{xml[:3000]}"
    )
    return call_llm(client, model, SYSTEM_PROMPT, user_text,
                    img_url=screenshot_to_url(screenshot), style=style)


def llm_summarize(client, model, task, history, style, notes=None):
    """探索结束后, 根据动作/思考记录 + note 笔记给出结构化结论。"""
    note_text = ""
    if notes:
        note_text = "\n\n探索过程中记录的 note 笔记(可信度最高, 优先采用):\n" + \
                    "\n".join(f"- {n}" for n in notes)
    user_text = (
        f"{task}\n\n以下是本次探索的动作与思考记录:\n"
        + "\n".join(json.dumps(h, ensure_ascii=False) for h in history)
        + note_text
        + "\n\n请根据这些记录输出 JSON 结论。"
    )
    return call_llm(client, model, SUMMARY_PROMPT, user_text, style=style)


def action_signature(action, tol=15):
    """把动作转成可比较签名: tap/swipe 的坐标量化到 tol 像素, 其余用动作名。"""
    name = action.get("action")
    if name == "tap":
        a = action.get("args") or {}
        x = round(a.get("x", 0) / tol) * tol
        y = round(a.get("y", 0) / tol) * tol
        return f"tap:{x},{y}"
    if name == "swipe":
        a = action.get("args") or {}
        pts = [round(a.get(k, 0) / tol) * tol for k in ("x1", "y1", "x2", "y2")]
        return "swipe:" + ",".join(map(str, pts))
    return name


def detect_loop(history, window=8, tol=15, repeat_threshold=3):
    """检测最近 window 步是否陷入重复循环:
    1) 某一(量化后)位置被 tap 了 >= repeat_threshold 次;
    2) 或最近 window 步的动作签名去重后 <= 2 种(来回重复)。
    """
    if len(history) < 4:
        return False
    recent = history[-window:]
    tap_count = {}
    for a in recent:
        if a.get("action") == "tap":
            a_args = a.get("args") or {}
            x = round(a_args.get("x", 0) / tol) * tol
            y = round(a_args.get("y", 0) / tol) * tol
            tap_count[(x, y)] = tap_count.get((x, y), 0) + 1
    if tap_count and max(tap_count.values()) >= repeat_threshold:
        return True
    sigs = [action_signature(a, tol) for a in recent]
    return len(set(sigs)) <= 2


def execute(d, action):
    name = action.get("action")
    args = action.get("args") or {}
    if name == "tap":
        d.click(args["x"], args["y"])
    elif name == "swipe":
        d.swipe(args["x1"], args["y1"], args["x2"], args["y2"], duration=0.3)
    elif name == "input_text":
        enable_input_ime(d)                # 启用输入法(中文输入依赖它)
        d.send_keys(args["text"])
    elif name == "note":
        pass      # 由 check_delete 主循环收集, 不产生界面动作
    elif name == "back":
        d.press("back")
    elif name == "home":
        d.press("home")
    elif name == "done":
        pass
    else:
        raise ValueError(f"未知动作: {name}")


def check_delete(app, device=None, max_steps=MAX_STEPS, out_dir=OUT_DIR):
    """检查前台 app 是否具有「删除记忆/遗忘」功能, 返回结论 dict 并写入 out_dir/result.json。"""
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("API_KEY")
    if not api_key:
        raise SystemExit("缺少 LLM key: 请设置环境变量 LLM_API_KEY(或 API_KEY)")
    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
        timeout=120,
        max_retries=0,
    )
    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
    style = os.environ.get("LLM_API_STYLE", DEFAULT_STYLE)

    d = u2.connect() if device is None else u2.connect(device)
    info = d.info or {}
    print("设备:", info.get("productName") or info.get("product"), "| 分辨率:", d.window_size())
    print("检查对象(需已在前台):", app)

    os.makedirs(out_dir, exist_ok=True)
    task = build_task(app)
    history = []
    notes = []        # agent 通过 note 动作记录的关键线索

    result = None
    stopped_reason = None
    for step in range(1, max_steps + 1):
        screenshot = d.screenshot()
        ts = datetime.datetime.now().strftime("%H%M%S")
        png = f"{out_dir}/{step:03d}_{ts}.png"
        screenshot.save(png)
        xml = d.dump_hierarchy()

        action = None
        for attempt in range(3):  # 简单重试, 规避偶发网络抖动
            try:
                action = llm_decide(client, model, task, screenshot, xml,
                                    step, history, style)
                break
            except Exception as e:
                print(f"[step {step}] LLM 调用失败(第{attempt + 1}次): {e}")
                time.sleep(3)
        if action is None:
            break

        print(f"[step {step}] {json.dumps(action, ensure_ascii=False)} | 截图 {png}")
        with open(f"{out_dir}/actions.log", "a", encoding="utf-8") as f:
            f.write(f"{step:03d} {json.dumps(action, ensure_ascii=False)}\n")

        history.append(action)
        act_name = action.get("action")

        if act_name == "note":
            txt = str((action.get("args") or {}).get("text") or "").strip()
            if txt:
                notes.append(txt)
                print(f"[step {step}] note: {txt}")
            continue

        if act_name == "done":
            result = action.get("args") or {}
            # 结论不完整(缺核心字段)时, 用历史 + note 再汇总一次
            if (result.get("has_memory_management") is None
                    or not isinstance(result.get("memory_features"), list)
                    or not result.get("conclusion")):
                try:
                    fixed = llm_summarize(client, model, task, history, style, notes)
                    if isinstance(fixed, dict) and fixed:
                        result = fixed
                except Exception as e:
                    print(f"汇总结论失败: {e}")
            break
        if detect_loop(history):
            stopped_reason = "检测到重复操作循环, 提前停止"
            print(f"[step {step}] {stopped_reason}")
            break
        execute(d, action)
        time.sleep(STEP_INTERVAL)
    else:
        stopped_reason = f"达到最大步数 {max_steps} 仍未输出 done"
        print(f"{stopped_reason}, 尝试根据历史汇总结论...")

    if result is None and stopped_reason:
        try:
            result = llm_summarize(client, model, task, history, style, notes)
        except Exception as e:
            print(f"汇总结论失败: {e}")

    result = normalize_result(result)
    result["app"] = app
    result["steps_used"] = len(history)
    result["notes"] = notes
    result["timestamp"] = datetime.datetime.now().isoformat()
    if stopped_reason:
        result["stopped_reason"] = stopped_reason

    with open(f"{out_dir}/result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"结果已写入 {out_dir}/result.json")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="当前打开的情感陪伴AI应用",
                    help="app 名称, 仅用于任务描述与结果标注")
    ap.add_argument("--device", default=None, help="设备序列号, 默认 adb 第一个设备")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args()

    result = check_delete(args.app, device=args.device,
                          max_steps=args.max_steps, out_dir=args.out_dir)
    print("\n" + "=" * 60)
    print("检查结论:")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=" * 60)


if __name__ == "__main__":
    main()
