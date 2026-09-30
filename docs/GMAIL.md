# Gmail 告警配置

2026-09-30 已核对 Google 官方文档，并从部署服务器验证 465/SSL 和 587/STARTTLS 都可以完成连接。当前配置采用：

- SMTP：`smtp.gmail.com`，465，SSL/TLS，验证服务器证书。
- 发件邮箱与认证用户名：`hsjavt@gmail.com`。
- 告警收件人：`hsjavt@gmail.com`。
- 凭据文件：服务器 `/opt/fishfishfish/secrets/smtp-password`，权限 600。

Google 优先推荐“使用 Google 账号登录”／OAuth。当前机器人使用标准 SMTP，未实现 Google OAuth，因此采用官方允许的应用专用密码方式，需要账号已开启两步验证。这里不使用普通 Google 登录密码，也不启用已淘汰的“不够安全的应用”设置。

由账号所有者自行完成：

1. 确认 Google 账号已开启两步验证。
2. 打开 <https://myaccount.google.com/apppasswords>，创建专用于 `Fishbot alerts` 的应用专用密码。
3. 在本机运行 `scripts/setup-gmail-server.sh SERVER SSH_USER PRIVATE_KEY_PATH`，隐藏输入生成的 16 位应用专用密码；显示分组用的空格会自动去除。
4. 命令保存凭据后，向配置的收件邮箱发送一封测试邮件。成功提交给 SMTP 不保证收件箱一定可见，请同时检查垃圾邮件目录。

密码不会出现在命令参数、聊天记录或日志中。配置脚本不会保存普通账号密码。更改 Google 账号密码会撤销应用专用密码，届时需要重新生成并运行配置命令。如果账号没有应用专用密码选项，应检查 Google 官方所列账号限制，不要关闭保护措施来绕过。

官方来源：

- [Google：使用应用专用密码登录](https://support.google.com/accounts/answer/185833?hl=zh-Hans)
- [Google：Gmail IMAP、POP 和 SMTP 协议及 TLS 端口](https://developers.google.com/workspace/gmail/imap/imap-smtp)
