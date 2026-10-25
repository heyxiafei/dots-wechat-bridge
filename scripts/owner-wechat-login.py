#!/usr/bin/env python3
"""Owner-run QR helpers; capture subprocess output in memory and show safe text only.

This helper never opens UI, reads credentials, starts a daemon, or creates a
Tunnel/plugin/subscription. The existing login CLI performs the requested login
operation against this checkout's existing runtime/state.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def run_private(root, script, arguments, timeout):
    return subprocess.run(
        [sys.executable, str(root / script), "--state-dir", str(root / "runtime/state"), *arguments],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--start", action="store_true")
    mode.add_argument("--poll", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    print("此入口只操作本目录的 runtime/state，不启动后台收发。", flush=True)
    print("请先保持本轮已知桥接实例停止，确认手机所示机器人与旧 bot 独立后再恢复收发。", flush=True)
    try:
        operation = run_private(root, "probe/login.py", ["--start" if args.start else "--poll"], 55)
        result = json.loads(operation.stdout)
        if not isinstance(result, dict):
            raise ValueError("Unexpected login result")
        if result.get("error") == "already_bound":
            print("本目录已有 ready 绑定；没有生成新二维码，请核对既有绑定。")
            return 1
        if operation.returncode or result.get("error"):
            print("登录操作未完成；私有诊断未显示。请交由助手核对固定错误类别，勿发送原始登录文件。")
            return 1
        status = result.get("status")
        if args.start and status == "wait":
            rendered = run_private(root, "scripts/render-qr.py", [], 20)
            if rendered.returncode:
                print("本地二维码图像未生成；私有诊断未显示。")
                return 1
            print("二维码已在本地生成，有效期约五分钟。请在本机 Preview 手动打开：")
            print(root / "runtime/state/weixin-login-qr.png")
            print("用本人手机扫码并确认后，再运行 owner-confirm-wechat.command。")
            print("不要把二维码图片、二维码网址或登录文件发送到聊天。")
            return 0
        messages = {
            "confirmed": "微信返回 confirmed，绑定已保存。尚需核对本人身份、独立 bot 与手机实际收件。",
            "wait": "仍在等待扫码；没有自动重复检查。",
            "scaned": "已扫码，仍需在本人手机确认；确认后再运行此入口。",
            "scaned_but_redirect": "微信要求按其区域继续确认；请再次运行确认入口。",
            "expired": "本次二维码已过期；准备扫码时再运行生成入口。",
            "need_verifycode": "微信需要额外校验；请让助手提供本人私有终端操作方法，不要把验证码发到聊天。",
            "verify_code_blocked": "微信校验被阻止；已停止，不自动重试。",
            "binded_redirect": "微信返回绑定重定向；已停止，需核对手机提示。",
        }
        print(messages.get(status, "未确认绑定；请核对手机提示，私有诊断未显示。"))
        return 0 if status in ("confirmed", "wait", "scaned", "scaned_but_redirect") else 1
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("本次操作未完成或超时，结果需核对；未显示私有输出。请勿连续重试或发送原始文件。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
