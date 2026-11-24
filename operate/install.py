# -*- coding: utf-8 -*-
"""GUI Agent 在真机上搜索并安装指定 app(默认: 猫箱)。

原理:
  截图 + UI 树 -> 多模态 LLM 决策 -> adb/uiautomator2 执行 -> 循环
  每一步截图和动作日志都存到 steps/install/ 目录, 方便无人值守后复盘。

连接真机(二选一):
  A. USB:  手机开「开发者选项 -> USB 调试」, 插电脑, 手机弹窗点允许,
           `adb devices` 能看到序列号即可。
  B. 无线: 手机开「开发者选项 -> 无线调试」(Android 11+), 手机与电脑同一 WiFi,
           `adb pair 手机IP:配对端口`(输入配对码) -> `adb connect 手机IP:连接端口`。

依赖与初始化:
  pip install uiautomator2 openai pillow
  python -m uiautomator2 init      # 给手机装 atx-agent 与 fastinput(用于中文输入)

环境变量:
  LLM_API_KEY    大模型 key(必填)
  LLM_BASE_URL   接口地址(如 https://sub.fancybag.cn/v1)
  LLM_MODEL      模型名(如 gpt-6-luna, 需多模态/视觉)
  LLM_API_STYLE  接口风格: "responses"(新版 Responses API, 中转站/Codex 常用, 默认)
                 或 "chat"(传统 chat/completions)

用法:
  python gui_agent_install_app.py                # 安装「猫箱」
  python gui_agent_install_app.py --app 星野     # 指定别的 app
  python gui_agent_install_app.py --device 序列号 # 多设备时指定
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
MAX_STEPS = 30          # 最多动作步数
STEP_INTERVAL = 1.5     # 每步之间等待秒数
OUT_DIR = "steps/install"
# ----------------------------------------

# ---------------- 默认模型配置(环境变量同名可覆盖) ----------------
# ⚠️ 这里含明文 key, 不要把这个文件提交到 git 或发给别人
DEFAULT_BASE_URL = "https://sub.fancybag.cn/v1"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_STYLE = "responses"
# ---------------------------------------------------------------

SYSTEM_PROMPT = """你是手机 GUI 操作智能体。根据截图和 UI 树决定下一步动作, 只输出一个 JSON, 不要输出其它文字:
{"thought": "简短说明", "action": "tap|swipe|input_text|back|home|done", "args": {...}}
- tap:        {"x": 整数, "y": 整数}   像素坐标(以截图分辨率为准)
- swipe:      {"x1":..,"y1":..,"x2":..,"y2":..}  向上翻页可用 y 从大到小
- input_text: {"text": "要输入的中文"}
- back / home / done: {}
任务完成后, 输出 {"action": "done"}。"""


def build_task(app):
    return (
        f"目标: 在手机上安装应用「{app}」。\n"
        "步骤建议:\n"
        "1) 打开手机上的应用宝, 点搜索框, 输入应用名, 搜索;\n"
        "2) 在结果里点开正确的那个应用, 点「安装/下载」按钮;\n"
        "3) 等待安装完成(按钮变成「打开」即完成), 然后输出 done。\n"
        "如果手机没有应用商店, 可以打开浏览器访问该应用官网下载 APK 并安装。\n"
        "注意: 若页面弹权限框/安装确认框, 按允许/继续。"
    )


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


def llm_decide(client, model, task, screenshot, xml, step, history, style):
    """截图 + UI 树 -> 多模态 LLM -> 动作 JSON。
    style: "responses" 优先走新版 Responses API, SDK/中转站不支持时自动降级到
           chat/completions; "chat" 直接走传统接口。
    """
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    img_url = f"data:image/png;base64,{b64}"
    user_text = (
        f"{task}\n当前第 {step} 步。最近历史动作: {history[-3:]}\n"
        f"UI树(节选):\n{xml[:3000]}"
    )
    return call_llm(client, model, SYSTEM_PROMPT, user_text,
                    img_url=img_url, style=style)


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
    elif name == "back":
        d.press("back")
    elif name == "home":
        d.press("home")
    elif name == "done":
        pass
    else:
        raise ValueError(f"未知动作: {name}")


def install_app(app, device=None, max_steps=MAX_STEPS, out_dir=OUT_DIR):
    """用 GUI agent 在真机上安装指定 app, 返回是否完成任务(agent 输出 done)。"""
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
    print("安装目标:", app)

    os.makedirs(out_dir, exist_ok=True)
    task = build_task(app)
    history = []

    for step in range(1, max_steps + 1):
        screenshot = d.screenshot()
        ts = datetime.datetime.now().strftime("%H%M%S")
        png = f"{out_dir}/{step:03d}_{ts}.png"
        screenshot.save(png)
        xml = d.dump_hierarchy()

        action = None
        for attempt in range(3):  # 简单重试, 规避偶发网络抖动
            try:
                action = llm_decide(client, model, task, screenshot, xml, step, history, style)
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
        if action.get("action") == "done":
            print("任务完成")
            return True
        execute(d, action)
        time.sleep(STEP_INTERVAL)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="猫箱")
    ap.add_argument("--device", default=None, help="设备序列号, 默认 adb 第一个设备")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    args = ap.parse_args()
    install_app(args.app, device=args.device, max_steps=args.max_steps)


if __name__ == "__main__":
    main()
