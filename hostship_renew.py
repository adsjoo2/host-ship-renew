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
    from patchright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    USING_PATCHRIGHT = False

PANEL_URL = "https://panel.host-ship.com/"
LOGIN_URL = PANEL_URL
SERVER_LIST_URL = "https://panel.host-ship.com/server"


def log(msg):
    print(f"[INFO] {msg}")


def warn(msg):
    print(f"[WARN] {msg}")


def err(msg):
    print(f"[ERROR] {msg}")


def restart_warp():
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
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "disconnect"], check=False, timeout=30, capture_output=True)
        time.sleep(3)
        try:
            subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "delete"], check=True, timeout=30, capture_output=True)
        except subprocess.CalledProcessError:
            warn("删除 WARP 注册失败（可能未注册），继续...")
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "new"], check=True, timeout=30, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "connect"], check=True, timeout=30, capture_output=True)
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


CF_TITLE_HINTS = ("just a moment", "attention required", "请稍候", "执行安全验证")


def _cf_challenge_active(page):
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
    for frame in page.frames:
        if "challenges.cloudflare.com" not in (frame.url or ""):
            continue
        try:
            box = frame.frame_element().bounding_box()
            if not box or box["width"] < 100:
                continue
            page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
            log("已尝试点击 Cloudflare Turnstile 复选框")
            return True
        except Exception:
            continue
    return False


def wait_for_cloudflare(page, timeout=60):
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
    return False


def goto_safe(page, url, timeout=60000):
    page.goto(url, timeout=timeout)
    wait_for_cloudflare(page)


def wait_turnstile_token(page, timeout=45):
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

        username_input.first.click()
        username_input.first.press_sequentially(username, delay=60)
        password_input.first.click()
        password_input.first.press_sequentially(password, delay=60)
        log("已填入用户名和密码")

        if wait_turnstile_token(page):
            passed = True
            break
        warn("Turnstile 未通过（可能显示 Verification failed），刷新页面重试")
        page.wait_for_timeout(3000)

    if not passed:
        raise RuntimeError("Turnstile 验证失败：当前 WARP 出口 IP 或浏览器指纹被 Cloudflare 拒绝")

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
        raise RuntimeError("登录后仍能看到密码输入框，可能账号密码错误，或页面上出现了验证码/人机校验等未处理的提示")

    log(f"登录成功，当前 URL: {page.url}")


def find_server_links(page):
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
    full_url = url if url.startswith("http") else f"{PANEL_URL.rstrip('/')}{url}"
    log(f"打开服务器详情页: {full_url}")
    goto_safe(page, full_url)
    page.wait_for_timeout(3000)

    days_left = get_renewal_days_remaining(page)
    if days_left is not None:
        log(f"距离到期还剩 {days_left} 天")
        if days_left >= 13:
            log("剩余天数充足，无需续期，跳过当前服务器")
            return True
    else:
        warn("未能读取到剩余天数，继续尝试查找续期按钮")

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

    try:
        pattern = re.compile(r'(Add \d+ days|Renew now|Confirm|确认|Yes|OK)', re.IGNORECASE)
        confirm_btn = page.locator('button', has_text=pattern)
        
        if confirm_btn.count() > 0 and confirm_btn.first.is_visible(timeout=3000):
            matched_text = confirm_btn.first.inner_text()
            confirm_btn.first.click()
            log(f"点击了弹窗确认按钮 (匹配文本: {matched_text})")
            page.wait_for_timeout(3000)
        else:
            warn("未找到弹窗内的确认按钮")
    except Exception as e:
        warn(f"查找确认按钮发生异常: {e}")

    days_left_after = get_renewal_days_remaining(page)
    if days_left_after is not None and days_left is not None:
        if days_left_after > days_left:
            log(f"剩余天数已从 {days_left} 变为 {days_left_after}，续期成功生效")
        elif days_left_after == days_left:
            warn("点击后剩余天数未变化，续期可能未被面板接受")
        else:
            log(f"剩余天数显示异常（从 {days_left} 变为 {days_left_after}），请以实际面板为准")

    return True


def run_browser_session(username, password, headless):
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
    log(f"自动化引擎: {'patchright' if USING_PATCHRIGHT else 'playwright'}")
    log("网络模式: Cloudflare WARP 系统级网络")

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
