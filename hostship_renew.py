#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import shutil
import subprocess
import time
import requests

try:
    # patchright 是 playwright 的直接替代品，修补了 CDP 自动化特征泄露，更容易通过 Turnstile
    from patchright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    USING_PATCHRIGHT = False

PANEL_URL = "https://panel.host-ship.com/"
LOGIN_URL = PANEL_URL  # 面板首页会自动展示登录表单（如未跳转，改成 https://panel.host-ship.com/login）
SERVER_LIST_URL = "https://panel.host-ship.com/server"


def log(msg):
    print(f"[INFO] {msg}")


def warn(msg):
    print(f"[WARN] {msg}")


def err(msg):
    print(f"[ERROR] {msg}")


# ----------------------------------------------------------------------------
# Cloudflare WARP 网络管理（参考 katabump-renew 做法）
# ----------------------------------------------------------------------------
def restart_warp():
    """断开并重新注册 WARP，更换系统级出口 IP。"""
    if not shutil.which("warp-cli"):
        warn("未找到 warp-cli，跳过 WARP 重连（本地直连运行时无需此步骤）")
        return False
    log("正在重启 WARP 以更换出口 IP...")
    try:
        old_ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
        log(f"当前 WARP IP: {old_ip}")
    except Exception:
        old_ip = "未知"

    try:
        subprocess.run(
            ["sudo", "warp-cli", "--accept-tos", "disconnect"],
            check=False, timeout=30, capture_output=True,
        )
        time.sleep(3)
        try:
            subprocess.run(
                ["sudo", "warp-cli", "--accept-tos", "registration", "delete"],
                check=True, timeout=30, capture_output=True,
            )
        except subprocess.CalledProcessError:
            warn("删除 WARP 注册失败（可能未注册），继续...")
        subprocess.run(
            ["sudo", "warp-cli", "--accept-tos", "registration", "new"],
            check=True, timeout=30, capture_output=True,
        )
        time.sleep(3)
        subprocess.run(
            ["sudo", "warp-cli", "--accept-tos", "connect"],
            check=True, timeout=30, capture_output=True,
        )
        time.sleep(10)
        new_ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
        log(f"WARP 重连成功，新 IP: {new_ip}")
        return True
    except FileNotFoundError:
        warn("未找到 warp-cli，跳过 WARP 重连（本地直连运行时无需此步骤）")
        return False
    except Exception as e:
        err(f"WARP 重连失败: {e}")
        return False


# ----------------------------------------------------------------------------
# Cloudflare 过盾相关
# ----------------------------------------------------------------------------
CF_TITLE_HINTS = ("just a moment", "attention required", "请稍候", "执行安全验证")


def _cf_challenge_active(page):
    """判断当前是否停留在 Cloudflare 全页挑战（Just a moment...）"""
    try:
        title = (page.title() or "").lower()
    except Exception:
        return False
    if any(h in title for h in CF_TITLE_HINTS):
        return True
    try:
        return page.locator("#challenge-running, #challenge-form, .cf-browser-verification").count() > 0
    except Exception:
        return False


def _click_turnstile(page):
    """尝试点击 Turnstile 复选框（best effort）。点到返回 True"""
    for frame in page.frames:
        if "challenges.cloudflare.com" not in (frame.url or ""):
            continue
        try:
            box = frame.frame_element().bounding_box()
            if not box or box["width"] < 100:
                continue
            # 复选框位于 iframe 左侧，垂直居中
            page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
            log("已尝试点击 Cloudflare Turnstile 复选框")
            return True
        except Exception:
            continue
    return False


def wait_for_cloudflare(page, timeout=60):
    """
    等待 Cloudflare 挑战通过。没有挑战则立即返回 True；超时返回 False。
    期间如果出现 Turnstile 复选框，会尝试点击。
    """
    if not _cf_challenge_active(page):
        return True
    log("检测到 Cloudflare 验证页，等待通过...")
    end = time.time() + timeout
    clicked_at = 0
    while time.time() < end:
        page.wait_for_timeout(2000)
        if not _cf_challenge_active(page):
            log("Cloudflare 验证已通过")
            return True
        if time.time() - clicked_at > 8 and _click_turnstile(page):
            clicked_at = time.time()
    warn("等待 Cloudflare 验证超时（可能当前 WARP IP 信誉不佳）")
    try:
        page.screenshot(path="cf_blocked.png", full_page=True)
    except Exception:
        pass
    return False


def goto_safe(page, url, timeout=60000):
    """page.goto + 自动等待 Cloudflare 挑战"""
    page.goto(url, timeout=timeout)
    wait_for_cloudflare(page)


def wait_turnstile_token(page, timeout=45):
    """
    等待登录页内嵌的 Turnstile 自动通过（出现非空 token）。没有该组件则直接返回 True。
    前 12 秒只等待不点击（托管模式通常会自动通过，乱点反而容易被判为机器人），之后才尝试点击。
    """
    sel = 'iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]'
    if page.locator(sel).count() == 0:
        return True
    log("登录页包含 Turnstile 组件，等待其自动通过...")
    start = time.time()
    last_click = 0
    while time.time() - start < timeout:
        try:
            token = page.locator('input[name="cf-turnstile-response"]')
            if token.count() > 0 and token.first.input_value():
                log(f"Turnstile 已通过（用时 {time.time() - start:.0f}s）")
                return True
        except Exception:
            pass
        if time.time() - start > 12 and time.time() - last_click > 8 and _click_turnstile(page):
            last_click = time.time()
        page.wait_for_timeout(1000)
    return False


def probe_exit(browser):
    """
    用一个临时上下文访问 IP 查询接口，返回 {"ip":..., "tz":...}。
    ip 用来确认 WARP 出口生效；tz 是出口 IP 所在时区，后面用来设置浏览器时区，
    避免“浏览器是 UTC、IP 却在别的国家”这种 Turnstile 会检查的不一致。
    失败返回 None。
    """
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        try:
            page.goto("https://ipwho.is/", timeout=20000)
            d = json.loads(page.inner_text("body", timeout=5000))
            if d.get("success", True) and d.get("ip"):
                tz = (d.get("timezone") or {}).get("id")
                log(f"当前浏览器出口 IP: {d['ip']}  ({d.get('country', '?')}, 时区 {tz})")
                return {"ip": d["ip"], "tz": tz}
        except Exception:
            pass
        try:
            page.goto("https://api.ipify.org", timeout=20000)
            ip = page.inner_text("body", timeout=5000).strip()
            if re.fullmatch(r"[0-9a-fA-F:.]{7,45}", ip):
                log(f"当前浏览器出口 IP: {ip}")
                return {"ip": ip, "tz": None}
        except Exception:
            pass
        warn("无法从浏览器获取出口 IP（查询接口可能超时）")
        return None
    finally:
        ctx.close()


def login(page, username, password, max_attempts=2):
    """
    登录面板。单次浏览器会话内最多刷新重试 max_attempts 次；
    若仍未过盾则抛出异常交由外层重启 WARP 更换 IP。
    """
    passed = False
    for attempt in range(1, max_attempts + 1):
        log(f"正在打开登录页...（当前会话第 {attempt}/{max_attempts} 次）")
        goto_safe(page, LOGIN_URL)
        page.wait_for_timeout(2000)

        username_input = page.get_by_placeholder("Username or Email")
        password_input = page.get_by_placeholder("Password")
        if username_input.count() == 0:
            raise RuntimeError('未找到 placeholder="Username or Email" 的输入框，可能被 Cloudflare 拦截或页面结构已变化')
        if password_input.count() == 0:
            raise RuntimeError('未找到 placeholder="Password" 的输入框，可能被 Cloudflare 拦截或页面结构已变化')

        # 像真人一样逐字输入，而不是瞬间填充
        username_input.first.click()
        username_input.first.press_sequentially(username, delay=60)
        password_input.first.click()
        password_input.first.press_sequentially(password, delay=60)
        log("已填入用户名和密码")

        if wait_turnstile_token(page):
            passed = True
            break
        warn("Turnstile 未通过（可能显示 Verification failed），刷新页面重试")
        try:
            page.screenshot(path=f"turnstile_failed_{attempt}.png", full_page=True)
        except Exception:
            pass
        page.wait_for_timeout(3000)

    if not passed:
        raise RuntimeError("Turnstile 验证失败：当前 WARP 出口 IP 或浏览器指纹被 Cloudflare 拒绝")

    # Turnstile 通过后按钮会启用；用提交按钮兜底，避免依赖按钮文字
    sign_in_btn = page.get_by_role("button", name="Sign In")
    if sign_in_btn.count() == 0:
        sign_in_btn = page.locator('button:has-text("Sign In"), form button[type="submit"], button[type="submit"]')
    if sign_in_btn.count() == 0:
        raise RuntimeError('未找到登录按钮，页面结构可能已变化')

    sign_in_btn.first.click()
    log("已点击登录按钮")

    page.wait_for_timeout(3000)
    wait_for_cloudflare(page)

    if page.get_by_placeholder("Password").count() > 0:
        screenshot_path = "login_failed.png"
        try:
            page.screenshot(path=screenshot_path, full_page=True)
            err(f"已保存失败截图: {screenshot_path}（会作为 Actions Artifact 上传，可下载查看）")
        except Exception as e:
            warn(f"截图保存失败: {e}")
        raise RuntimeError("登录后仍能看到密码输入框，可能账号密码错误，或页面上出现了验证码/人机校验等未处理的提示")

    log(f"登录成功，当前 URL: {page.url}")


def find_server_links(page):
    """收集所有服务器详情页链接。"""
    manage_links = page.locator('a:has-text("Manage Server")')
    count = manage_links.count()
    if count > 0:
        hrefs = []
        for i in range(count):
            href = manage_links.nth(i).get_attribute("href")
            if href and href not in hrefs:
                hrefs.append(href)
        log(f"在当前页（Dashboard）通过 'Manage Server' 按钮找到 {len(hrefs)} 个服务器")
        return hrefs

    log(f"当前页未找到服务器卡片，尝试访问服务器列表页: {SERVER_LIST_URL}")
    goto_safe(page, SERVER_LIST_URL)
    page.wait_for_timeout(3000)

    manage_links = page.locator('a:has-text("Manage Server")')
    count = manage_links.count()
    if count > 0:
        hrefs = []
        for i in range(count):
            href = manage_links.nth(i).get_attribute("href")
            if href and href not in hrefs:
                hrefs.append(href)
        log(f"通过 'Manage Server' 按钮找到 {len(hrefs)} 个服务器")
        return hrefs

    candidate_selectors = [
        'a[href*="/server/"]',
        'a[href*="/servers/"]',
        'a[href*="/service/"]',
        'a[href*="/services/"]',
    ]
    for sel in candidate_selectors:
        links = page.locator(sel)
        count = links.count()
        if count > 0:
            log(f"使用选择器 '{sel}' 找到 {count} 个服务器链接")
            hrefs = []
            for i in range(count):
                href = links.nth(i).get_attribute("href")
                if href and href not in hrefs:
                    hrefs.append(href)
            return hrefs
    warn("未匹配到任何服务器链接选择器，请检查页面结构并更新 candidate_selectors")
    return []


def get_renewal_days_remaining(page):
    """读取页面上 'Renewal in X Days' 卡片的剩余天数。"""
    try:
        el = page.locator('text=/\\d+\\s*Days?/i').first
        if el.count() > 0:
            text = el.inner_text(timeout=3000)
            m = re.search(r'(\d+)\s*Days?', text, re.IGNORECASE)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def renew_server(page, url):
    """进入单个服务器详情页并点击续期，包含天数阈值判断与动态按钮匹配"""
    full_url = url if url.startswith("http") else f"{PANEL_URL.rstrip('/')}{url}"
    log(f"打开服务器详情页: {full_url}")
    goto_safe(page, full_url)
    page.wait_for_timeout(3000)

    days_left = get_renewal_days_remaining(page)
    if days_left is not None:
        log(f"距离到期还剩 {days_left} 天")
        # 优化判断：如果剩余天数 >= 13 天，接近上限，直接跳过
        if days_left >= 13:
            log("剩余天数充足，无需续期，跳过当前服务器")
            return True
    else:
        warn("未能读取到剩余天数，继续尝试查找续期按钮")

    # 点击详情页的第一层 Renew 按钮
    renew_btn_texts = ["Renew", "续期", "Renouveler"]
    target_btn = None
    for text in renew_btn_texts:
        btn = page.locator(f'button:has-text("{text}")')
        if btn.count() > 0 and btn.first.is_visible():
            target_btn = btn.first
            break

    if target_btn is None:
        warn(f"未在 {full_url} 找到续期按钮")
        return False

    try:
        if target_btn.is_disabled():
            log("续期按钮当前不可点击（可能已达 Maximum 14 days reached），跳过")
            return False
    except Exception:
        pass

    target_btn.scroll_into_view_if_needed()
    target_btn.click()
    log("已点击主界面续期按钮，等待弹窗...")
    page.wait_for_timeout(2500)

    # 弹窗内确认按钮：使用正则表达式匹配动态的 "Add X days" 或常规确认词
    try:
        # re.IGNORECASE 忽略大小写，匹配 Add 任意数字 days
        pattern = re.compile(r'(Add \d+ days|Renew now|Confirm|确认|Yes|OK)', re.IGNORECASE)
        confirm_btn = page.locator('button', has_text=pattern)
        
        if confirm_btn.count() > 0 and confirm_btn.first.is_visible(timeout=3000):
            matched_text = confirm_btn.first.inner_text()
            confirm_btn.first.click()
            log(f"点击了弹窗确认按钮 (匹配文本: {matched_text})")
            page.wait_for_timeout(2000)
        else:
            warn("未找到弹窗内的确认按钮")
    except Exception as e:
        warn(f"查找确认按钮发生异常: {e}")

    # 统一截图留证（仅截取弹窗局部，保护背景隐私数据）
    try:
        modal = page.locator('[role="dialog"], .modal-content, .modal').first
        if modal.count() > 0 and modal.is_visible():
            modal.screenshot(path="renew_result.png")
            log("已保存【续期弹窗】局部截图: renew_result.png")
        else:
            page.locator('div:has-text("Renew server")').last.screenshot(path="renew_result.png")
            log("已保存【续期提示区域】截图: renew_result.png")
    except Exception as e:
        warn(f"局部截图保存失败: {e}")

    days_left_after = get_renewal_days_remaining(page)
    if days_left_after is not None and days_left is not None:
        if days_left_after != days_left:
            log(f"剩余天数已从 {days_left} 变为 {days_left_after}，续期生效")
        else:
            warn("点击后剩余天数未变化，续期可能未被面板接受")

    return True


def run_browser_session(username, password, headless):
    """单次浏览器会话：走系统级 WARP 网络完成探测出口、登录及续期。成功返回 True，登录/过盾失败返回 False。"""
    launch_kwargs = {
        "headless": headless,
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled"],
    }

    with sync_playwright() as playwright:
        browser = None
        for channel in ([os.environ.get("BROWSER_CHANNEL", "chrome")] if os.environ.get("BROWSER_CHANNEL", "chrome") else []) + [None]:
            try:
                kw = dict(launch_kwargs)
                if channel:
                    kw["channel"] = channel
                browser = playwright.chromium.launch(**kw)
                log(f"浏览器已启动: {channel or '内置 Chromium'}")
                break
            except Exception as e:
                warn(f"启动 {channel or 'Chromium'} 失败: {str(e).splitlines()[0]}")
        if browser is None:
            err("无法启动浏览器")
            return False

        page = None
        try:
            exit_info = probe_exit(browser)

            ctx_kwargs = {"no_viewport": True}
            if exit_info and exit_info.get("tz"):
                ctx_kwargs["timezone_id"] = exit_info["tz"]
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()

            login(page, username, password)

            hrefs = find_server_links(page)
            if not hrefs:
                err("未找到任何服务器，流程终止")
                return True

            success_count = 0
            for idx, href in enumerate(hrefs):
                log(f"--- 处理第 {idx + 1}/{len(hrefs)} 个服务器 ---")
                try:
                    if renew_server(page, href):
                        success_count += 1
                except PlaywrightTimeout:
                    warn(f"处理 {href} 超时，跳过")
                except Exception as e:
                    warn(f"处理 {href} 时出错: {e}")

            log(f"全部处理完成，成功续期 {success_count}/{len(hrefs)} 个服务器")
            return True

        except Exception as e:
            err(f"本次会话执行失败: {e}")
            if page is not None:
                try:
                    page.screenshot(path="error.png", full_page=True)
                    err("已保存错误截图: error.png（会作为 Actions Artifact 上传，可下载查看）")
                except Exception as shot_err:
                    warn(f"错误截图保存失败: {shot_err}")
            return False
        finally:
            browser.close()


def main():
    username = os.environ.get("PANEL_USERNAME", "").strip()
    password = os.environ.get("PANEL_PASSWORD", "").strip()

    if not username or not password:
        err("未设置 PANEL_USERNAME 或 PANEL_PASSWORD 环境变量")
        return

    headless = os.environ.get("HEADLESS", "true").strip().lower() not in ("0", "false", "no")
    log(f"自动化引擎: {'patchright' if USING_PATCHRIGHT else 'playwright（建议安装 patchright 以提高过盾率）'}")
    log("网络模式: Cloudflare WARP 系统级网络（不再使用 sing-box / PROXY_URL 代理）")

    max_warp_attempts = 3
    for attempt in range(1, max_warp_attempts + 1):
        log(f"=== 第 {attempt}/{max_warp_attempts} 次尝试 ===")
        if attempt > 1:
            log("尝试通过重启 WARP 更换出口 IP 后重新登录...")
            if not restart_warp():
                warn("WARP 未能更换 IP，停止重试")
                break

        if run_browser_session(username, password, headless):
            return

    err("多次尝试更换 WARP IP 后仍未能完成登录续期，流程终止")


if __name__ == "__main__":
    main()
