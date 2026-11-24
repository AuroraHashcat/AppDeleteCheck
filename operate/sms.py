# -*- coding: utf-8 -*-
"""读取手机短信验证码。

原理:
  通过 adb shell 查询短信库 content://sms/inbox。
  部分 ROM(荣耀/Honor 等) 会给 shell(uid 2000) 授予 SYSTEM_FIXED 的
  android.permission.READ_SMS, 因此无需 root、无需装 App 即可直接读取。
  若某台设备的 shell 没有该权限(会抛 SecurityException), 可改用
  code_from_notifications() 从通知栏兜底。

用法:
    from sms import wait_for_code
    code = wait_for_code(d, timeout=120)
"""
import re
import time

# content query 的输出形如:
#   Row: 0 _id=12, address=10086, body=您的验证码是123456, date=1764000000000
_ROW_RE = re.compile(
    r"Row: \d+ .*?address=(?P<address>.*?), body=(?P<body>.*?), date=(?P<date>\d+)",
    re.S,
)

# 「验证码」等关键词后紧跟的一串数字
_KEYWORD_RE = re.compile(
    r"(?:验证码|校验码|动态码|确认码|安全码|登录码|verification\s*code|code)"
    r"[^0-9]{0,12}(\d{4,8})",
    re.I,
)


def _sh(d, args):
    """执行 adb shell 命令并返回字符串输出(兼容 str / list 入参)。"""
    resp = d.shell(args)
    out = getattr(resp, "output", None)
    if out is None:
        out = str(resp)
    return out


def device_now_ms(d):
    """手机端当前时间(毫秒)。用设备时钟避免电脑与手机时间不一致。"""
    try:
        return int(str(_sh(d, ["date", "+%s"])).strip()) * 1000
    except Exception:
        return int(time.time() * 1000)


def extract_code(text):
    """从短信正文里提取验证码; 找不到返回 None。"""
    if not text:
        return None
    m = _KEYWORD_RE.search(text)
    if m:
        return m.group(1)
    for pat in (r"(?<!\d)(\d{6})(?!\d)", r"(?<!\d)(\d{5})(?!\d)", r"(?<!\d)(\d{4})(?!\d)"):
        m = re.search(pat, text)
        if m:
            return m.group(0)
    return None


def query_sms(d, since_ms=0, limit=20):
    """读取收件箱短信, 返回 [{'address','body','date'}...] (按时间倒序)。

    since_ms: 只返回 date >= since_ms 的短信, 用于过滤旧验证码。
    若 shell 无 READ_SMS 权限则抛 PermissionError。
    """
    out = _sh(d, [
        "content", "query",
        "--uri", "content://sms/inbox",
        "--projection", "address:body:date",
        "--sort", "date DESC",
    ])
    if "SecurityException" in out or "Permission Denial" in out:
        raise PermissionError("shell 没有 READ_SMS 权限: " + out[:200].replace("\n", " "))

    msgs = []
    for m in _ROW_RE.finditer(out):
        try:
            date = int(m.group("date"))
        except ValueError:
            continue
        if date < since_ms:
            continue
        msgs.append({
            "address": m.group("address").strip(),
            "body": m.group("body"),
            "date": date,
        })
        if len(msgs) >= limit:
            break
    return msgs


def latest_code(d, since_ms=0):
    """返回 (code, msg); 没有则 (None, None)。"""
    for msg in query_sms(d, since_ms=since_ms, limit=5):
        code = extract_code(msg["body"])
        if code:
            return code, msg
    return None, None


def code_from_notifications(d, since_ms=0):
    """兜底: 从通知栏 dump 里找验证码(shell 有 DUMP 权限即可)。"""
    try:
        out = _sh(d, ["dumpsys", "notification", "--noredact"])
    except Exception:
        return None
    # 只取疑似验证码的通知文本行
    for line in out.splitlines():
        if "验证码" not in line and "code" not in line.lower():
            continue
        code = extract_code(line)
        if code:
            return code
    return None


def wait_for_code(d, since_ms=None, timeout=120, interval=3, log=print):
    """轮询等待新短信里的验证码, 超时返回 None。"""
    if since_ms is None:
        since_ms = device_now_ms(d)
    deadline = time.time() + timeout
    log(f"[sms] 等待验证码(最多 {timeout}s, since={since_ms})...")
    perm_warned = False
    while time.time() < deadline:
        try:
            code, msg = latest_code(d, since_ms=since_ms)
        except PermissionError as e:
            log(f"[sms] 读取短信失败: {e}")
            return None
        except Exception as e:  # 偶发 adb 抖动, 继续重试
            log(f"[sms] 查询异常(继续重试): {e}")
            code, msg = None, None
        if code:
            log(f"[sms] 收到验证码 {code} (来自 {msg['address']})")
            return code
        if not perm_warned:
            perm_warned = True
        time.sleep(interval)
    log("[sms] 等待验证码超时")
    return None


if __name__ == "__main__":
    import argparse
    import uiautomator2 as u2

    ap = argparse.ArgumentParser(description="测试读取短信验证码")
    ap.add_argument("--device", default=None)
    ap.add_argument("--tail", type=int, default=5, help="打印最近几条短信")
    args = ap.parse_args()

    dev = u2.connect() if args.device is None else u2.connect(args.device)
    rows = query_sms(dev, since_ms=0, limit=args.tail)
    if not rows:
        print("收件箱为空(或没有新短信)")
    for r in rows:
        print(f"[{r['date']}] {r['address']}: {r['body'][:80]}")
        print("        -> 提取验证码:", extract_code(r["body"]))
