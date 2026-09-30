# 服务器部署与运维

建议 Linux x86_64/arm64，2 核、2 GB RAM，至少 2 GB 可用磁盘。需 Docker Engine + Compose plugin，以及出站 HTTPS 和 SMTP；应用无需开放公网端口。已在用户服务器的 Docker 26.1.3 / Compose 2.27.0 中构建镜像并完成测试。

该服务器不能直连 Docker Hub，项目 `.env` 已设置 `PYTHON_IMAGE` 为服务器现用加速源的官方 Python 3.12 amd64 镜像，固定摘要 `sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b`。该摘要已从 Docker 官方标签 API 核对；没有修改 Docker 全局仓库配置。Dockerfile 的默认来源仍为固定摘要的官方镜像。

## 提供给部署者的信息

- 公网 IP、SSH 用户名、SSH 端口。
- 本机 SSH 私钥文件的绝对路径；不在聊天、仓库或日志里粘贴私钥。
- SMTP 主机、端口、发件邮箱、用户名、接收告警邮箱；授权码独立写入 secret 文件。

部署前核对 SSH 主机指纹。不要禁用 StrictHostKeyChecking 或覆盖已有 known_hosts 项。完成只读系统检查后再安装缺失依赖。

## 安装与登录

将项目同步到 `/opt/fishfishfish`（路径可按服务器权限调整），不要同步 `.git`、本机会话或不相关文件。回测产物、数据清单和当前代码对应的测试证据按需同步。然后：

```sh
cd /opt/fishfishfish
mkdir -p data secrets
chmod 700 secrets
sudo chown -R 10001:10001 data secrets
docker compose build
docker compose run --rm login
docker compose run --rm trader doctor
docker compose run --rm trader drills
```

登录命令由用户在终端输入账号和密码。会话仅存该服务器；无需复制当前浏览器的 Cookie。独立的 `login` 服务明确以可写方式挂载 secrets，常驻交易和看门狗均以只读方式挂载。不要用 `compose run -v ...:rw trader login` 临时覆盖：部署服务器的 Compose 2.27.0 仍保留了原挂载的只读属性。`login` 位于 tools profile，仅显式执行时启动，不随 `up -d` 启动。

编辑 `config.toml` 的邮件设置，以权限 600 创建 `secrets/smtp-password`，所有者 UID 10001。不要将授权码放在 shell 命令参数或历史中。可用受限文本编辑器或秘密管理器写入。

```sh
docker compose run --rm trader email-test
docker compose up -d
docker compose ps
docker compose logs --tail=100 trader watchdog
docker compose run --rm trader status
```

这会启动影子模式。回测未通过也可以研究性影子运行，但不能执行 canary/live。没有有效会话时预检失败退出，Docker 会重试；先完成登录后再常驻运行。

## 升级和切换模式

计划停止先 SIGTERM 并等到状态显示仓位和 pending 均为空：

```sh
docker compose stop trader
docker compose run --rm trader status
```

退出失败时不要认为 stop 已清仓。宽限期只有 120 秒；检查网站持仓和告警，再恢复进程处理。

满足所有上线门槛后，先停止影子，60 秒内执行 `qualify-shadow`；然后将配置文件的 `mode`、`database` 改为 canary 对应值，再运行 `docker compose run --rm trader canary`。canary 成功对账后改为 live 对应值，执行 `gates` 和 `up -d --force-recreate`。保持三个 SQLite 文件，不能覆盖或复用它们。

影子证据/测试邮件 24 小时内有效。代码更改使证据失效，需重新评估。已有持仓恢复启动仍允许仅退出，以免资格检查把仓位困住。

## 监控与备份

`watchdog` 独立于 trader 运行，使用独立 SQLite 连接，超过 60 秒未收到心跳即发邮件。它与 trader 同机，因此不能检测整机断电或完全失网。

整机可用性应由第二台机器或已有基础设施监控器检查 SSH 连通性、Docker 健康状态及 SQLite 心跳，并使用第二台机器自己的邮件服务发送告警。不要把 SMTP 或会话文件暴露在公开 HTTP 服务中。外部监控尚未配置时，不能声称有整机故障告警。

SQLite 启用 WAL 和 FULL 同步。运行中备份用 SQLite backup API，不能只复制 `.sqlite3` 主文件而遗漏 WAL：

```sh
docker compose exec trader python -c "import sqlite3; a=sqlite3.connect('data/shadow.sqlite3'); b=sqlite3.connect('data/shadow-backup.sqlite3'); a.backup(b); b.close(); a.close()"
```

对应模式替换数据库文件名。会话文件单独加密备份或重新登录生成。恢复数据库后先只读核对账户，再让进程恢复；不允许丢失数据库后直接对现有仓位初始化新机器人。

## 验收清单

- `doctor` 能读取真实站点快照，交易规则及行情来源吻合。
- 告警测试邮件到达；进程心跳异常和恢复通知均验证。
- 默认影子模式，50 条策略资金；账户外部流水不改变本地策略资金。
- 运行时用户 UID 10001，可写 data，可读但不可写 secrets，无新增公网端口。
- `drills` 通过且代码哈希一致，回测报告完整，影子天数由持续心跳测量。
- 未通过回测/14 天影子时，canary 和 live 被拒绝；没有任何绕过开关。
