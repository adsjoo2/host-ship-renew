# Host-Ship Auto Renew

基于 GitHub Actions + `patchright` + **Cloudflare WARP** 的 [Host-Ship](https://panel.host-ship.com/) 免费服务器自动续期脚本。

## ✨ 核心特性

- **免代理节点配置**：采用 `fscarmen/warp-on-actions` 系统级 WARP 网络出网，告别 `sing-box` 和 `PROXY_URL` 节点维护。
- **高通过率过盾**：结合 `patchright` + `xvfb` 虚拟显示器 + 真实 Chrome，自动对齐 WARP 出口 IP 时区，轻松通过 Cloudflare Turnstile 验证。
- **智能换 IP 重试**：当遇到验证失败或 IP 信誉不佳时，自动通过 `warp-cli` 重新注册并更换全新 WARP 出口 IP 重试（最多 3 次）。
- **截图留证 & 自动保活**：每次运行自动保存结果或错误截图至 Actions Artifact，并自动更新 `keep_alive.txt` 防止仓库因长期无提交而停用定时任务。

---

## 🚀 快速配置

1. **Fork 或克隆本仓库**。
2. 进入仓库 **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**，添加以下两个环境变量：

| Secret 名称 | 必填 | 说明 |
| :--- | :---: | :--- |
| `PANEL_USERNAME` | ✅ | Host-Ship 面板登录用户名或邮箱 |
| `PANEL_PASSWORD` | ✅ | Host-Ship 面板登录密码 |

3. 进入仓库 **Actions** 页面，启用 Workflows。

---

## ⏰ 运行方式

- **自动运行**：默认每 5 天（`15 3 */5 * *`）自动执行一次续期检查。
- **手动触发**：进入 **Actions** -> **HostShip Auto Renew** -> 点击 **Run workflow** 即可立即测试运行。
- **查看结果**：运行结束后，可在对应 Action 任务底部下载 `debug-screenshots` 查看续期结果截图（`renew_result.png`）。
