# 鱼干量化交易机器人

针对 `https://raricy.com/fish/trade` 的 BTC 模拟盘。Python 3.11+，SQLite 持久化，Docker Compose 常驻运行；运行时代码仅使用标准库。

**默认是影子模式，不向网站提交买卖。** 策略本金 50 条鱼干，BTC、1 倍、单仓。实现不保证盈利；回测及运行门槛未通过时，程序拒绝启动真实鱼干交易。

## 当前交付

- 4h EMA50/EMA200 趋势过滤、1h 20 根高点突破、Wilder ATR14 止损和跟踪止损。
- 单笔计划风险 0.25%、投入上限 20%、6 根完整小时冷却、日亏损 1% 退出、净值高点回撤 5% 停机复核。
- 与网站一致的杠杆结算公式和 0.0001 鱼干精度；策略实际固定 1 倍。
- 下单前持久化意图、幂等重试、响应丢失恢复、独立策略账本、登录到期管理。
- 经 SHA-256 校验的 Binance 历史下载、24 个月分段回测、交易成本压力测试、三种基准。
- SMTP 告警、独立进程看门狗、单实例锁、部署配置和故障测试。

依据公开源码版本 `9f5006acb5b0bae14625f55e4b73f669c245ac43`：
[结算](https://github.com/raricycms/raricy.com/blob/9f5006acb5b0bae14625f55e4b73f669c245ac43/src/lib/market-math.ts)、
[交易服务](https://github.com/raricycms/raricy.com/blob/9f5006acb5b0bae14625f55e4b73f669c245ac43/src/lib/market-service.ts)、
[认证](https://github.com/raricycms/raricy.com/blob/9f5006acb5b0bae14625f55e4b73f669c245ac43/src/lib/session.ts)。

本地的 `reports/backtest/backtest.json` 是实际回测结果。`not_qualified` 表示不能上线，不能通过改 JSON 或放宽阈值来绕过。代码变化会使已有上线证据失效。

## 本地验证

```sh
python3 -m unittest discover -s tests -v
python3 -m fishbot drills
python3 -m fishbot download --start 2024-03 --end 2026-08
python3 -m fishbot backtest --start 2024-09 --output data/evidence
```

下载为 2024-03 至 2026-08：前 6 个月预热，2024-09～2025-08 为开发段，2025-09～2026-02 为验证段，2026-03～2026-08 为封存测试段。策略参数固定；不会自动按测试段收益调参。以后更新评估日期，应同时更新下载范围，保留至少 167 天预热。

回测数据在 `data/history/`，不进入 Git。`manifest.json` 保存数据源校验值。每段独立从 50 条开始；历史数据仅用于预热，分段边界不继承仓位。强制在段末退出的仓位单独记为 `boundary_closures`，不计入 30 笔自然完成交易的门槛。

## 登录和影子运行

先在终端执行正常账号登录，密码只用于向 `raricy.com` 的登录接口换取会话，不保存密码：

```sh
python3 -m fishbot login
python3 -m fishbot doctor
python3 -m fishbot run
```

`login` 使用不可见密码输入，生成权限 600 的 `secrets/session.json`。请使用独立登录会话。`doctor` 只读核对行情、费率、最小金额、余额、持仓快照及网站与 Binance 的已收盘行情。

影子模式也要求有效登录，以验证站点行情及会话可靠性，但模拟成交仅写本地。默认按每次成交 0.10% 的不利偏差模拟。用另一终端检查：

```sh
python3 -m fishbot status
python3 -m fishbot watchdog
```

在 `config.toml` 配置 SMTP。SSL 使用 465；STARTTLS 通常使用 587。授权码写入 `secrets/smtp-password`，权限必须为 600。然后执行 `python3 -m fishbot email-test`。此命令会向配置的收件人发送测试邮件。

## 开始交易的门槛

真实交易模式指消耗网站鱼干，不涉及交易所资金。按照以下顺序启用：

1. 当前代码的回测最终测试段通过净收益、回撤、压力测试和至少 30 笔完整交易要求。
2. `drills` 全部通过；连续健康影子运行至少 14 天。行情/会话异常或超过 60 秒的运行间断会重新计时。
3. 停止影子进程后立即执行 `qualify-shadow`，要求最近心跳在 60 秒内且健康、账户无真实持仓；先确保影子仓位已退出。证据有效期 24 小时。
4. 准备独立 `canary.toml`：复制配置，将 `mode` 设为 `canary`、`database` 设为 `data/canary.sqlite3`。执行 `python3 -m fishbot --config canary.toml canary`。仅用 1 条鱼干开仓，核对后立即平仓；该验证会产生小额手续费和行情损益。
5. 准备独立 `live.toml`：`mode = "live"`、`database = "data/live.sqlite3"`。执行 `gates` 检查，然后 `run`。策略预算固定 50 条，单次最多 10 条起步。

三个模式的数据库不能混用。仅在账户无已有仓位时初始化。不要与另一个机器人或人工同时交易 BTC/ETH；发现未知仓位或非本机器人交易时会暂停并报警。签到和转账流水不会改变策略本金或利润。

## 重要运行约束

- 没有服务端止损。断网、会话失效、平台拒单时，仓位仍可能存在；持久化退出意图会继续重试。
- 网站缺少独立持仓 API。客户端从交易页的 Next.js 序列化数据读取持仓，仅 JSON 解析、不执行脚本。接口或页面结构变化会触发暂停。这部分必须用 `doctor` 在部署环境核验。
- 若买入请求结果不明且对账不能确认，禁止新开仓并告警。未成交且明确被拒绝的过期信号会放弃；真正不确定的订单留待复核。
- 跟踪止损每小时更新，价格每 5 秒检查。实时净值用扣除平仓手续费的预计到账金额；1% 和 5% 是触发线，不是亏损上限。
- 会话到期前 72 小时通知，24 小时前退出。`login` 可以原子替换会话文件；机器人下次请求会读取新会话。
- `Ctrl-C`/SIGTERM 会请求平仓，并在退出尚未确认时继续等待。Docker 的 120 秒宽限期后可能强制结束，持仓不会因此自动消失。重启后优先恢复退出。
- 若代码/资格报告变化时仍有已知持仓，启动进入仅退出模式，不会因缺少上线证据拒绝恢复仓位。
- 本机看门狗只能发现交易进程失联。整台服务器断电/网络中断须从另一台机器监控，参见部署文档。

部署步骤见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)，接口和风控细节见 [docs/DESIGN.md](docs/DESIGN.md)。

## 图形化监控

只读测试观测台已提供净值曲线、14 天进度、回测对照、验收门槛与告警。通过 SSH 隧道访问，不开放公网端口。运行 `sh scripts/open-monitor.sh`，然后打开 http://127.0.0.1:18787 。详细说明见 [监控使用文档](docs/MONITORING.md)。

## V2 十仓研究

独立 V2 包已实现 BTC/ETH 多仓、突破/回调/均值回归、三档组合预算和滚动研究。最终 15 个候选均未通过门槛，因此 V2 未启动，V1 继续影子运行。研究设计、复现命令及结果见 [V2 研究说明](docs/V2_RESEARCH.md)；面板的“V2 多仓研究”可查看完整对照。
