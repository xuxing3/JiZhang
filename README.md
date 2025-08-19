# JiZhang（记账）

一个简洁的记账项目：前端 + Node.js API 提供网页端录入与统计；可选的 Telegram 机器人使用通义千问（DashScope/Qwen）识别支付截图并入库到 MongoDB。

项目组成
- `server.js`：Express + Mongoose 的后端 API，同时服务 `public/` 静态前端
- `public/`：静态前端（Vue + Chart.js）
- `tele_qwen_bot_monthly.py`：Telegram 机器人（识别截图、入库、生成月报）
- `.env` / `.env.example`：环境变量配置
- `requirements.txt`：机器人所需 Python 依赖

运行要求
- Node.js 18+
- MongoDB 数据库（Atlas 或自建）
- Python 3.11+（仅当启用 Telegram 机器人时）

快速开始 — Web/API
- 安装依赖：`npm install`
- 配置环境：复制 `.env.example` 为 `.env`，至少填写 `MONGODB_URI`
- 启动：`npm start`（默认 `http://localhost:3000`）
- 健康检查：`curl http://localhost:3000/api/health`

快速开始 — Telegram 机器人（可选）
- 使用 BotFather 创建机器人并获取 `TELEGRAM_TOKEN`
- 在 DashScope 控制台获取 `DASHSCOPE_API_KEY`
- `.env` 中确保包含：`MONGODB_URI`、`TELEGRAM_TOKEN`、`DASHSCOPE_API_KEY`，以及 `ALLOWED_USER_IDS` 或 `FORCE_CHAT_ID`（限制可用用户）
- 安装依赖：`pip install -r requirements.txt`
- 运行：`python tele_qwen_bot_monthly.py`

使用 Docker 同时启动 Web 与 Bot
- 准备 `.env`：由 `.env.example` 复制并填写；注意 Docker 的环境文件必须是 `KEY=VALUE`，等号两侧不能有空格
- 启动：`docker compose up --build -d`
- 访问：`http://localhost:3000`
- 停止：`docker compose down`

镜像与服务
- `web`：Node.js API + 静态前端（暴露端口 `3000`）
- `bot`：Python Telegram 机器人（需要 `MONGODB_URI`、`TELEGRAM_TOKEN`、`DASHSCOPE_API_KEY`、`ALLOWED_USER_IDS`/`FORCE_CHAT_ID`）

环境变量（.env）
- 必填
  - `MONGODB_URI`：MongoDB 连接串
- 可选（Web/API）
  - `PORT`：默认 `3000`
  - `DB_NAME`：数据库名
  - `COLLECTION`：集合名，默认 `expenses`
  - `API_KEY`：若设置，所有 API 请求需带 `x-api-key: <值>` 头
  - `FORCE_CHAT_ID`：将 API 数据限制到某个 chat/user id
  - `AI_PROVIDER`：文本解析提供方，可选 `openai` / `dashscope`（留空则自动根据可用的 Key 选择或使用规则解析）
  - `OPENAI_API_KEY`、`OPENAI_MODEL`（默认 `gpt-4o-mini`）
  - `DASHSCOPE_API_KEY`、`QWEN_MODEL`（默认 `qwen-turbo`）
- 可选（机器人）
  - `TELEGRAM_TOKEN`：Telegram 机器人 Token
  - `DASHSCOPE_API_KEY`：通义千问 DashScope Key
  - `ALLOWED_USER_IDS`：允许的 Telegram 用户 ID（逗号或空格分隔）；或使用 `FORCE_CHAT_ID` 指定单个 ID

命令示例
- 健康检查：`curl http://localhost:3000/api/health`
- 指定端口启动：`PORT=4000 npm start`
- 启用 API Key 时的请求：`curl -H "x-api-key: <key>" http://localhost:3000/api/health`

文本入账（AI 解析）
- 接口：`POST /api/expenses/parse`
- 说明：传入一段自然语言记账文本（中文/英文均可），服务端会优先调用配置的 AI（OpenAI/Qwen），失败或未配置时会回退到规则解析；解析成功后自动入库，并返回创建的记录和解析结果。
- 请求体（JSON）：
  - `text`：必填，自然语言文本，如：`"午饭 麦当劳 23.5元 12:10"`
  - `tz`：可选，默认 `Asia/Shanghai`
  - `chat_id`：可选；若设置了 `FORCE_CHAT_ID` 则忽略此值
- 示例：
  - `curl -X POST http://localhost:3000/api/expenses/parse -H 'Content-Type: application/json' -d '{"text":"买菜 45.5 超市 18:30"}'`
  - 启用 API Key：`curl -X POST http://localhost:3000/api/expenses/parse -H 'x-api-key: <key>' -H 'Content-Type: application/json' -d '{"text":"午饭 星巴克 28 12:05"}'`
 - 返回：`{ created: <入库文档>, parsed: { amount, category, payee, time, note } }`

提示
- Web 与 Bot 是两个进程，可分别或同时运行；它们共享同一个 `MONGODB_URI`
- 若报表中文乱码，Docker 的 `bot` 镜像已内置 Noto CJK 字体；本地运行可自行安装中文字体
- 请勿提交 `.env` 或任何敏感信息；以 `.env.example` 作为参考

机器人命令用法
- 图片入账：向机器人发送支付截图，机器人会识别金额、商家、类型和时间（仅取当天时间），入库后返回一行可直接用于编辑的指令参数片段，例如：
  - `66f01c0b2f... amount=28.5 category=餐饮 payee="肯德基" time="2025-08-12 19:30"`
  - 可复制到 `/edit` 命令后快速修正。

- `/report [YYYY-MM]`：生成指定月份的消费分类柱状图、每日合计折线图，并附带 Excel 汇总（原始明细/按类别/按商家/按日期）。
  - 无参数时默认当月，例如：`/report 2025-08`

- `/list [YYYY-MM] [N]`：列出指定月份最近 N 条记录。
  - `YYYY-MM` 与 `N` 参数均可选；仅提供数字则视为 N。
  - 示例：`/list`、`/list 30`（本月 30 条）、`/list 2025-08 50`。

- `/edit <_id> key=value ...`：修改指定记录，支持字段：`amount`、`category`、`payee`、`time`。
  - 时间格式：`YYYY-MM-DD HH:MM`（本地时区 Asia/Shanghai）。
  - 含空格的值使用引号包裹（支持单引号或双引号）。
  - 示例：
    - `/edit 66f01c0b2f amount=25 category=餐饮 payee="麦当劳" time="2025-08-12 19:17"`

- `/delete <_id[,<_id2> ...]>`：删除一条或多条记录。
  - `_id` 可用逗号或空格分隔，例如：`/delete 66f01c0b2f,66f01c8a90` 或 `/delete 66f01c0b2f 66f01c8a90`。

- 权限控制：
  - 仅 `ALLOWED_USER_IDS`（或 `FORCE_CHAT_ID`）包含的用户可用；未授权用户会收到提示。
