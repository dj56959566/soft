# Nezha watchdog — 哪吒自动巡检

# watchdog2.py all-watchdog.yml------是拉启服务目录下的所有应用

每 5 分钟查一次两个哪吒面板，掉了就自动恢复，不用再手动进 Northflank。

- `https://nf.djbadboy.nyc.mn/` — Northflank 项目 `test`，服务 `nezha`
- `https://nf.djsb.nyc.mn/` — Northflank 项目 `bbqtest`，服务 `docker`

## 它怎么工作

1. GET 面板域名，200 = 正常；1033 / 530 / 超时 / 其他状态 = 掉线，20 秒后复查一次防误报。
2. 掉线就调 Northflank API：先 `POST /projects/{project}/services/{service}/resume {"instances": 1}`，409（没暂停）就改调 `scale {"instances": 1}`，再不行调 `restart`。
3. 等 120 秒再查一次。恢复了就 TG 报一声，没恢复就 TG 报警，手动处理。
4. 正常时什么消息都不发，不刷屏。

## 部署步骤

1. 在 GitHub 新建一个公开仓库（建议名 `nezha-watchdog`），把 `watchdog.py` 和 `.github/workflows/nezha-watchdog.yml` 传上去。
2. Northflank 建 API Token：
   - Team Settings > API > Roles：新建一个角色，给 `Project > Services > General > Read` + `Update`。
   - Team Settings > API > Tokens > Create API token：选上面那个角色，名字 `nezha-watchdog`，Token 只显示一次，复制好。
   - 注意：两个服务如果不在同一个 Team，一个 Token 可能只够管一个。先用一个试，第二个恢复失败（403）再加第二个 Token。
3. GitHub 仓库 Settings > Secrets and variables > Actions > New repository secret：
   - `NF_API_TOKEN` = 上一步的 Token（不要贴到聊天里，不要提交进仓库）。
   - 可选：`TG_BOT_TOKEN`（Telegram Bot Token）、`TG_CHAT_ID`（要收提醒的 Chat ID）。
4. Actions > Nezha watchdog > Run workflow，手动跑一次，看日志：
   - 两个都是 OK = 巡检正常。
   - 如果 resume 返回 404，说明 project/service 写错了：看 Northflank 地址栏 `.../project/<projectId>/services/<serviceId>/...` 或 `northflank forward service` 的输出，改 `watchdog.py` 里 `SERVICES` 那两行。

## 已知

- 2026-11-14 08:20（北京时间），第一个服务收到 SIGTERM 停掉，之后一直 0 实例，面板报 Cloudflare Tunnel Error 1033；2026-11-15 18:26 手动恢复后正常。
- 免费项目没有自动缩放功能，所以停不是自动缩放缩到 0 的；Northflank 官方也说没有闲置自动暂停。关停原因至今未在审计日志里找到，两个服务一起停，更像是平台侧的原因。
- GitHub 定时任务 60 天无仓库活动会被停用，万一巡检停了，进 Actions 手动跑一次即可。
