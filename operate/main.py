# -*- coding: utf-8 -*-
"""批量检查 AI 陪伴 app 是否具有「删除记忆/遗忘」功能。

对名单里的每个 app 依次执行:
  1) install.install_app()       在真机上安装该 app;
  2) login.login_with_otp()      手机号 + 短信验证码登录;
  3) check_delete.check_delete() 检查删除功能(内部写 result.json 并返回结论);
  4) 得到结论后卸载该 app;
  5) 处理下一条。

用法:
  python main.py                       # 只处理名单第 1 个(--limit 默认 1, 便于先跑通)
  python main.py --limit 5             # 处理前 5 个
  python main.py --start 3 --limit 5   # 从第 4 个开始处理 5 个
  python main.py --skip-install        # app 已装好, 跳过安装
  python main.py --skip-login          # 跳过登录(例如 app 无需登录)
  python main.py --skip-uninstall      # 检查完不卸载
  python main.py --device 序列号        # 多设备时指定
  python main.py --phone 13800000000   # 登录手机号(也可用环境变量 LOGIN_PHONE)
  python main.py --sms-timeout 180     # 等验证码的超时秒数(默认 120)

名单默认读 ../parse/android_ai_apps.json, 可用 --list 覆盖。
每个 app 的截图/结果分别存到 steps/install/<idx>_<pkg>/ 与 steps/check_delete/<idx>_<pkg>/,
汇总结论写入 steps/pipeline_results.json。
"""
import argparse
import json
import os
import time

import uiautomator2 as u2

import check_delete
import install
import llm_api
import login

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_APP_LIST = os.path.join(HERE, "..", "parse", "android_ai_apps.json")


def load_apps(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("apps") or data.get("list") or []
    if not isinstance(data, list):
        raise ValueError(f"名单格式不支持: {path}")
    return data


def uninstall_pkg(d, pkg):
    try:
        return d.app_uninstall(pkg)
    except Exception as e:
        print(f"  app_uninstall 失败({e}), 改用 pm uninstall")
        return d.shell(["pm", "uninstall", pkg])


def print_stats(name, stats):
    """打印单个 app 的耗时 / token 统计。"""
    ph = stats.get("phases") or {}
    tk = stats.get("tokens") or {}
    print(f"[{name}] 耗时 {stats.get('total_sec')}s "
          f"(安装 {ph.get('install', 0)}s / 登录 {ph.get('login', 0)}s / "
          f"检查 {ph.get('check', 0)}s / 卸载 {ph.get('uninstall', 0)}s)")
    total = stats.get("total_tokens", 0)
    prompt = sum(v.get("prompt_tokens", 0) for v in tk.values())
    completion = sum(v.get("completion_tokens", 0) for v in tk.values())
    calls = stats.get("llm_calls", 0)
    if total:
        print(f"[{name}] token 合计 {total:,} "
              f"(输入 {prompt:,} / 输出 {completion:,}), LLM 调用 {calls} 次")
    else:
        print(f"[{name}] token 合计 0 (LLM 调用 {calls} 次, "
              f"其中 {stats.get('missing_usage_calls', 0)} 次接口未返回 usage)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default=DEFAULT_APP_LIST, help="app 名单 json 路径")
    ap.add_argument("--limit", type=int, default=1, help="最多处理几个 app(默认 1)")
    ap.add_argument("--start", type=int, default=0, help="从第几个开始(0-based)")
    ap.add_argument("--device", default=None, help="设备序列号, 默认 adb 第一个设备")
    ap.add_argument("--phone", default=login.DEFAULT_PHONE,
                    help="登录手机号(默认读环境变量 LOGIN_PHONE)")
    ap.add_argument("--sms-timeout", type=int, default=login.SMS_TIMEOUT,
                    help="等待短信验证码的超时秒数")
    ap.add_argument("--skip-install", action="store_true", help="跳过安装(app 已在手机)")
    ap.add_argument("--skip-login", action="store_true", help="跳过登录步骤")
    ap.add_argument("--skip-uninstall", action="store_true", help="检查完不卸载")
    args = ap.parse_args()

    apps_all = load_apps(args.list)
    apps = apps_all[args.start: args.start + args.limit]
    print(f"名单共 {len(apps_all)} 个, 本次处理 {len(apps)} 个(start={args.start}, limit={args.limit})")

    d = u2.connect() if args.device is None else u2.connect(args.device)

    summary = []
    for i, app in enumerate(apps):
        name = app.get("name") or ""
        pkg = app.get("pkg") or app.get("package") or app.get("package_name") or ""
        idx = args.start + i
        tag = pkg or f"{idx:03d}"
        print("\n" + "=" * 60)
        print(f"[{i + 1}/{len(apps)}] {name}  ({pkg})")
        print("=" * 60)

        if not name:
            print("缺少 app 名称, 跳过")
            continue

        # ---- 统计: 本 app 开始 ----
        t_app = time.time()
        stats = {"phases": {}, "tokens": {}, "total_sec": 0.0,
                 "total_tokens": 0, "llm_calls": 0, "missing_usage_calls": 0}

        # 1) 安装
        t0 = time.time()
        llm_api.reset_usage()
        if not args.skip_install:
            ok = install.install_app(
                name, device=args.device,
                out_dir=os.path.join(HERE, "steps", "install", f"{idx:03d}_{tag}"))
            if not ok:
                print(f"[{name}] 安装未确认完成, 继续尝试检查(若 app 未打开会失败)")
        else:
            print("跳过安装(假定已安装)")
        stats["phases"]["install"] = round(time.time() - t0, 1)
        stats["tokens"]["install"] = llm_api.get_usage()

        # 2) 登录(手机号 + 短信验证码)
        t0 = time.time()
        llm_api.reset_usage()
        login_ok = None
        login_state = None
        if not args.skip_login:
            login_out = os.path.join(HERE, "steps", "login", f"{idx:03d}_{tag}")
            lg = login.login_with_otp(
                phone=args.phone, device=args.device, pkg=pkg or None,
                sms_timeout=args.sms_timeout, out_dir=login_out)
            login_ok = bool(lg.get("ok"))
            login_state = lg.get("login_state")
            if login_ok:
                print(f"[{name}] 登录完成 (验证码 {lg.get('code')}, "
                      f"系统核对={lg.get('login_state')})")
            else:
                print(f"[{name}] 登录未确认成功: {lg.get('reason')} "
                      f"(系统核对={lg.get('login_state')}) -- 仍继续尝试检查")
        else:
            print("跳过登录")
        stats["phases"]["login"] = round(time.time() - t0, 1)
        stats["tokens"]["login"] = llm_api.get_usage()

        # 3) 检查删除功能(内部写 result.json 并返回)
        t0 = time.time()
        llm_api.reset_usage()
        out_dir = os.path.join(HERE, "steps", "check_delete", f"{idx:03d}_{tag}")
        result = check_delete.check_delete(name, device=args.device, out_dir=out_dir)
        stats["phases"]["check"] = round(time.time() - t0, 1)
        stats["tokens"]["check"] = llm_api.get_usage()

        result.setdefault("name", name)
        result.setdefault("pkg", pkg)
        if login_ok is not None:
            result["login_ok"] = login_ok
            result["login_state"] = login_state
        print(f"[{name}] has_feature={result.get('has_feature')} "
              f"delete_type={result.get('delete_type')} "
              f"forms={result.get('memory_forms')} "
              f"best={result.get('best_granularity')}")

        # 4) 卸载
        t0 = time.time()
        llm_api.reset_usage()
        if pkg and not args.skip_uninstall:
            uninstall_pkg(d, pkg)
            print(f"[{name}] 已卸载 {pkg}")
        stats["phases"]["uninstall"] = round(time.time() - t0, 1)
        stats["tokens"]["uninstall"] = llm_api.get_usage()

        # 5) 汇总本 app 的耗时与 token
        stats["total_sec"] = round(time.time() - t_app, 1)
        stats["total_tokens"] = sum(v.get("total_tokens", 0) for v in stats["tokens"].values())
        stats["llm_calls"] = sum(v.get("calls", 0) for v in stats["tokens"].values())
        stats["missing_usage_calls"] = sum(
            v.get("missing_usage", 0) for v in stats["tokens"].values())
        result["stats"] = stats
        summary.append(result)
        print_stats(name, stats)
        time.sleep(2)

    # 汇总
    os.makedirs(os.path.join(HERE, "steps"), exist_ok=True)
    with open(os.path.join(HERE, "steps", "pipeline_results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 总体耗时 / token 统计
    def _st(s, *keys, default=0):
        cur = s.get("stats") or {}
        for k in keys:
            cur = (cur or {}).get(k) if isinstance(cur, dict) else None
        return cur if cur is not None else default

    grand = {
        "apps": len(summary),
        "total_sec": round(sum(_st(s, "total_sec") for s in summary), 1),
        "total_tokens": sum(_st(s, "total_tokens") for s in summary),
        "llm_calls": sum(_st(s, "llm_calls") for s in summary),
        "phases_sec": {},
        "tokens_by_phase": {},
        "per_app": [
            {
                "name": s.get("name"),
                "pkg": s.get("pkg"),
                "sec": _st(s, "total_sec"),
                "tokens": _st(s, "total_tokens"),
                "login_ok": s.get("login_ok"),
                "has_feature": s.get("has_feature"),
                "delete_type": s.get("delete_type"),
                "memory_forms": s.get("memory_forms"),
                "best_granularity": s.get("best_granularity"),
                "unknown_forms": s.get("unknown_forms"),
            }
            for s in summary
        ],
    }
    for ph in ("install", "login", "check", "uninstall"):
        grand["phases_sec"][ph] = round(sum(_st(s, "phases", ph) for s in summary), 1)
        grand["tokens_by_phase"][ph] = sum(_st(s, "tokens", ph, "total_tokens") for s in summary)

    with open(os.path.join(HERE, "steps", "pipeline_stats.json"), "w", encoding="utf-8") as f:
        json.dump(grand, f, ensure_ascii=False, indent=2)

    print(f"\n完成 {len(summary)} 个, 汇总写入 steps/pipeline_results.json")
    print(f"[总计] 耗时 {grand['total_sec']}s | token {grand['total_tokens']:,} | "
          f"LLM 调用 {grand['llm_calls']} 次")
    if summary:
        print(f"[平均] 每个 app {grand['total_sec'] / len(summary):.1f}s | "
              f"token {grand['total_tokens'] / len(summary):,.0f}")
    print("        分阶段耗时: " + " / ".join(
        f"{k} {v}s" for k, v in grand["phases_sec"].items()))
    print("        分阶段token: " + " / ".join(
        f"{k} {v:,}" for k, v in grand["tokens_by_phase"].items()))
    print("        明细写入 steps/pipeline_stats.json")


if __name__ == "__main__":
    main()
