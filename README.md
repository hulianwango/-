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
- `save_annotation_draft(paper_id, annotation_json)`
- `update_annotation_draft(draft_id, annotation_json)`

限制：

- `search_papers` 拒绝空 query，最多返回 10 条。
- `read_text_chunks` 每次最多 5 个 chunks。
- `read_page_text` 每次只读 1 页。
- 每次返回文本总长度最多 6000 字符。
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

## Remote MCP 部署建议

- 使用 HTTPS 反向代理，只转发 `/mcp` 路径。
- 不要把 `/local` 路径暴露到公网。
- 最稳妥的部署方式是让 remote MCP 只访问同步后的 SQLite 索引数据库，不挂载原始 PDF 文件夹。
- 反向代理和应用日志都应避免记录请求体，尤其是 chunk 原文和草稿正文。

