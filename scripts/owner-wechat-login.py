#!/usr/bin/env python3
"""Owner-run QR helpers; capture subprocess output in memory and show safe text only.

The --connect flow opens the local QR image in Preview on macOS. This helper
never displays credentials, starts a daemon, or creates a Tunnel/plugin/
subscription. The existing login CLI performs the requested login operation
against this checkout's existing runtime/state.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


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


def connect_session(root, clock=time.monotonic, pause=time.sleep):
    """One bounded owner scan session. Never retries an ambiguous operation."""
    def login(flag):
        response = run_private(root, 'probe/login.py', [flag], 55)
        data = json.loads(response.stdout)
        if not isinstance(data, dict):
            raise ValueError('invalid_login_response')
        if response.returncode and data.get('error') != 'already_bound':
            raise ValueError('login_failed')
        return data
    result = login('--start')
    if result.get('error') == 'already_bound':
        return 0
    if result.get('status') != 'wait':
        return 1
    rendered = run_private(root, 'scripts/render-qr.py', [], 20)
    if rendered.returncode:
        return 1
    image = root/'runtime/state/weixin-login-qr.png'
    if sys.platform != 'darwin':
        print('请在本机私有视图打开：'+str(image), flush=True)
    else:
        opened = subprocess.run(['/usr/bin/open', '-a', 'Preview', str(image)],
                                capture_output=True, timeout=10)
        if opened.returncode:
            return 1
    print('请用本人微信扫描本机二维码并确认；确认后安装器核验接续门槛，再决定是否启动。', flush=True)
    deadline = clock()+300
    while clock() < deadline:
        result = login('--poll')
        status = result.get('status')
        if status == 'confirmed':
            print('本人绑定已确认；尚未证明手机收发验收通过。', flush=True)
            return 0
        if status not in ('wait', 'scaned', 'scaned_but_redirect'):
            print('扫码流程未完成，需要本人处理：'+(status if status in ('expired', 'need_verifycode', 'verify_code_blocked', 'binded_redirect') else 'unknown_response'), flush=True)
            return 1
        pause(2)
    print('扫码等待已到期；未启动服务，不自动重新生成二维码。', flush=True)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--start", action="store_true")
    mode.add_argument("--poll", action="store_true")
    mode.add_argument("--connect", action="store_true", help="Show one private QR and wait for owner confirmation")
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    if args.connect:
        try:
            return connect_session(root)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            print('扫码结果未确认；未启动服务，不自动重试。', flush=True)
            return 1
    print("此入口只操作本目录的 runtime/state，不启动后台收发。", flush=True)
    print("请先确认真实旧收发者已隔离；复用项目时核对同一绑定和保留状态，通过接续门槛后再恢复收发。", flush=True)
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
            "confirmed": "微信返回 confirmed，绑定已保存。尚需核对同一本人、原订阅与历史接续，以及手机实际收件。",
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
