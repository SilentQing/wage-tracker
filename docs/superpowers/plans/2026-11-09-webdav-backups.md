# WebDAV Backups Implementation Plan

**Goal:** 将 VPS 已校验的本地备份可靠上传到用户的飞牛 NAS。
**Architecture:** 显式启用的 HTTPS WebDAV 适配器；持久 SQLite 队列与独立后台上传线程；PUT 后 GET 校验 JSON 和 meta 文件。网络期间不持有数据库写锁。使用租约串行上传，失败退避重试。
**Tech Stack:** Python 标准库 http.client/ssl、SQLite、FastAPI、原生 JS；不增加依赖。
**Spec:** 聊天中用户已批准：上传 JSON＋校验文件、远端 SHA256 验证、重启续传、失败保留本地、独立远端状态及立即重试；当前目标 NAS WebDAV 尚未创建，默认关闭。

## Constraints and Review Focus
- 保留现有未提交修改，不部署、不改真实账本、不擅自创建 NAS 服务。
- 只允许 HTTPS，证书验证开启，不跟随重定向；错误及状态不能泄露地址、密码或响应正文。
- 成功必须两文件均上传并下载校验；部分成功/响应丢失可按相同文件名安全重试。
- 重启/并发服务进程不得重复领取活动任务；过期租约能恢复。
- 上传失败不改变本地成功状态，待传文件不被30份保留策略删除。
- 启用后补传现有受管理定时/手动备份；不上传 before-restore 等无 manifest 文件。

## Steps
- [x] webdav_client.py：配置校验、HTTPS PUT/GET、大小和超时边界、脱敏错误；tests/test_webdav.py 验证。
- [x] backup_service.py：队列、领取租约、失败重试、独立状态和保留规则；tests/test_remote_backups.py 验证。
- [x] app.py 重试接口，static/backups.js 和模板展示独立状态。
- [x] Dockerfile、Compose、.env.example 与 README 完整接线并说明飞牛接入步骤。
- [x] 完整后端、JS语法、浏览器回归与差异复核；记录真实NAS未接入的验证边界。

## Verification — 2026-11-09
- `D:\Codex\FN\wage-tracker\.venv\Scripts\python.exe -m unittest discover -s tests -q`：56项通过。
- 同解释器运行 `tests/browser_payroll.py`：Edge无头手机/桌面回归通过，远端未启用/失败/重试/成功通过路由模拟验证，无页面错误。
- `node --check` 三个前端JS及 `git diff --check` 通过。
- 测试显式关闭真实WebDAV配置；协议测试使用模拟HTTPS连接，不连接NAS。
- NAS服务和目录尚未创建，未执行真实上传、VPS部署或Docker构建。默认开关保持false，后续按README完成接入验收。
