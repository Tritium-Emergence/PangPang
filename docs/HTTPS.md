# 可信 HTTPS 入口

PangPang 不安装、不检测、也不配置任何 VPN。远程访问只有一种合同：操作者已经有一个浏览器和 Connector 的 Python 都信任的 `https://` 名称，并且自己把请求转到仍只监听回环地址的协调器。

## 步骤

1. 协调器所在机器已安装 Python 3.11+ 的 PangPang。数据目录用绝对路径，或使用正式安装的默认目录（见 INSTALL.md）。不要把示例里的域名换成自己的私有地址后提交回仓库。
2. 准备一个已经可达的 HTTPS URL。证书由操作者的代理或域名签发。公信证书对 Connector 最省事。私有 CA 要装进每台机器上 Python 实际使用的信任库；macOS 上的 Python 通常不读钥匙串，需要 `SSL_CERT_FILE`。不要关闭证书校验。
3. 协调器仍然只绑定 `127.0.0.1`、`localhost` 或 `::1`。不要为了组网改成 `0.0.0.0`。
4. 用该外部地址启动，并在需要同账号执行时显式声明部署等级。设置了 `--origin` 之后，程序不会再推断 POSIX L0：

```bash
python -m pangpang.cli cloud --data /var/lib/pangpang --origin https://bot.example.com
```

5. `--origin` 只有一个。本机浏览器和同机 Connector 也使用这个 HTTPS 名称，不要再改走 `http://127.0.0.1`。服务器自己要能解析这个名字并信任证书。Host 与 Origin 必须和该名称逐字一致；`X-Forwarded-Host` 不会被采信。
6. 另一台机器的 Connector 使用同一个 HTTPS 地址和网页上的配对码：

```bash
python -m pangpang.cli connector --cloud https://bot.example.com --workspace /work --data /var/lib/pangpang-connector
```

证书不被 Python 信任时，错误是 `connector_tls_untrusted`。主机名不一致时是 `connector_tls_hostname_mismatch`。这两种情况都不会改为明文，也不会跳过 CA。

7. 手机浏览器打开同一个 HTTPS 名称并登录。普通 `http://` 私网地址不能当作远程 Web 的安全上下文。
8. 派一个真实任务，确认回执写回，而不是只看到进程还活着。
9. 失败时按层看：DNS、浏览器证书、Python 证书、Host/Origin 不一致、代理缓冲了 SSE、连接断开。网络通不等于执行器就绪，就绪不等于任务完成。

反向代理必须把 SSE 立刻刷出。RUNBOOK 里的 Caddy 片段使用 `flush_interval -1`。代理后面的登录限流会合成一个桶，因为程序不信任 `X-Forwarded-For`。

## 网络本身不归 PangPang

下面的链接只是操作者可能已经在用的做法，不是兼容矩阵，也不是安装步骤：

- 家庭网络、没有自己的 VPS：Tailscale Serve，按其文档建立私网和 HTTPS。https://tailscale.com/kb/1242/tailscale-serve
- 已经自己管理网络：Headscale https://headscale.net/ 或 WireGuard https://www.wireguard.com/

VPN 身份不是 PangPang 的登录、配对或执行权限。流量是否离开操作者的网络，还取决于所配置的模型 API 和默认开启的联网搜索。
