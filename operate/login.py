# -*- coding: utf-8 -*-
"""手机号 + 短信验证码 登录。

位置: install 之后、check_delete 之前的一个独立步骤。

流程:
  打开/确认 app 在前台 -> GUI agent 找到登录入口 -> 输入手机号
  -> 点「获取验证码」-> agent 输出 fetch_sms_code -> 从手机短信库读取验证码
  -> agent 输入验证码 -> 确认登录 -> 进入主界面输出 done。

用法:
  python login.py                                  # 手机号取环境变量 LOGIN_PHONE
  python login.py --phone 13800000000
  python login.py --device 序列号 --pkg com.example.app
"""
import argparse
import datetime
import json
import os
import re
import time

import uiautomator2 as u2
from openai import OpenAI

import sms as smsutil
import ui_utils
from check_delete import (
    call_llm,
    extract_json,
    screenshot_to_url,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_STYLE,
)

# ---------------- 可调参数 ----------------
MAX_STEPS = 25
STEP_INTERVAL = 1.5
SMS_TIMEOUT = 120
# 不硬编码手机号(避免泄露到仓库): 用 --phone 传入, 或设置环境变量 LOGIN_PHONE
DEFAULT_PHONE = os.environ.get("LOGIN_PHONE", "")
OUT_DIR = "steps/login"
# ----------------------------------------

LOGIN_SYSTEM_PROMPT = """你是手机 GUI 操作智能体, 任务是在当前 App 上判断登录状态并完成登录。
只输出一个 JSON, 不要输出其它文字:
{"thought": "简短说明", "action": "tap|swipe|input_text|fill_phone|fill_code|fetch_sms_code|check_login|back|home|done", "args": {...}}
动作说明:
- tap:            {"x": 整数, "y": 整数}   像素坐标(以截图分辨率为准)
- swipe:          {"x1":..,"y1":..,"x2":..,"y2":..}
- input_text:     {"text": "要输入的内容"}  先 tap 聚焦输入框; 手机号/验证码请改用 fill_phone / fill_code
- fill_phone:     {}   由系统自动把手机号填进输入框(不要自己输手机号)
- fetch_sms_code: {}   表示「我已点了获取验证码」, 系统会去读短信并把验证码告诉你
- fill_code:      {}   由系统自动把已读到的验证码填进输入框
- check_login:    {}   让系统核对当前登录状态, 会告诉你「已登录 / 未登录 / 无法判断」
- back / home:    {}
- done:           {}   确认已登录后才用
铁律(必须遵守):
1) **判断登录状态只能去【我的 / 个人中心】页面**, 依据只能是已登录标志:
   「退出登录 / 退出账号 / 切换账号 / 账号与安全」, 或个人页显示手机号、头像、昵称。
   **绝对不能因为「首页没有看到登录入口」就认为已登录**; 首页不显示登录按钮是常态。
2) 输出 done 之前,**必须先输出 check_login**, 由系统核对后再收尾。
3) 优先级: 「本机号码一键登录 / 一键登录 / 本机手机号免密 / 运营商登录」>「手机号 + 验证码登录」。
4) 走手机号流程时不要用 input_text 自己输手机号或验证码, 一定用 fill_phone / fill_code。
5) 出现「用户协议 / 隐私政策」勾选框时先勾选; 不要点无关广告或第三方登录。
"""


def build_task(phone):
    return (
        f"任务: 判断当前 App 的登录状态, 未登录则完成登录。可用手机号: {phone}。\n"
        "第 0 步(必做): 进入【我的 / 个人中心 / 我的-个人中心】页面, 判断是否已登录。\n"
        "  - 判断依据只能是「退出登录 / 退出账号 / 切换账号 / 账号与安全」或个人页显示手机号/头像/昵称;\n"
        "  - **不能因为首页没有登录入口就认为已登录**;\n"
        "  - 判断完先输出 check_login, 让系统核对。\n"
        "若已登录: 输出 done。\n"
        "若未登录, 按以下顺序处理:\n"
        "  A. 有「本机号码一键登录 / 一键登录 / 本机手机号免密登录 / 运营商登录」→ 点它, 勾选协议并确认;\n"
        "  B. 没有一键登录入口时, 选「手机号登录 / 其他手机号登录 / 验证码登录」, 然后:\n"
        f"     1) 输出 fill_phone —— 由系统自动填入手机号 {phone} 并校验;\n"
        "     2) 点击「获取验证码 / 发送验证码」;\n"
        "     3) 输出 fetch_sms_code —— 系统会读取短信验证码;\n"
        "     4) 输出 fill_code —— 系统会自动把验证码填进去;\n"
        "     5) 点击「登录 / 确认 / 确定」。\n"
        "登录流程走完后, 再进入【我的】页面确认已登录, 输出 check_login, 最后输出 done。"
    )


# 登录态判断用的关键词
_LOGIN_OUT_WORDS = ("登录/注册", "登录注册", "立即登录", "点击登录", "手机号登录",
                    "未登录", "去登录", "登录后查看", "登录体验")
_LOGIN_IN_WORDS = ("退出登录", "退出账号", "切换账号", "注销账号", "账号与安全", "账户与安全")
_MASKED_PHONE_RE = re.compile(r"\d{3}\*{2,4}\d{4}")


def detect_login_state(d):
    """根据当前界面文字判断登录态: 'in' / 'out' / 'unknown'。

    只看确定性的文字标志, 不做"没有登录入口就算已登录"这种推断。
    """
    try:
        xml = d.dump_hierarchy()
    except Exception:
        return "unknown"
    texts = re.findall(r'text="([^"]*)"', xml)
    joined = " | ".join(t for t in texts if t)
    for w in _LOGIN_IN_WORDS:
        if w in joined:
            return "in"
    for w in _LOGIN_OUT_WORDS:
        if w in joined:
            return "out"
    if _MASKED_PHONE_RE.search(joined):
        return "in"
    return "unknown"


def llm_decide(client, model, task, screenshot, xml, step, history, observations, style):
    """截图 + UI 树 (+ 短信观察) -> 多模态 LLM -> 动作 JSON。"""
    obs = ""
    if observations:
        obs = "\n系统提示:\n" + "\n".join(observations[-3:])
    user_text = (
        f"{task}{obs}\n当前第 {step} 步。最近历史动作: {history[-3:]}\n"
        f"UI树(节选):\n{xml[:3000]}"
    )
    return call_llm(client, model, LOGIN_SYSTEM_PROMPT, user_text,
                    img_url=screenshot_to_url(screenshot), style=style)


def execute(d, action):
    name = action.get("action")
    args = action.get("args") or {}
    if name == "tap":
        d.click(args["x"], args["y"])
    elif name == "swipe":
        d.swipe(args["x1"], args["y1"], args["x2"], args["y2"], duration=0.3)
    elif name == "input_text":
        # 自动聚焦 + 写入 + 校验(失败会自动尝试多种方式)
        ui_utils.smart_input(d, args["text"])
    elif name == "back":
        d.press("back")
    elif name == "home":
        d.press("home")
    elif name in ("done", "fill_phone", "fill_code", "fetch_sms_code", "check_login"):
        pass      # 由 login_with_otp 主循环单独处理
    else:
        raise ValueError(f"未知动作: {name}")


def _ensure_foreground(d, pkg, log=print):
    """确保目标 app 在前台; 已在前后台则不动, 避免重启打乱状态。"""
    if not pkg:
        return
    try:
        cur = (d.app_current() or {}).get("package")
    except Exception:
        cur = None
    if cur != pkg:
        log(f"[login] 启动应用 {pkg} (当前前台: {cur})")
        try:
            d.app_start(pkg)
            time.sleep(3)
        except Exception as e:
            log(f"[login] 启动应用失败: {e}")


def login_with_otp(phone=None, device=None, pkg=None, max_steps=MAX_STEPS,
                   sms_timeout=SMS_TIMEOUT, out_dir=OUT_DIR):
    """在真机上完成手机号+短信验证码登录。返回 dict 并写入 out_dir/login_result.json。"""
    phone = (phone or os.environ.get("LOGIN_PHONE") or DEFAULT_PHONE or "").strip()
    if not phone:
        raise SystemExit(
            "缺少登录手机号: 请用 --phone 指定, 或设置环境变量 LOGIN_PHONE")

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
    print("登录手机号:", phone)

    os.makedirs(out_dir, exist_ok=True)
    _ensure_foreground(d, pkg)

    task = build_task(phone)
    history = []
    observations = []
    result = {"ok": False, "phone": phone, "code": None, "reason": "", "steps_used": 0,
              "login_state": "unknown", "login_verified": False}
    fetched_sms = False
    login_checks = 0        # agent 主动核对登录态的次数
    done_rejections = 0     # done 被系统驳回的次数

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
                                    step, history, observations, style)
                break
            except Exception as e:
                print(f"[step {step}] LLM 调用失败(第{attempt + 1}次): {e}")
                time.sleep(3)
        if action is None:
            result["reason"] = "LLM 多次调用失败"
            break

        print(f"[step {step}] {json.dumps(action, ensure_ascii=False)} | 截图 {png}")
        with open(f"{out_dir}/actions.log", "a", encoding="utf-8") as f:
            f.write(f"{step:03d} {json.dumps(action, ensure_ascii=False)}\n")

        history.append(action)
        name = action.get("action")

        if name == "check_login":
            login_checks += 1
            st = detect_login_state(d)
            result["login_state"] = st
            if st == "in":
                observations.append(
                    "系统核对: 当前**已登录**(界面出现「退出登录 / 账号与安全」等标志)。"
                    "请输出 done 收尾。")
            elif st == "out":
                observations.append(
                    "系统核对: 当前**未登录**(界面出现「登录/注册/立即登录」等字样)。"
                    "请继续完成登录流程, 不要输出 done。")
            else:
                observations.append(
                    "系统核对: 从当前界面**无法判断**登录状态。请进入【我的 / 个人中心】页面, "
                    "或滑动页面找到「退出登录 / 账号与安全」等标志后再输出 check_login。")
            time.sleep(1)
            continue

        if name == "done":
            st = detect_login_state(d)
            result["login_state"] = st
            reject = None
            if st == "out":
                reject = ("系统核对: 当前**仍未登录**(界面出现「登录/注册/立即登录」等字样)。"
                          "请进入【我的 / 个人中心】页面完成登录, 不要输出 done。")
            elif st == "unknown" and login_checks == 0:
                reject = ("系统无法确认登录状态。请先进入【我的 / 个人中心】页面, "
                          "输出 check_login 核对一次, 再决定是否 done。")
            if reject and done_rejections < 2:
                done_rejections += 1
                print(f"[login] 驳回 agent 的 done(第 {done_rejections} 次): {reject}")
                observations.append(reject)
                time.sleep(1)
                continue
            result["ok"] = (st != "out")
            result["login_verified"] = (st == "in")
            result["reason"] = action.get("thought") or "agent 判定已登录"
            print(f"[login] agent 判定登录完成 (系统核对: {st})")
            break

        if name == "fill_phone":
            ok = ui_utils.smart_input(d, phone)
            observations.append(
                f"已自动填入手机号 {phone}, 请接着点击「获取验证码」。" if ok else
                f"自动填入手机号失败(界面里没看到 {phone})。请先 tap 手机号输入框聚焦, "
                f"再输出 fill_phone 重试。")
            time.sleep(1)
            continue

        if name == "fill_code":
            code_now = result.get("code")
            if not code_now:
                observations.append(
                    "还没有读到验证码。请先点击「获取验证码」, 再输出 fetch_sms_code。")
                time.sleep(1)
                continue
            ok = ui_utils.smart_input(d, code_now)
            observations.append(
                f"已自动填入验证码 {code_now}, 请点击「登录」。" if ok else
                f"自动填入验证码失败(界面里没看到 {code_now})。请先 tap 验证码输入框聚焦, "
                f"再输出 fill_code 重试。")
            time.sleep(1)
            continue

        if name == "fetch_sms_code":
            if fetched_sms:
                observations.append(
                    "已经读过短信了。不要重复 fetch_sms_code, 请输出 fill_code 把验证码填进去。")
                time.sleep(1)
                continue
            # 防呆: 页面上有输入框却是空的 -> 多半是手机号没填就点了「获取验证码」
            if ui_utils.editable_is_empty(d):
                if ui_utils.smart_input(d, phone):
                    observations.append(
                        f"检测到手机号还没填, 已自动补填 {phone}。"
                        f"请重新点击「获取验证码」, 然后再输出 fetch_sms_code。")
                    time.sleep(1)
                    continue
            fetched_sms = True
            # 以「现在」为起点, 只接受之后到达的短信, 避免拿到上一条旧验证码
            since_ms = smsutil.device_now_ms(d)
            code = smsutil.wait_for_code(d, since_ms=since_ms, timeout=sms_timeout)
            if code:
                result["code"] = code
                observations.append(
                    f"短信验证码是 {code}。请输出 fill_code, 系统会自动把它填进验证码输入框。")
            else:
                observations.append(
                    "没有读到短信验证码(可能未收到或超时)。可以再点一次「获取验证码」后重试, "
                    "如果手机上有短信通知也可以自行查看。")
            time.sleep(1)
            continue

        try:
            execute(d, action)
        except Exception as e:
            print(f"[step {step}] 执行动作失败: {e}")
        time.sleep(STEP_INTERVAL)
    else:
        result["reason"] = result["reason"] or f"达到最大步数 {max_steps} 仍未完成登录"

    result["steps_used"] = len(history)
    result["timestamp"] = datetime.datetime.now().isoformat()
    with open(f"{out_dir}/login_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[login] 结果已写入 {out_dir}/login_result.json")
    return result


def main():
    ap = argparse.ArgumentParser(description="手机号+短信验证码登录")
    ap.add_argument("--phone", default=DEFAULT_PHONE,
                    help="登录手机号(默认读环境变量 LOGIN_PHONE)")
    ap.add_argument("--device", default=None, help="设备序列号, 默认 adb 第一个设备")
    ap.add_argument("--pkg", default=None, help="应用包名, 若不在前台会先启动")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    ap.add_argument("--sms-timeout", type=int, default=SMS_TIMEOUT)
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args()

    result = login_with_otp(phone=args.phone, device=args.device, pkg=args.pkg,
                            max_steps=args.max_steps, sms_timeout=args.sms_timeout,
                            out_dir=args.out_dir)
    print("\n" + "=" * 60)
    print("登录结果:")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=" * 60)


if __name__ == "__main__":
    main()
