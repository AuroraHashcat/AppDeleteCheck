# -*- coding: utf-8 -*-
"""UI 输入小工具: 输入法兼容 + "文字是否真的写进去了"的校验。

背景与要解决的问题:
  1. uiautomator2 3.x 把 set_fastinput_ime() 改名为 set_input_ime(),
     继续用旧名字会抛 DeprecationWarning。
  2. 只调 send_keys() 时, 如果目标输入框没有真正获得焦点, 文字会被静默丢弃;
     agent 以为输入成功就点了「获取验证码」, 于是出现
     「手机号没输进去就点了获取验证码」的现象。
  3. UiObject.set_text() 走无障碍接口(不依赖输入法), 是最稳的写法,
     所以这里用它作为首选, send_keys 只作为兜底。
"""
import re
import time

_EDIT_CLASSES = ("android.widget.EditText", "android.widget.AutoCompleteTextView")


def enable_input_ime(d):
    """启用 uiautomator2 自带输入法(新旧 API 自适应)。返回是否成功。"""
    try:
        d.set_input_ime(True)          # u2 >= 3.0
        return True
    except (AttributeError, TypeError):
        pass
    try:
        d.set_fastinput_ime(True)      # 旧版 API(deprecated)
        return True
    except Exception:
        return False


def digits_only(text):
    """只保留数字, 用于忽略 199 8624 5117 这类分组显示。"""
    return re.sub(r"\D", "", text or "")


def find_editable(d, max_index=5):
    """返回页面上第一个可见输入框(UiObject), 没有则 None。"""
    for cls in _EDIT_CLASSES:
        for i in range(max_index):
            try:
                el = d(className=cls, instance=i)
                if el.exists:
                    return el
            except Exception:
                break
    return None


def focused_text(d):
    """当前焦点控件的文字; 拿不到返回 ''. """
    try:
        el = d(focused=True)
        if el.exists:
            return el.get_text() or ""
    except Exception:
        pass
    return ""


def verify_text(d, text, timeout=2.0):
    """确认 text 确实出现在界面上(数字按去分隔符后比较)。"""
    want = digits_only(text)
    deadline = time.time() + timeout
    while True:
        got = focused_text(d)
        if got:
            if (want and want in digits_only(got)) or (text and text in got):
                return True
        else:
            # 焦点信息拿不到时, 退回看整棵 UI 树
            try:
                xml = d.dump_hierarchy()
                if (text and text in xml) or (want and want in digits_only(xml)):
                    return True
            except Exception:
                pass
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


def smart_input(d, text, log=print):
    """尽最大努力把 text 写进输入框并校验, 返回是否成功。

    顺序:
      1) 当前焦点控件 set_text (无障碍, 不依赖输入法);
      2) 找第一个 EditText, 点击聚焦后 set_text;
      3) 启用输入法 + send_keys(clear=True) 兜底。
    """
    # 1) 焦点控件直接写
    try:
        el = d(focused=True)
        if el.exists:
            el.set_text(text)
            if verify_text(d, text):
                return True
    except Exception as e:
        log(f"[input] 焦点控件 set_text 失败: {e}")

    # 2) 找到输入框, 点一下再写
    try:
        box = find_editable(d)
        if box is not None:
            box.click()
            time.sleep(0.4)
            box.set_text(text)
            if verify_text(d, text):
                return True
    except Exception as e:
        log(f"[input] 定位/写入输入框失败: {e}")

    # 3) 兜底: 输入法 + send_keys
    try:
        enable_input_ime(d)
        time.sleep(0.3)
        d.send_keys(text, clear=True)
        if verify_text(d, text):
            return True
    except Exception as e:
        log(f"[input] send_keys 失败: {e}")

    log(f"[input] 写入失败, 界面里没看到: {text}")
    return False


def editable_is_empty(d):
    """页面上有输入框且它是空的 -> True(用于防呆: 手机号没填就点获取验证码)。"""
    box = find_editable(d)
    if box is None:
        return False
    try:
        return not digits_only(box.get_text() or "")
    except Exception:
        return False
