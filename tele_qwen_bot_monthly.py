import os, re, json, base64, mimetypes, logging, requests, tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from openpyxl import Workbook  # 仅作为ExcelWriter引擎依赖

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler

# 报表依赖
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm

# Mongo
from pymongo import MongoClient, ASCENDING, ReturnDocument
from bson import ObjectId
from bson.errors import InvalidId

# ================= 基础配置 =================
load_dotenv()

# 从环境变量加载敏感配置（.env 中配置）
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
DASHSCOPE_API_KEY = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
# 优先使用通用的 MONGODB_URI 命名，兼容 MONGO_URI
MONGO_URI = (os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()

# 允许的 Telegram 用户 ID 列表：
# - 优先读取 ALLOWED_USER_IDS（逗号或空格分隔）
# - 兼容 FORCE_CHAT_ID（单个 ID）
_allowed_raw = (os.getenv("ALLOWED_USER_IDS") or os.getenv("FORCE_CHAT_ID") or "").strip()
_allowed = set()
if _allowed_raw:
    for part in re.split(r"[\s,]+", _allowed_raw):
        if not part:
            continue
        try:
            _allowed.add(int(part))
        except Exception:
            pass
ALLOWED_USER_IDS = _allowed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tele-qwen-monthly")

# 仅作为临时输出目录；所有文件发完即删除
REPORT_DIR = Path("report_output")
REPORT_DIR.mkdir(exist_ok=True)

# ===== 关键词分类映射（可自行扩展）=====
CATEGORY_KEYWORDS = {
    "餐饮": ["麦当劳", "肯德基", "星巴克", "美团", "饿了么", "必胜客", "海底捞", "喜茶", "蜜雪", "奶茶", "餐饮", "外卖", "饭", "火锅"],
    "购物": ["淘宝", "天猫", "京东", "拼多多", "超市", "屈臣氏", "沃尔玛", "大润发", "山姆", "购物", "买菜"],
    "出行": ["滴滴", "高德", "地图", "打车", "共享单车", "哈啰", "青桔", "地铁", "公交", "出行", "高速", "停车"],
    "数码": ["Apple", "苹果", "小米", "华为", "京东电器", "数码", "配件"],
    "娱乐": ["腾讯视频", "爱奇艺", "优酷", "B站", "QQ音乐", "网易云", "游戏", "会员", "电影"],
    "通讯": ["话费", "流量", "通信", "联通", "移动", "电信", "宽带"],
    "医疗": ["医院", "药店", "医保", "体检", "诊所"],
    "转账": ["转账", "收款", "还款", "红包", "转付", "待确认收款"],
    "生活缴费": ["水费", "电费", "燃气", "物业", "停车费", "供暖", "生活缴费"],
}
CATEGORY_PRIORITY = ["转账", "生活缴费", "出行", "餐饮", "购物", "数码", "娱乐", "通讯", "医疗"]

# ================= Matplotlib 中文字体设置 =================
def setup_chinese_font():
    candidates = [
        "Noto Sans CJK SC",
        "Noto Sans CJK JP",
        "Source Han Sans SC",
        "Source Han Sans CN",
        "Microsoft YaHei",
        "SimHei",
        "WenQuanYi Zen Hei",
    ]
    available = {f.name for f in fm.fontManager.ttflist}
    chosen = None
    for name in candidates:
        if name in available:
            chosen = name
            break
    if chosen:
        matplotlib.rcParams["font.sans-serif"] = [chosen]
        logger.info(f"Matplotlib 使用中文字体: {chosen}")
    else:
        logger.warning("未找到中文字体，图像可能出现乱码。建议安装 Noto/思源黑体。")
    matplotlib.rcParams["axes.unicode_minus"] = False

# ================= 工具函数 =================
def encode_image_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def clean_amount(raw) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw)
    s = s.replace("￥", "").replace("元", "").replace("RMB", "").replace("CNY", "").strip()
    s = s.replace(",", "")
    m = re.search(r"(-?\d+(?:\.\d+)?)", s)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except:
        return 0.0

def pick_category(payee: str = "", desc: str = "", hint: str = "") -> str:
    text = f"{payee} {desc} {hint}".lower()
    hits = set()
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in text:
                hits.add(cat)
                break
    if not hits and hint:
        for cat in CATEGORY_KEYWORDS.keys():
            if cat in hint:
                hits.add(cat)
                break
    if not hits:
        if any(x in text for x in ["转账", "收款", "待确认"]):
            return "转账"
        return "其他"
    for cat in CATEGORY_PRIORITY:
        if cat in hits:
            return cat
    return list(hits)[0]

def time_today_shanghai(raw_time: str) -> str:
    raw_time = (raw_time or "").strip()
    m = re.search(r"(\d{1,2}:\d{2})", raw_time)
    tz = ZoneInfo("Asia/Shanghai")
    now_tz = datetime.now(tz)
    today = now_tz.strftime("%Y-%m-%d")
    hm = m.group(1) if m else now_tz.strftime("%H:%M")
    return f"{today} {hm}"

def local_to_utc_dt(time_local_str: str):
    """
    输入: 'YYYY-MM-DD HH:MM'（Asia/Shanghai）
    返回: (dt_local[带tz], dt_utc[带tz])
    """
    tz_sh = ZoneInfo("Asia/Shanghai")
    dt_local = datetime.strptime(time_local_str, "%Y-%m-%d %H:%M").replace(tzinfo=tz_sh)
    dt_utc = dt_local.astimezone(ZoneInfo("UTC"))
    return dt_local, dt_utc

def fmt_doc_line(doc) -> str:
    return f"{str(doc.get('_id'))} | {doc.get('time_local','')} | {doc.get('amount',0):.2f} | {doc.get('category','')} | {doc.get('payee','')}"

def quote_for_kv(val: str) -> str:
    """
    生成 key="value" 中的安全 value：
    - 若包含双引号但不含单引号，用单引号包裹；
    - 若包含单引号但不含双引号，用双引号包裹；
    - 若两者都含，转义双引号，用双引号包裹；
    - 普通含空格值默认用双引号包裹。
    """
    s = str(val or "")
    if '"' in s and "'" not in s:
        return f"'{s}'"
    if "'" in s and '"' not in s:
        return f"\"{s}\""
    if "'" in s and '"' in s:
        s = s.replace('"', '\\"')
        return f"\"{s}\""
    if re.search(r"\s", s):
        return f"\"{s}\""
    return f"\"{s}\""  # 统一双引号，便于复制

# ================= 授权 =================
def ensure_allowed(update: Update) -> bool:
    user = update.effective_user
    if not user or user.id not in ALLOWED_USER_IDS:
        # 尽量避免泄露限制规则
        try:
            if update.message:
                # 不给出太多信息
                return_msg = "⛔️ 未授权用户。"
                # 群里避免刷屏，可仅私聊提示；这里统一简单提示
                update.message.reply_text(return_msg)
        except Exception:
            pass
        return False
    return True

# ================= Mongo 层 =================
def get_mongo():
    client = MongoClient(MONGO_URI, tz_aware=True)
    db = client.get_database("tele_finance")
    col = db.get_collection("expenses")
    col.create_index([("chat_id", ASCENDING), ("ym", ASCENDING), ("ts_utc", ASCENDING)])
    return col

def insert_expense(col, amount: float, category: str, payee: str, time_local_str: str, chat_id: int):
    dt_local, dt_utc = local_to_utc_dt(time_local_str)
    doc = {
        "chat_id": chat_id,
        "amount": float(amount),
        "category": category,
        "payee": payee,
        "time_local": time_local_str,   # 显示专用
        "ym": time_local_str[:7],       # 月份分区
        "ts_utc": dt_utc,               # UTC 存库
        "tz": "Asia/Shanghai",
        "created_at_utc": datetime.utcnow()
    }
    res = col.insert_one(doc)
    doc["_id"] = res.inserted_id   # 确保能拿到 _id
    return doc

def load_month_df(col, month_arg: str, chat_id: int) -> pd.DataFrame:
    # 兼容历史没有 chat_id 的旧数据：同时取本 chat 和 chat_id 缺失的记录
    cur = col.find(
        {"ym": month_arg, "$or": [{"chat_id": chat_id}, {"chat_id": {"$exists": False}}]},
        projection={"_id": 0, "amount": 1, "category": 1, "payee": 1, "time_local": 1}
    )
    rows = list(cur)
    if not rows:
        return pd.DataFrame(columns=["Time", "Amount", "Category", "Payee"])
    df = pd.DataFrame(rows).rename(columns={
        "time_local": "Time",
        "amount": "Amount",
        "category": "Category",
        "payee": "Payee"
    })
    df["Time"] = pd.to_datetime(df["Time"], format="%Y-%m-%d %H:%M", errors="coerce")
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").fillna(0)
    df = df.dropna(subset=["Time"]).copy()
    return df

# ================= 通义 API =================
def extract_json_from_qwen(result_text):
    if isinstance(result_text, list):
        try:
            result_text = result_text[0]["text"]
        except Exception:
            result_text = str(result_text)

    m = re.search(r"```json\s*(\{.*?\})\s*```", result_text, re.DOTALL)
    if m:
        return json.loads(m.group(1))

    m = re.search(r"(\{.*?\})", result_text, re.DOTALL)
    if m:
        return json.loads(m.group(1))

    try:
        return json.loads(result_text)
    except Exception:
        pass

    raise ValueError("未能从通义响应中提取 JSON")

async def call_qwen(image_path: str):
    mime_type, _ = mimetypes.guess_type(image_path)
    data_url = f"data:{mime_type};base64,{encode_image_base64(image_path)}"

    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "qwen-vl-plus",
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"image": data_url},
                        {"text": (
                            "请从这张付款截图中提取如下字段，直接返回 JSON（不要加解释、不要加 Markdown）："
                            "{ \"amount\": \"支付金额(数字或字符串)\", "
                            "\"payee\": \"商家名称\", "
                            "\"category\": \"消费类型(如:餐饮/购物/出行/转账等)\", "
                            "\"time\": \"交易时间(如: 19:17；若包含日期请忽略日期，仅返回时间)\" }"
                        )}
                    ]
                }
            ]
        },
        "parameters": {"use_raw_prompt": True}
    }

    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    result = r.json()
    if "output" not in result:
        raise RuntimeError(result.get("code", "Unknown"), result.get("message", "No message"))
    return result["output"]["choices"][0]["message"]["content"]

# ================= Telegram 处理 =================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_allowed(update):
        return

    # 下载到临时文件，处理完立即删除
    local_tmp = tempfile.NamedTemporaryFile(prefix="screenshot_", suffix=".jpg", delete=False)
    local_path = local_tmp.name
    local_tmp.close()

    try:
        photo = update.message.photo[-1]
        tf = await context.bot.get_file(photo.file_id)
        await tf.download_to_drive(local_path)

        qwen_resp = await call_qwen(local_path)
        data = extract_json_from_qwen(qwen_resp)

        raw_amount = data.get("amount")
        payee = (data.get("payee") or "").strip()
        hint_cat = (data.get("category") or "").strip()
        raw_time = (data.get("time") or "").strip()

        amount = clean_amount(raw_amount)
        category = pick_category(payee=payee, desc="", hint=hint_cat)

        # 只取时间，日期强制用 Asia/Shanghai 的“今天”
        time_str = time_today_shanghai(raw_time)

        # 入库（按 chat 维度）
        chat_id = update.effective_chat.id
        col = get_mongo()
        doc = insert_expense(col, amount, category, payee, time_str, chat_id=chat_id)

        # ===== 返回一行可直接复制到 /edit 的消息 =====
        amt_str = f"{amount:g}"  # 去掉多余0
        line = f'{str(doc["_id"])} amount={amt_str} category={category} payee={quote_for_kv(payee)} time={quote_for_kv(time_str)}'
        await update.message.reply_text(line)

    except Exception as e:
        logger.exception("处理失败")
        await update.message.reply_text(f"❌ 处理失败：{e}")

    finally:
        # 自动删除截图
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception:
            pass

# ===== 文本入账（AI 优先）=====
# 通过通义千问文本模型从自由文本中抽取结构化字段，仅返回 JSON
def call_qwen_text(text: str) -> dict:
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    prompt = (
        "从用户的记账文本中抽取字段，只返回 JSON（不要加入任何解释或 Markdown）："
        '{ "amount": 数字, "category": "字符串", "payee": "字符串", '
        '"time": "字符串或空", "note": "原文或摘要" }。'
        "如果只有时间（如 19:17）仅返回 HH:MM；若无时间则返回空字符串。文本："
        + str(text)
    )
    # DashScope text-generation 接口要求 input.prompt 为字符串
    payload = {
        "model": "qwen-turbo",
        "input": {"prompt": prompt},
        "parameters": {"use_raw_prompt": True},
    }
    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        # 打印一小段响应内容，便于定位 400 错误原因
        try:
            detail = r.text[:300]
        except Exception:
            detail = str(e)
        raise RuntimeError(f"DashScope text gen error: {r.status_code} {detail}")
    result = r.json()
    if "output" not in result and "output_text" not in result:
        raise RuntimeError(result.get("code", "Unknown"), result.get("message", "No message"))
    # 兼容不同返回格式
    content = None
    out = result.get("output") or {}
    if isinstance(out, dict) and "text" in out:
        content = out.get("text")
    if content is None:
        # 兼容 choices/message 结构
        try:
            content = out.get("choices", [])[0]["message"]["content"]
        except Exception:
            content = None
    if content is None:
        content = result.get("output_text")
    if content is None:
        raise RuntimeError("DashScope response has no text content")
    return extract_json_from_qwen(content)

# 文本入账：解析自由文本中的 金额/时间/商家 并入库（启发式作为兜底）
def parse_text_message(raw: str):
    s = (raw or "").strip()
    # 金额（支持 23.5、23,50、23 元、￥23 等）
    m_amt = re.search(r"(-?\d+(?:[.,]\d+)?)(?:\s*(?:元|块|rmb|cny|￥))?", s, re.I)
    amount = float(m_amt.group(1).replace(",", "")) if m_amt else None

    # 时间：优先 YYYY-MM-DD HH:MM，其次 YYYY-MM-DD + HH:MM，再次 HH:MM
    m_full = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})[ T]?(\d{1,2}:\d{2})", s)
    m_ymd = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", s)
    m_hm = re.search(r"(\d{1,2}:\d{2})", s)
    time_local = None
    if m_full:
        ymd = m_full.group(1).replace("/", "-")
        hm = m_full.group(2)
        time_local = f"{ymd} {hm}"
    elif m_ymd and m_hm:
        ymd = m_ymd.group(1).replace("/", "-")
        time_local = f"{ymd} {m_hm.group(1)}"
    elif m_hm:
        time_local = time_today_shanghai(m_hm.group(1))
    else:
        time_local = time_today_shanghai("")

    # 商家：尝试 在/于/去/给/向 之后的词块；否则取首个中文/字母串
    payee = ""
    m_payee = re.search(r"[在于去给向]([\u4e00-\u9fa5A-Za-z0-9_\-·]{2,20})", s)
    if m_payee:
        payee = m_payee.group(1)
    if not payee:
        m_cn = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,20})", s)
        payee = m_cn.group(1) if m_cn else ""

    # 分类：关键词匹配（与截图解析一致）
    category = pick_category(payee=payee, desc=s, hint="")
    note = s
    return {
        "amount": amount,
        "time_local": time_local,
        "category": category,
        "payee": payee,
        "note": note,
    }

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_allowed(update):
        return
    try:
        text = update.message.text or ""
        # 1) 优先用 AI 解析
        data = None
        try:
            data = call_qwen_text(text)
        except Exception as e:
            logger.warning(f"AI 文本解析失败，回退启发式：{e}")
        # 2) 兜底启发式
        if not isinstance(data, dict):
            data = parse_text_message(text)

        # 归一化与修正
        amt = clean_amount(data.get("amount"))
        if not amt:
            await update.message.reply_text("❌ 未识别到金额，请包含如 23 或 23.5 元/￥ 等数字。")
            return

        payee = (data.get("payee") or "").strip()
        hint_cat = (data.get("category") or "").strip()
        category = pick_category(payee=payee, desc=text, hint=hint_cat)
        raw_time = (data.get("time") or data.get("time_local") or "").strip()
        time_str = time_today_shanghai(raw_time)

        # 入库
        chat_id = update.effective_chat.id
        col = get_mongo()
        doc = insert_expense(col, amt, category, payee, time_str, chat_id=chat_id)

        # 回显可编辑片段
        amt_str = f"{amt:g}"
        line = f'{str(doc["_id"])} amount={amt_str} category={category} payee={quote_for_kv(payee)} time={quote_for_kv(time_str)}'
        await update.message.reply_text(line)
    except Exception as e:
        logger.exception("文本入账失败")
        await update.message.reply_text(f"❌ 文本入账失败：{e}")

# /report [YYYY-MM]
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_allowed(update):
        return

    cat_png = daily_png = sum_xlsx = None
    try:
        sh_tz = ZoneInfo("Asia/Shanghai")
        month_arg = " ".join(context.args) if context.args else None
        if not month_arg:
            month_arg = datetime.now(sh_tz).strftime("%Y-%m")

        col = get_mongo()
        df = load_month_df(col, month_arg, chat_id=update.effective_chat.id)
        if df.empty:
            await update.message.reply_text(f"⚠️ {month_arg} 无入账记录。")
            return

        setup_chinese_font()

        # ==== 生成图表（临时文件）====
        cat_png = REPORT_DIR / f"category_bar_{month_arg}.png"
        daily_png = REPORT_DIR / f"daily_line_{month_arg}.png"
        sum_xlsx = REPORT_DIR / f"summary_{month_arg}.xlsx"

        # 图1：分类柱状图
        cat = df.groupby("Category", dropna=False)["Amount"].sum().sort_values(ascending=False)
        plt.figure()
        ax = cat.plot(kind="bar")
        ax.set_title(f"按类别消费合计（{month_arg}）")
        ax.set_xlabel("类别")
        ax.set_ylabel("金额")
        plt.tight_layout()
        plt.savefig(cat_png, dpi=150)
        plt.close()

        # 图2：每日折线图
        daily = df.groupby(df["Time"].dt.date)["Amount"].sum().sort_index()
        plt.figure()
        ax = daily.plot(kind="line", marker="o")
        ax.set_title(f"每日消费折线图（{month_arg}）")
        ax.set_xlabel("日期")
        ax.set_ylabel("金额")
        plt.tight_layout()
        plt.savefig(daily_png, dpi=150)
        plt.close()

        # 汇总 Excel（临时文件，发完即删）：
        raw_out = df.sort_values("Time").copy()
        raw_out["Time"] = raw_out["Time"].dt.strftime("%Y-%m-%d %H:%M")
        with pd.ExcelWriter(sum_xlsx, engine="openpyxl") as writer:
            raw_out.to_excel(writer, index=False, sheet_name="Raw")
            cat.to_frame("sum").to_excel(writer, sheet_name="ByCategory")
            df.groupby("Payee", dropna=False)["Amount"].sum().sort_values(ascending=False).to_frame("sum").to_excel(writer, sheet_name="ByPayee")
            daily.to_frame("sum").to_excel(writer, sheet_name="ByDate")

        with open(cat_png, "rb") as f:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=f, caption=f"按类别（{month_arg}）")
        with open(daily_png, "rb") as f:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=f, caption=f"每日合计（{month_arg}）")
        with open(sum_xlsx, "rb") as f:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=f, filename=sum_xlsx.name)

    except Exception as e:
        logger.exception("生成报表失败")
        await update.message.reply_text(f"❌ 生成报表失败：{e}")

    finally:
        # 发送完后清理所有临时输出
        for p in [cat_png, daily_png, sum_xlsx]:
            try:
                if p and Path(p).exists():
                    Path(p).unlink()
            except Exception:
                pass

# /list [YYYY-MM] [N]
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_allowed(update):
        return
    try:
        args = context.args or []
        month = None
        limit = 20
        # 解析参数
        if len(args) >= 1:
            if re.fullmatch(r"\d{4}-\d{2}", args[0] or ""):
                month = args[0]
                if len(args) >= 2 and args[1].isdigit():
                    limit = int(args[1])
            elif args[0].isdigit():
                limit = int(args[0])
        if not month:
            month = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")

        col = get_mongo()
        chat_id = update.effective_chat.id
        cur = col.find(
            {"ym": month, "$or": [{"chat_id": chat_id}, {"chat_id": {"$exists": False}}]},
            sort=[("ts_utc", -1)],
            limit=limit
        )
        docs = list(cur)
        if not docs:
            await update.message.reply_text(f"⚠️ {month} 无入账记录。")
            return

        lines = [f"📄 最近 {len(docs)} 条（{month}）："]
        for i, d in enumerate(docs, 1):
            lines.append(f"{i:02d}. {fmt_doc_line(d)}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.exception("/list 失败")
        await update.message.reply_text(f"❌ /list 失败：{e}")

# 解析 k=v 参数（支持用引号包裹含空格的值）
def parse_kv_pairs(text: str) -> dict:
    pairs = {}
    for k, v in re.findall(r'(\w+)=(".*?"|\'.*?\'|[^\s]+)', text):
        if v.startswith(("'", '"')) and v.endswith(("'", '"')):
            v = v[1:-1]
        pairs[k.lower()] = v
    return pairs

# /edit <_id> key=value ...
# 允许字段：amount, category, payee, time(YYYY-MM-DD HH:MM)
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_allowed(update):
        return
    try:
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("用法：/edit <_id> amount=12.5 category=餐饮 payee=\"肯德基\" time=\"2025-08-12 19:30\"")
            return

        _id_raw = context.args[0]
        try:
            _id = ObjectId(_id_raw)
        except InvalidId:
            await update.message.reply_text("❌ _id 无效。")
            return

        kv_text = " ".join(context.args[1:])
        updates = parse_kv_pairs(kv_text)
        if not updates:
            await update.message.reply_text("❌ 未解析到任何可更新字段。")
            return

        allowed = {"amount", "category", "payee", "time"}
        unknown = set(updates.keys()) - allowed
        if unknown:
            await update.message.reply_text(f"❌ 存在不支持字段：{', '.join(unknown)}")
            return

        col = get_mongo()
        chat_id = update.effective_chat.id

        # 取原始文档（限定 chat）
        old = col.find_one({"_id": _id, "$or": [{"chat_id": chat_id}, {"chat_id": {"$exists": False}}]})
        if not old:
            await update.message.reply_text("❌ 未找到该记录（或不属于当前会话）。")
            return

        set_doc = {}
        if "amount" in updates:
            set_doc["amount"] = clean_amount(updates["amount"])
        if "category" in updates:
            set_doc["category"] = str(updates["category"]).strip()
        if "payee" in updates:
            set_doc["payee"] = str(updates["payee"]).strip()
        if "time" in updates:
            t = updates["time"].strip()
            try:
                _dt_local, _dt_utc = local_to_utc_dt(t)
            except Exception:
                await update.message.reply_text("❌ time 格式必须为 YYYY-MM-DD HH:MM（本地为 Asia/Shanghai）。")
                return
            set_doc["time_local"] = t
            set_doc["ym"] = t[:7]
            set_doc["ts_utc"] = _dt_utc
            set_doc["tz"] = "Asia/Shanghai"

        if not set_doc:
            await update.message.reply_text("❌ 没有可更新的内容。")
            return

        new_doc = col.find_one_and_update(
            {"_id": _id, "$or": [{"chat_id": chat_id}, {"chat_id": {"$exists": False}}]},
            {"$set": set_doc},
            return_document=ReturnDocument.AFTER
        )
        if not new_doc:
            await update.message.reply_text("❌ 更新失败。")
            return

        await update.message.reply_text(
            "✅ 已更新：\n"
            f"旧：{fmt_doc_line(old)}\n"
            f"新：{fmt_doc_line(new_doc)}"
        )

    except Exception as e:
        logger.exception("/edit 失败")
        await update.message.reply_text(f"❌ /edit 失败：{e}")

# /delete <_id[,<_id2> ...]>
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_allowed(update):
        return
    try:
        if not context.args:
            await update.message.reply_text("用法：/delete <_id[,<_id2> ...]>（多个可用逗号或空格分隔）")
            return

        # 支持逗号或空格
        raw = " ".join(context.args).replace(",", " ")
        id_strs = [s for s in raw.split() if s.strip()]
        ids = []
        invalid = []
        for s in id_strs:
            try:
                ids.append(ObjectId(s))
            except InvalidId:
                invalid.append(s)

        col = get_mongo()
        chat_id = update.effective_chat.id

        deleted = 0
        not_found = 0
        for oid in ids:
            res = col.delete_one({"_id": oid, "$or": [{"chat_id": chat_id}, {"chat_id": {"$exists": False}}]})
            if res.deleted_count == 1:
                deleted += 1
            else:
                not_found += 1

        msg = [f"🗑️ 删除完成：{deleted} 条"]
        if not_found:
            msg.append(f"未找到：{not_found} 条")
        if invalid:
            msg.append(f"无效 _id：{', '.join(invalid)}")
        await update.message.reply_text("；".join(msg))
    except Exception as e:
        logger.exception("/delete 失败")
        await update.message.reply_text(f"❌ /delete 失败：{e}")

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN 未配置")
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置")
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI 未配置")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.run_polling()

if __name__ == "__main__":
    main()
