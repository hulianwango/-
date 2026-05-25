# Private Literature MCP Site

本项目是一个本机私人文献网站 + MCP 安全检索/草稿写入服务的第一版骨架。

## 安全边界

- 真实论文目录只从 `config.local.yaml` 或 `.env` 读取，不写死在代码中。
- `config.local.yaml`、`.env`、`papers/`、`data/`、`outputs/`、`logs/`、`*.pdf`、`*.db` 已加入 `.gitignore`。
- 本地网页默认监听 `127.0.0.1`，PDF 阅读接口只允许本机访问。
- MCP 接口只提供检索和 AI 草稿写入，不提供 PDF 下载、PDF 打开、全文导出、后台管理、删除、上传、正式批注审批。
- MCP 不返回 PDF 路径、数据库路径、本机绝对路径、PDF 下载链接或私人 notes。
- MCP 文献检索类响应只返回安全字段：`paper_id`、`title`、`authors`、`year`、`journal`、`doi`、`page_number`、`chunk_id`、`snippet`、`score`。
- MCP 草稿写入类只返回草稿处理所需的 `paper_id`、`draft_id`、`status`，不返回路径或原文。
- MCP 请求写入 `logs/mcp_access.jsonl`，日志只记录工具名、状态码、查询长度和 chunk 数，不记录完整原文、路径或数据库位置。

## 组成

- 后端：FastAPI
- 前端：React + PDF.js
- 数据库：SQLite + FTS5
- PDF 文本提取：PyMuPDF
- 文件夹监听：watchdog

## 本地配置

`config.local.yaml` 已经创建，并被 `.gitignore` 忽略。可用 `.env` 覆盖：

```env
LIT_PAPERS_DIR=papers
LIT_DATABASE_PATH=data/library.db
LIT_LOGS_DIR=logs
MCP_BEARER_TOKEN=change-me-before-exposing
```

上线或暴露 MCP 前，请把 `MCP_BEARER_TOKEN` 改成强随机 token。

## 启动

```powershell
python -m pip install -r requirements.txt
python scripts/scan_papers.py
python run_server.py
```

打开本地网站：

[http://127.0.0.1:8000](http://127.0.0.1:8000)

## MCP 工具

允许：

- `search_papers(query, limit)`
- `get_paper_metadata(paper_id)`
- `read_text_chunks(paper_id, chunk_ids)`
- `read_page_text(paper_id, page_number)`
- `list_paper_categories(include_empty)`
- `move_paper_file(paper_id, category_path, create_missing_category)`
- `save_annotation_draft(paper_id, annotation_json)`
- `update_annotation_draft(draft_id, annotation_json)`

限制：

- `search_papers` 拒绝空 query，最多返回 10 条。
- `read_text_chunks` 每次最多 5 个 chunks。
- `read_page_text` 每次只读 1 页。
- 每次返回文本总长度最多 6000 字符。
- `move_paper_file` 每次只移动 1 篇已入库 PDF，只接受 `D:/OneDrive/桌面/论文文件集合` 内的相对目标目录，不接受绝对路径，不删除文件，不移动到该最大文件夹以外；默认允许在该最大文件夹内创建新的小分类文件夹。
- `save_annotation_draft` 只写 `paper_ai_drafts`。
- `update_annotation_draft` 只修改 pending 状态的 `paper_ai_drafts`。
- 正式 `paper_annotations` 只能由本地网页端接受草稿后写入。

JSON-RPC 入口：

```http
POST /mcp
Authorization: Bearer <token>
Content-Type: application/json
```

REST 调试入口：

```http
GET /mcp/tools
POST /mcp/tools/search_papers
POST /mcp/tools/read_text_chunks
```

## 网页端草稿流程

1. MCP 检索索引后的文本块。
2. ChatGPT 根据文本块生成结构化 `annotation_json`。
3. MCP 调用 `save_annotation_draft()` 保存到 `paper_ai_drafts`。
4. 网页端展示待审草稿。
5. 用户编辑后点击接受，后端复制到 `paper_annotations`，并同步机制、课题相关字段和推荐标签。
6. 用户拒绝时只改变草稿状态，不写正式表。

## 长期运行部署

目标架构：

- 固定公网地址：`https://literature.example.com`
- ChatGPT Connector URL：`https://literature.example.com/mcp`
- 本地后端：`http://127.0.0.1:8000`
- Cloudflare Named Tunnel：把 `literature.example.com` 转发到本机后端。
- Windows 自动启动：`cloudflared` 使用 Windows 服务，后端使用计划任务或服务。

本服务实现 OAuth 2.1 authorization code + PKCE，ChatGPT Connector 可通过以下地址发现认证信息：

- `GET /.well-known/oauth-protected-resource`
- `GET /.well-known/oauth-authorization-server`
- `GET /.well-known/openid-configuration`

`/mcp` 和 `/mcp/tools/*` 未认证时会返回 `WWW-Authenticate`，其中包含 `resource_metadata`，ChatGPT 会用它继续发现 OAuth metadata。

### 1. 固定 config.local.yaml

`config.local.yaml` 被 `.gitignore` 忽略，应保留在本机。把 `oauth.public_base_url` 固定成正式域名，后续不要再写临时地址：

```yaml
app:
  host: "127.0.0.1"
  port: 8000

paths:
  papers_dir: "D:/path/to/papers"
  database: "data/library.db"
  logs_dir: "logs"

mcp:
  bearer_token: "replace-with-strong-local-test-token"
  require_auth: true
  rate_limit_per_minute: 60
  max_search_limit: 10
  max_chunks_per_request: 5
  max_response_chars: 6000

index:
  chunk_size: 1800
  chunk_overlap: 250

oauth:
  enabled: true
  public_base_url: "https://literature.example.com"
  username: "admin"
  password_hash: "$2b$12$replace-with-bcrypt-hash"
  token_expires_seconds: 43200
  code_expires_seconds: 600
```

也可以用环境变量覆盖：`OAUTH_PUBLIC_BASE_URL`、`OAUTH_USERNAME`、`OAUTH_PASSWORD_HASH`、`OAUTH_TOKEN_EXPIRES_SECONDS`、`OAUTH_CODE_EXPIRES_SECONDS`。

生成 OAuth 登录密码哈希：

```powershell
python -m pip install -r requirements.txt
python scripts/make_password_hash.py
```

### 2. Cloudflare Named Tunnel

在 Cloudflare 中确保 `literature.example.com` 所属域名已经接入同一个账号，然后用管理员 PowerShell 执行：

```powershell
winget install --id Cloudflare.cloudflared --exact
cloudflared tunnel login
cloudflared tunnel create literature-mcp
cloudflared tunnel route dns literature-mcp literature.example.com
```

把隧道配置写入当前用户的 cloudflared 配置目录：

```powershell
notepad "$env:USERPROFILE\.cloudflared\config.yml"
```

配置内容参考 [deploy/cloudflared/config.yml.example](deploy/cloudflared/config.yml.example)：

```yaml
tunnel: <your-tunnel-uuid>
credentials-file: 'C:\Users\<your-user>\.cloudflared\<your-tunnel-uuid>.json'

ingress:
  - hostname: literature.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

验证配置并前台试运行：

```powershell
cloudflared tunnel --config "$env:USERPROFILE\.cloudflared\config.yml" ingress validate
cloudflared tunnel --config "$env:USERPROFILE\.cloudflared\config.yml" run literature-mcp
```

确认 `https://literature.example.com/health` 返回 `{"status":"ok"}` 后，停止前台进程，再安装 Windows 服务：

```powershell
cloudflared service install
Start-Service cloudflared
Get-Service cloudflared
```

后续修改 `config.yml` 后重启 tunnel 服务：

```powershell
Restart-Service cloudflared
```

### 3. 后端开机自启

推荐先用 Windows 计划任务启动后端，优点是不需要额外服务包装器，也更适合读取当前用户 OneDrive 里的论文目录。

在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_backend_startup_task.ps1
```

如果要指定虚拟环境或固定 Python：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_backend_startup_task.ps1 -PythonPath "E:\python\python.exe"
```

脚本默认创建登录后自动运行的任务 `PrivateLiteratureMcpBackend`，并立即启动一次。检查状态：

```powershell
Get-ScheduledTask -TaskName PrivateLiteratureMcpBackend
Get-ScheduledTaskInfo -TaskName PrivateLiteratureMcpBackend
```

如需随 Windows 启动触发，可用管理员 PowerShell 增加 `-AtStartup`，但如果论文目录在当前用户 OneDrive 下，仍建议使用登录触发：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_backend_startup_task.ps1 -AtStartup
```

需要真正的 Windows 服务时，可使用 NSSM 或 WinSW 这类服务包装器，把 `python.exe` 作为程序、`run_server.py` 作为参数、项目根目录作为工作目录。服务运行账号必须能读取 `config.local.yaml`、`data/`、`logs/` 和论文目录。

### 4. ChatGPT Connector 固定设置

在 ChatGPT 创建或更新 Connector：

1. Connector URL 填写 `https://literature.example.com/mcp`。
2. 认证方式选择 OAuth。
3. ChatGPT 会读取 protected resource metadata、authorization server metadata，并调用 `/oauth/register` 动态注册 public client。
4. 授权页输入 `oauth.username` 对应的用户名和密码。
5. ChatGPT 用 authorization code + PKCE 换取 access token 后，即可调用 `/mcp`。

完成后验证：

```powershell
python scripts/test_oauth_metadata.py --base-url https://literature.example.com
python scripts/test_mcp_json_rpc_compat.py --base-url https://literature.example.com --allow-empty-results
```

### 5. 安全注意事项

- 不再使用 `trycloudflare` 临时地址作为正式 Connector 地址。
- 固定域名上线后，`oauth.public_base_url`、Cloudflare hostname、Connector URL 必须一致。
- `mcp.bearer_token` 只用于本地 PowerShell 调试，仍应使用强随机值。
- OAuth 密码只保存 bcrypt 哈希，不保存明文。
- 不要把 `/local` 当成公开接口使用；Cloudflare Tunnel 虽然转发整站，但 MCP 安全边界只承诺 `/mcp` 和 OAuth 发现/授权路径。
- Cloudflare 和应用日志都应避免记录请求体，尤其是 chunk 原文、草稿正文、token、PDF 路径和数据库路径。
- MCP 不暴露 PDF、PDF 路径、数据库路径、本机路径、private notes 或完整全文。
- MCP 不提供 `download_pdf`、`get_pdf_file`、`get_full_text`、`read_all_pages`、`list_all_chunks`、`export_database`。
- `save_annotation_draft` 只写 `paper_ai_drafts` 草稿表，不批准草稿。
- `approve_annotation` 不作为 MCP 工具暴露。

Cloudflare 参考文档：

- [Run cloudflared as a Windows service](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/local-management/as-a-service/windows/)
- [Tunnel ingress rules](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/configuration-file/)
- [Route tunnel traffic with DNS records](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/routing-to-tunnel/dns/)
