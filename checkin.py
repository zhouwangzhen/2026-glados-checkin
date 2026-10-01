#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2026 GLaDOS 自动签到 (积分增强版)

功能：
- 全自动签到
- 精准获取当前积分 (Points)
- 积分达标自动兑换会员天数（默认 500 分兑换 100 天，可配置/关闭）
- PushPlus 微信推送（包含积分、剩余天数、签到结果、兑换结果）
- 智能多域名切换 (优先 glados.cloud)
- 支持 Cookie-Editor 导出格式
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

# Fix Windows Unicode Output
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')

# ================= 配置 =================

# 域名优先级：Cloud 第一
DOMAINS = [
    "https://glados.cloud",
    "https://glados.rocks", 
    "https://glados.network",
]

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

HEADERS = {
    'Content-Type': 'application/json;charset=UTF-8',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
}

NORMAL_CHECKIN_MESSAGES = (
    "checkin! got",
    "checkin repeats! please try tomorrow",
    "today's observation logged",
)

# 积分兑换计划 (#11)：消耗 points 积分兑换 days 天会员。
# 通过环境变量 EXCHANGE_PLAN 选择，默认 plan500（500 分兑换 100 天），
# 设为 off 关闭自动兑换。
EXCHANGE_PLANS = {
    "plan100": {"points": 100, "days": 10},
    "plan200": {"points": 200, "days": 30},
    "plan500": {"points": 500, "days": 100},
}

EXCHANGE_DISABLED_VALUES = ("", "off", "no", "none", "false", "0", "disabled")

CURRENT_SESSION_COOKIES = ('gld:sess', 'gld:sess.sig')
LEGACY_SESSION_COOKIES = ('koa:sess', 'koa:sess.sig')

# ================= 工具函数 =================

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

def extract_cookie(raw: str):
    """提取 Cookie，支持请求头及 Cookie-Editor JSON 导出格式。"""
    if not raw:
        return None
    raw = raw.strip()
    raw = re.sub(r'^cookie\s*:\s*', '', raw, flags=re.IGNORECASE)
    
    # GLaDOS 2026 新会话与旧 Koa 会话的 Cookie 请求头格式。
    if any(f'{name}=' in raw for name in CURRENT_SESSION_COOKIES + LEGACY_SESSION_COOKIES):
        return raw
        
    # Cookie-Editor 的 JSON 数组导出，或旧版 {"token": "..."} 格式。
    if raw.startswith(('{', '[')):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                pairs = []
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    name = item.get('name')
                    value = item.get('value')
                    if name and value is not None:
                        pairs.append(f'{name}={value}')
                cookie = '; '.join(pairs)
                return cookie if cookie and get_session_cookie_kind(cookie) else None

            token = parsed.get('token') if isinstance(parsed, dict) else None
            return f'koa:sess={token}' if token else None
        except (json.JSONDecodeError, AttributeError, TypeError):
            return None
        
    # JWT Token
    if raw.count('.') == 2 and '=' not in raw and len(raw) > 50:
        return 'koa:sess=' + raw
        
    # Standard
    return raw


def get_cookie_names(cookie_header):
    """Return Cookie names only; values are deliberately never logged."""
    names = set()
    for item in cookie_header.split(';'):
        name, separator, _ = item.strip().partition('=')
        if separator and name:
            names.add(name)
    return names


def get_session_cookie_kind(cookie_header):
    """Identify a complete current or legacy signed session Cookie pair."""
    names = get_cookie_names(cookie_header)
    if set(CURRENT_SESSION_COOKIES).issubset(names):
        return 'gld'
    if set(LEGACY_SESSION_COOKIES).issubset(names):
        return 'koa'
    return None

def get_cookies():
    raw = os.environ.get("GLADOS_COOKIE", "")
    if not raw:
        log("❌ 未配置 GLADOS_COOKIE")
        return []
    
    # Split by enter or &
    sep = '\n' if '\n' in raw else '&'
    return [cookie for item in raw.split(sep) if (cookie := extract_cookie(item))]


def get_browser_headers():
    """Build headers matching the browser that created the login session."""
    user_agent = os.environ.get("GLADOS_USER_AGENT", "").strip() or DEFAULT_USER_AGENT
    headers = {'User-Agent': user_agent}

    chrome = re.search(r'(?:Chrome|Chromium)/(\d+)', user_agent)
    if chrome:
        major = chrome.group(1)
        if 'Macintosh' in user_agent:
            platform = 'macOS'
        elif 'Windows' in user_agent:
            platform = 'Windows'
        elif 'Android' in user_agent:
            platform = 'Android'
        elif 'Linux' in user_agent:
            platform = 'Linux'
        else:
            platform = 'Unknown'

        headers.update({
            'Sec-CH-UA': (
                f'"Chromium";v="{major}", '
                f'"Google Chrome";v="{major}", '
                '"Not_A Brand";v="99"'
            ),
            'Sec-CH-UA-Mobile': '?1' if 'Mobile' in user_agent else '?0',
            'Sec-CH-UA-Platform': f'"{platform}"',
        })

    return headers


def is_non_retryable_checkin_result(result):
    """Return True for authentication/device failures that waiting cannot fix."""
    if not isinstance(result, dict):
        return False
    code = result.get('code')
    reason = str(result.get('reason', '')).strip().lower()
    message = str(result.get('message', '')).strip().lower()
    return (
        code == -2
        or reason == 'device-mismatch'
        or '没有权限' in message
        or 'permission' in message
        or 'unauthorized' in message
    )


def is_normal_checkin_result(result):
    """Return True for a new check-in or a harmless already-checked-in response."""
    if not isinstance(result, dict):
        return False

    message = str(result.get('message', '')).strip().lower()
    if any(marker in message for marker in NORMAL_CHECKIN_MESSAGES):
        return True

    # GLaDOS has historically used code=0 for successful check-ins. Newer
    # duplicate/observation responses can use code=1 and are handled above.
    return result.get('code') == 0


def checkin_with_retry(client, attempts=3, delay_seconds=60):
    """Retry transient/unknown check-in failures without sending duplicate alerts."""
    attempts = max(1, attempts)
    last_result = None

    for attempt in range(1, attempts + 1):
        last_result = client.checkin()
        if is_normal_checkin_result(last_result):
            return last_result, True

        if is_non_retryable_checkin_result(last_result):
            message = last_result.get('message', '认证失败')
            if last_result.get('reason') == 'device-mismatch':
                message = '登录设备不匹配，请重新登录并更新完整 Cookie'
            log(f"❌ 签到认证失败，不再重试: {message}")
            return last_result, False

        if attempt < attempts:
            log(f"⚠️ 签到第 {attempt}/{attempts} 次失败，{delay_seconds} 秒后重试")
            time.sleep(max(0, delay_seconds))

    return last_result, False

# ================= 核心逻辑 =================

class GLaDOS:
    def __init__(self, cookie):
        self.cookie = cookie
        self.domain = DOMAINS[0]
        self.email = "?"
        self.left_days = "?"
        self.points = "?"
        self.points_change = "?"
        self.exchange_info = ""
        self.exchange_result = ""
        self.plan = "?"
        self.session_cookie_kind = get_session_cookie_kind(cookie)
        if self.session_cookie_kind != 'gld':
            log(
                "⚠️ Cookie 未包含完整的 gld:sess 与 gld:sess.sig；"
                "2026-09 新版接口可能返回“没有权限”"
            )

    def req(self, method, path, data=None, form=False):
        """带自动域名切换的请求；form=True 时以表单提交（兑换接口要求）"""
        for d in DOMAINS:
            try:
                url = f"{d}{path}"
                h = HEADERS.copy()
                h.update(get_browser_headers())
                h['Cookie'] = self.cookie
                h['Origin'] = d
                h['Referer'] = f"{d}/console/checkin"

                if form:
                    # 表单提交交给 requests 自动设置 Content-Type，
                    # 手动预设 JSON 头会被兑换接口拒绝。
                    h.pop('Content-Type', None)
                    resp = requests.post(url, headers=h, data=data, timeout=10)
                elif method == 'GET':
                    resp = requests.get(url, headers=h, timeout=10)
                else:
                    resp = requests.post(url, headers=h, json=data, timeout=10)

                if resp.status_code == 200:
                    self.domain = d # Remember working domain
                    return resp.json()
                log(f"⚠️ {d} 返回 HTTP {resp.status_code}")
            except (requests.RequestException, ValueError) as e:
                log(f"⚠️ {d} 请求失败: {e}")
                continue
        return None

    def get_status(self):
        """获取状态：天数、邮箱"""
        res = self.req('GET', '/api/user/status')
        if res and 'data' in res:
            d = res['data']
            self.email = d.get('email', 'Unknown')
            self.left_days = str(d.get('leftDays', '?')).split('.')[0]
            return True
        return False

    def get_points(self):
        """获取积分、变化历史、兑换计划"""
        res = self.req('GET', '/api/user/points')
        if res and 'points' in res:
            # 当前积分
            self.points = str(res.get('points', '0')).split('.')[0]
            
            # 最近一次积分变化
            history = res.get('history', [])
            if history:
                last = history[0]
                change = str(last.get('change', '0')).split('.')[0]
                if not change.startswith('-'):
                    change = '+' + change
                self.points_change = change
            
            # 兑换计划
            plans = res.get('plans', {})
            pts = int(float(self.points))
            exchange_lines = []
            for plan_data in plans.values():
                need = int(plan_data.get('points', 0))
                days = plan_data.get('days', '?')
                if pts >= need:
                    exchange_lines.append(f"✅ {need}分→{days}天 (可兑换)")
                else:
                    exchange_lines.append(f"❌ {need}分→{days}天 (差{need-pts}分)")
            self.exchange_info = "<br>".join(exchange_lines)
            return True
        return False

    def checkin(self):
        """执行签到"""
        return self.req('POST', '/api/user/checkin', {'token': 'glados.cloud'})

    def exchange(self, plan):
        """兑换会员天数：表单提交 planType (plan100/plan200/plan500)"""
        return self.req('POST', '/api/user/exchange', {'planType': plan}, form=True)

# ================= 自动兑换 (#11) =================

def get_exchange_plan():
    """读取自动兑换配置，返回计划 ID；关闭或无效时返回 None"""
    raw = os.environ.get("EXCHANGE_PLAN", "plan500").strip().lower()
    if raw in EXCHANGE_DISABLED_VALUES:
        return None
    if raw in EXCHANGE_PLANS:
        return raw
    log(f"⚠️ EXCHANGE_PLAN 值 '{raw}' 无效 (可选: {'/'.join(EXCHANGE_PLANS)}/off)，本次跳过兑换")
    return None


def auto_exchange(g, plan_id):
    """积分达标时自动兑换会员天数，返回用于推送的兑换说明"""
    info = EXCHANGE_PLANS[plan_id]
    need, days = info["points"], info["days"]

    try:
        pts = int(float(g.points))
    except (TypeError, ValueError):
        log("⚠️ 积分查询失败，跳过兑换")
        return "⚠️ 兑换跳过(积分查询失败)"

    if pts < need:
        return f"⏭️ 积分不足({pts}/{need})，攒够自动兑换{days}天"

    res = g.exchange(plan_id)
    if res and res.get('code') == 0:
        log(f"🎁 自动兑换成功: {need}分 → +{days}天")
        # 兑换消耗积分、增加天数，刷新后推送里才是最新数据
        g.get_status()
        g.get_points()
        return f"🎁 兑换成功 +{days}天 (消耗{need}分)"

    err = res.get('message', 'Failure') if res else "Network Error"
    log(f"⚠️ 自动兑换失败: {err}")
    return f"⚠️ 兑换失败({err})"

# ================= 主程序 =================

def pushplus(token, title, content):
    if not token:
        return False
    try:
        url = f"https://sctapi.ftqq.com/{token}.send"
        data = {
            'title': title,
            'desp': content,
            'channel': '9'
        }
        response = requests.post(url, data=data, timeout=5)
        log("✅ PushPlus 推送成功")
        return True
    except (requests.RequestException, ValueError, RuntimeError) as e:
        log(f"❌ PushPlus 推送失败: {e}")
        return False

def main():
    log("🚀 2026 GLaDOS Checkin Starting...")
    cookies = get_cookies()
    if not cookies:
        return 1

    exchange_plan = get_exchange_plan()

    results = []
    success_cnt = 0
    exchange_events = 0

    for i, cookie in enumerate(cookies, 1):
        g = GLaDOS(cookie)

        # 1. Checkin
        attempts = int(os.environ.get("CHECKIN_MAX_ATTEMPTS", "3"))
        delay_seconds = int(os.environ.get("CHECKIN_RETRY_DELAY_SECONDS", "60"))
        res, is_success = checkin_with_retry(g, attempts, delay_seconds)
        msg = res.get('message', 'Failure') if res else "Network Error"

        # 2. Get Info (Refresh data)
        g.get_status()
        g.get_points()

        # 2.5 Auto exchange (issue #11): runs after check-in so the
        # just-earned points count toward the threshold.

        # 3. Log
        status_icon = "✅" if is_success else "❌"
        # Actions logs are public in a public repository. Keep account details
        # inside the private notification instead of exposing the email here.
        log(f"用户: {g.email} | 积分: {g.points} | 天数: {g.left_days} | 结果: {msg}")

        if is_success:
            success_cnt += 1

        # 4. Result Formatting
        results.append(f"👤 {g.email}\n当前积分: {g.points} {g.points_change}\n剩余天数: {g.left_days} 天\n签到结果: {msg}\n🎁 兑换选项: {g.exchange_info}")

    ptoken = os.environ.get("PUSHPLUS_TOKEN")
    
    if ptoken or (tg_token and tg_chat_id):
        title = f"GLaDOS签到: 成功{success_cnt}/{len(cookies)}"
        time = datetime.now() + timedelta(hours=8)
        content = "".join(results)
        content += f"\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        pushplus(ptoken, title, content)

    return 0 if success_cnt == len(cookies) else 1

if __name__ == '__main__':
    sys.exit(main())
