// server.js
require("dotenv").config();
const express = require("express");
const cors = require("cors");
const mongoose = require("mongoose");
const path = require("path");

const app = express();

/* ======================
 * Config
 * ====================== */
const PORT = process.env.PORT || 3000;
const MONGODB_URI = process.env.MONGODB_URI;
const DB_NAME = process.env.DB_NAME || undefined;
const COLLECTION = process.env.COLLECTION || "expenses"; // change if your collection is different

// Optional: lightweight header auth
const API_KEY = process.env.API_KEY || null;
// Optional AI for text-to-expense parsing
const AI_PROVIDER = (process.env.AI_PROVIDER || "").toLowerCase(); // "openai" | "dashscope" | ""
const OPENAI_API_KEY = process.env.OPENAI_API_KEY || "";
const OPENAI_MODEL = process.env.OPENAI_MODEL || "gpt-4o-mini";
const DASHSCOPE_API_KEY = process.env.DASHSCOPE_API_KEY || "";
const QWEN_MODEL = process.env.QWEN_MODEL || "qwen-turbo";

// Optional: force a single chat/user id (e.g. 957879521)
const CHAT_ID =
  process.env.FORCE_CHAT_ID && !Number.isNaN(+process.env.FORCE_CHAT_ID)
    ? +process.env.FORCE_CHAT_ID
    : null;

/* ======================
 * Middleware
 * ====================== */
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));

const requireKey = (req, _res, next) => {
  if (!API_KEY) return next();
  const key = req.get("x-api-key");
  if (key === API_KEY) return next();
  return next({ status: 401, message: "Unauthorized" });
};

/* ======================
 * DB
 * ====================== */
mongoose
  .connect(MONGODB_URI, { dbName: DB_NAME })
  .then(() => console.log("MongoDB connected"))
  .catch((err) => {
    console.error("MongoDB connection error:", err.message);
    process.exit(1);
  });

// We keep schema flexible so legacy fields from your bot (ts_utc, time_local, tz, ym, etc.) are preserved.
const expenseSchema = new mongoose.Schema(
  {
    amount: { type: Number, required: true, min: 0 },
    category: { type: String, default: "Uncategorized", index: true },
    payee: { type: String, default: "", index: true },
    // Preferred canonical time field
    time: { type: Date, index: true },

    // Legacy/extra fields used by your Telegram bot
    chat_id: { type: Number, index: true },
    ts_utc: { type: Date },
    time_local: { type: String },
    tz: { type: String },
    ym: { type: String },
    created_at_utc: { type: Date },

    note: { type: String, default: "" },
    source: {
      type: String,
      enum: ["telegram", "web", "import", "other"],
      default: "telegram",
    },
    createdAt: { type: Date, default: Date.now },
  },
  { versionKey: false, strict: false }
);

const Expense = mongoose.model("Expense", expenseSchema, COLLECTION);

/* ======================
 * Helpers
 * ====================== */
const parseDate = (s) => (s ? new Date(s) : null);
const rxEscape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Basic Chinese/English keyword map for category guessing
const CATEGORY_KEYWORDS = {
  "餐饮": [
    "麦当劳",
    "肯德基",
    "星巴克",
    "美团",
    "饿了么",
    "必胜客",
    "海底捞",
    "喜茶",
    "奶茶",
    "餐饮",
    "外卖",
    "饭",
    "午饭",
    "晚饭",
    "早餐",
    "火锅",
  ],
  "购物": ["淘宝", "天猫", "京东", "拼多多", "超市", "沃尔玛", "山姆", "购物", "买菜"],
  "出行": ["滴滴", "高德", "打车", "共享单车", "地铁", "公交", "高速", "停车"],
  "数码": ["Apple", "苹果", "小米", "华为", "数码", "配件"],
  "娱乐": ["腾讯视频", "爱奇艺", "优酷", "B站", "QQ音乐", "网易云", "游戏", "会员", "电影"],
  "通讯": ["话费", "流量", "联通", "移动", "电信", "宽带"],
  "医疗": ["医院", "药店", "医保", "体检", "诊所"],
  "转账": ["转账", "收款", "还款", "红包", "待确认收款"],
  "生活缴费": ["水费", "电费", "燃气", "物业", "供暖", "生活缴费"],
};
const CATEGORY_PRIORITY = [
  "转账",
  "生活缴费",
  "出行",
  "餐饮",
  "购物",
  "数码",
  "娱乐",
  "通讯",
  "医疗",
];

function guessCategory(payee = "", note = "") {
  const text = `${payee} ${note}`.toLowerCase();
  const hits = new Set();
  for (const [cat, kws] of Object.entries(CATEGORY_KEYWORDS)) {
    for (const kw of kws) {
      if (!kw) continue;
      if (text.includes(String(kw).toLowerCase())) {
        hits.add(cat);
        break;
      }
    }
  }
  if (!hits.size) {
    if (text.includes("转账") || text.includes("收款")) return "转账";
    return "其他";
  }
  for (const c of CATEGORY_PRIORITY) if (hits.has(c)) return c;
  return [...hits][0];
}

// Heuristic parser as fallback (CN-friendly)
function parseTextHeuristic(text) {
  const s = (text || "").trim();
  // amount
  const amtMatch = s.match(/(-?\d+(?:[.,]\d+)?)(?:\s*(?:元|块|rmb|cny|￥))?/i);
  const amount = amtMatch ? Number(amtMatch[1].replace(",", "")) : null;

  // time/date
  // Try YYYY-MM-DD HH:mm or HH:mm
  let timeStr = null;
  const mFull = s.match(/(\d{4}-\d{1,2}-\d{1,2})[ T]?(\d{1,2}:\d{2})/);
  const mYMD = s.match(/(\d{4}-\d{1,2}-\d{1,2})/);
  const mHM = s.match(/(\d{1,2}:\d{2})/);
  if (mFull) timeStr = `${mFull[1]} ${mFull[2]}`;
  else if (mYMD && mHM) timeStr = `${mYMD[1]} ${mHM[1]}`;
  else if (mHM) timeStr = mHM[1];

  // crude payee extraction: words after 在/于/去/给/向 or first CN word block
  let payee = "";
  const mPayee = s.match(/[在于去给向]([\u4e00-\u9fa5A-Za-z0-9_\-·]{2,20})/);
  if (mPayee) payee = mPayee[1];
  if (!payee) {
    const mCn = s.match(/([\u4e00-\u9fa5A-Za-z]{2,20})/);
    payee = mCn ? mCn[1] : "";
  }

  const category = guessCategory(payee, s);
  const note = s;
  return { amount, time: timeStr, category, payee, note };
}

function extractJsonLoose(str) {
  if (!str) return null;
  try {
    return JSON.parse(str);
  } catch {}
  const codeBlock = str.match(/```json\s*(\{[\s\S]*?\})\s*```/i);
  if (codeBlock) {
    try { return JSON.parse(codeBlock[1]); } catch {}
  }
  const objLike = str.match(/(\{[\s\S]*\})/);
  if (objLike) {
    try { return JSON.parse(objLike[1]); } catch {}
  }
  return null;
}

async function callOpenAIExtract(text) {
  if (!OPENAI_API_KEY) return null;
  const sys = "You extract structured expense data from short user texts. Output ONLY compact JSON with keys: amount(number), category(string), payee(string), time(string), note(string). If time mentions only hour:minute, output HH:MM. If missing, output empty string. Chinese supported.";
  const user = `文本：${text}\n请抽取并只返回 JSON：{ "amount": number, "category": string, "payee": string, "time": string, "note": string }`;
  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${OPENAI_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: OPENAI_MODEL,
      temperature: 0,
      messages: [
        { role: "system", content: sys },
        { role: "user", content: user },
      ],
    }),
  });
  if (!res.ok) return null;
  const data = await res.json();
  const content = data?.choices?.[0]?.message?.content || "";
  return extractJsonLoose(content);
}

async function callQwenExtract(text) {
  if (!DASHSCOPE_API_KEY) return null;
  const payload = {
    model: QWEN_MODEL,
    input: {
      messages: [
        {
          role: "user",
          content: [
            {
              text:
                "从用户发送的记账文本中抽取字段，仅返回 JSON（不要解释/不要Markdown）：" +
                '{ "amount": 数字, "category": "字符串", "payee": "字符串", "time": "字符串或空", "note": "原文或摘要" }' +
                "。如果只有时间（如19:17）只返回HH:MM；若无时间返回空字符串。文本：" +
                String(text),
            },
          ],
        },
      ],
    },
    parameters: { use_raw_prompt: true },
  };
  const res = await fetch(
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${DASHSCOPE_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    }
  );
  if (!res.ok) return null;
  const data = await res.json();
  const content = data?.output?.choices?.[0]?.message?.content;
  let textOut = "";
  if (Array.isArray(content)) {
    const t = content.find((c) => c.text)?.text;
    textOut = t || "";
  } else if (typeof content === "string") {
    textOut = content;
  }
  return extractJsonLoose(textOut);
}

function combineDateTime(tz, timeStr) {
  // timeStr can be "YYYY-MM-DD HH:mm" or "HH:mm"; default to today in tz
  try {
    const tzIntl = tz || "Asia/Shanghai";
    if (!timeStr) return new Date();
    const hm = timeStr.match(/^(\d{1,2}:\d{2})$/);
    let localStr;
    if (hm) {
      // Build today in tz
      const now = new Date();
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: tzIntl,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      })
        .formatToParts(now)
        .reduce((acc, p) => ((acc[p.type] = p.value), acc), {});
      localStr = `${parts.year}-${parts.month}-${parts.day} ${hm[1]}`;
    } else {
      localStr = timeStr;
    }
    // Parse as local time in tz by approximating via Date + offset formatting
    // We will trust server's Date parsing in UTC then adjust mirrors later.
    return new Date(localStr.replace("/", "-"));
  } catch {
    return new Date();
  }
}

// Format "YYYY-MM-DD HH:mm" in a timezone
function formatTimeLocal(date, tz = "Asia/Shanghai") {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    })
      .formatToParts(date)
      .reduce((acc, p) => ((acc[p.type] = p.value), acc), {});
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  } catch {
    // Fallback to UTC if timezone is invalid
    const d = new Date(date);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(
      d.getUTCDate()
    )} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  }
}

// Normalize _time from multiple possible fields (time, ts_utc, time_local, created_at_utc, createdAt)
const addParsedTimeStage = {
  $addFields: {
    _time: {
      $let: {
        vars: { tz: { $ifNull: ["$tz", "Asia/Shanghai"] } },
        in: {
          $switch: {
            branches: [
              // 1) time already a Date
              { case: { $eq: [{ $type: "$time" }, "date"] }, then: "$time" },

              // 2) time is a string -> try common formats, then generic toDate
              {
                case: { $eq: [{ $type: "$time" }, "string"] },
                then: {
                  $ifNull: [
                    {
                      $dateFromString: {
                        dateString: "$time",
                        format: "%Y-%m-%d %H:%M",
                        onNull: null,
                        onError: null,
                      },
                    },
                    {
                      $ifNull: [
                        {
                          $dateFromString: {
                            dateString: "$time",
                            format: "%Y/%m/%d, %H:%M",
                            onNull: null,
                            onError: null,
                          },
                        },
                        { $toDate: "$time" },
                      ],
                    },
                  ],
                },
              },

              // 3) bot's ts_utc (Date)
              {
                case: { $eq: [{ $type: "$ts_utc" }, "date"] },
                then: "$ts_utc",
              },

              // 4) created_at_utc (Date)
              {
                case: { $eq: [{ $type: "$created_at_utc" }, "date"] },
                then: "$created_at_utc",
              },

              // 5) time_local string + tz (e.g., "2025-08-12 12:50")
              {
                case: { $eq: [{ $type: "$time_local" }, "string"] },
                then: {
                  $ifNull: [
                    {
                      $dateFromString: {
                        dateString: "$time_local",
                        format: "%Y-%m-%d %H:%M",
                        timezone: "$$tz",
                        onNull: null,
                        onError: null,
                      },
                    },
                    {
                      $dateFromString: {
                        dateString: "$time_local",
                        timezone: "$$tz",
                        onNull: null,
                        onError: null,
                      },
                    },
                  ],
                },
              },

              // 6) createdAt (Date)
              {
                case: { $eq: [{ $type: "$createdAt" }, "date"] },
                then: "$createdAt",
              },
            ],
            default: null,
          },
        },
      },
    },
  },
};

// Optional chat_id guard stage
const chatGuardStage = CHAT_ID
  ? { $match: { $or: [{ chat_id: CHAT_ID }, { chat_id: { $exists: false } }] } }
  : null;

/* ======================
 * Routes
 * ====================== */

// Health
app.get("/api/health", (_req, res) => res.json({ ok: true }));

// List with date range + search + pagination
app.get("/api/expenses", requireKey, async (req, res) => {
  try {
    const page = Math.max(parseInt(req.query.page || "1", 10), 1);
    const limit = Math.min(
      Math.max(parseInt(req.query.limit || "50", 10), 1),
      500
    );
    const start = parseDate(req.query.start);
    const end = parseDate(req.query.end);
    const q = (req.query.q || "").trim();

    const pipeline = [addParsedTimeStage];
    if (chatGuardStage) pipeline.push(chatGuardStage);

    // Ad-hoc chat_id filter via query (?chat_id=957879521)
    if (req.query.chat_id) {
      const reqChatId = +req.query.chat_id;
      if (!Number.isNaN(reqChatId))
        pipeline.push({ $match: { chat_id: reqChatId } });
    }

    if (start || end) {
      const bound = {};
      if (start) bound.$gte = start;
      if (end) bound.$lte = end;
      pipeline.push({ $match: { _time: bound } });
    }

    if (q) {
      const r = new RegExp(rxEscape(q), "i");
      pipeline.push({
        $match: { $or: [{ payee: r }, { category: r }, { note: r }] },
      });
    }

    pipeline.push(
      { $sort: { _time: -1, _id: -1 } },
      {
        $project: {
          amount: 1,
          category: 1,
          payee: 1,
          note: 1,
          source: 1,
          chat_id: 1,
          createdAt: 1,
          time: "$_time",
        },
      },
      {
        $facet: {
          data: [{ $skip: (page - 1) * limit }, { $limit: limit }],
          meta: [{ $count: "total" }],
        },
      },
      {
        $project: {
          data: 1,
          total: { $ifNull: [{ $arrayElemAt: ["$meta.total", 0] }, 0] },
        },
      }
    );

    const out = await Expense.aggregate(pipeline)
      .option({ allowDiskUse: true })
      .exec();

    const { data, total } = out[0] || { data: [], total: 0 };
    res.json({ total, page, pages: Math.ceil(total / limit), limit, data });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "Failed to fetch expenses" });
  }
});

// Create
app.post("/api/expenses", requireKey, async (req, res) => {
  try {
    const { amount, category, payee, time, note, source, tz } = req.body || {};
    if (!amount || !time) {
      return res.status(400).json({ error: "amount and time are required" });
    }

    const payload = {
      amount: Number(amount),
      category: category || "Uncategorized",
      payee: payee || "",
      note: note || "",
      source: source || "web",
      time: new Date(time),
    };

    // Add chat_id if forced or provided
    if (CHAT_ID) payload.chat_id = CHAT_ID;
    else if (req.body.chat_id && !Number.isNaN(+req.body.chat_id)) {
      payload.chat_id = +req.body.chat_id;
    }

    // Legacy-shaped compatibility fields (optional, nice for mixed data)
    const tzStr = tz || "Asia/Shanghai";
    payload.ts_utc = payload.time;
    payload.tz = tzStr;
    payload.time_local = formatTimeLocal(payload.time, tzStr);
    payload.ym = payload.time.toISOString().slice(0, 7); // "YYYY-MM"
    payload.created_at_utc = new Date();

    const doc = await Expense.create(payload);
    res.status(201).json(doc);
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "Failed to create expense" });
  }
});

// Parse raw text to expense via AI + fallback, then create
app.post("/api/expenses/parse", requireKey, async (req, res) => {
  try {
    const { text, tz, chat_id } = req.body || {};
    if (!text || !String(text).trim()) {
      return res.status(400).json({ error: "text is required" });
    }

    const tzStr = tz || "Asia/Shanghai";

    let parsed = null;
    // Choose provider
    if (AI_PROVIDER === "openai" && OPENAI_API_KEY) {
      parsed = await callOpenAIExtract(text).catch(() => null);
    } else if (AI_PROVIDER === "dashscope" && DASHSCOPE_API_KEY) {
      parsed = await callQwenExtract(text).catch(() => null);
    } else if (OPENAI_API_KEY) {
      // default to OpenAI if key present
      parsed = await callOpenAIExtract(text).catch(() => null);
    } else if (DASHSCOPE_API_KEY) {
      parsed = await callQwenExtract(text).catch(() => null);
    }

    // Fallback if AI not configured or failed
    if (!parsed || typeof parsed !== "object") {
      parsed = parseTextHeuristic(String(text));
    }

    // Normalize fields
    const amount = Number(parsed.amount);
    if (!Number.isFinite(amount) || amount === 0) {
      return res.status(400).json({ error: "failed to parse amount" });
    }
    const payee = (parsed.payee || "").toString();
    const note = (parsed.note || "").toString() || String(text);
    let category = (parsed.category || "").toString();
    if (!category) category = guessCategory(payee, note);

    // time: accept full or HH:mm; default now in tz
    const dt = combineDateTime(tzStr, (parsed.time || "").toString().trim());

    const payload = {
      amount: Number(amount),
      category: category || "Uncategorized",
      payee: payee || "",
      note,
      source: "web",
      time: dt,
    };
    if (CHAT_ID) payload.chat_id = CHAT_ID;
    else if (chat_id && !Number.isNaN(+chat_id)) payload.chat_id = +chat_id;

    // Legacy mirrors
    payload.ts_utc = payload.time;
    payload.tz = tzStr;
    payload.time_local = formatTimeLocal(payload.time, tzStr);
    payload.ym = payload.time.toISOString().slice(0, 7);
    payload.created_at_utc = new Date();

    const doc = await Expense.create(payload);
    res.status(201).json({ created: doc, parsed });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "Failed to parse and create expense" });
  }
});

// Update (partial)
app.patch("/api/expenses/:id", requireKey, async (req, res) => {
  try {
    const id = req.params.id;

    const fields = {};
    if (req.body.amount !== undefined) fields.amount = Number(req.body.amount);
    if (req.body.category !== undefined)
      fields.category = req.body.category || "Uncategorized";
    if (req.body.payee !== undefined) fields.payee = req.body.payee || "";
    if (req.body.note !== undefined) fields.note = req.body.note || "";
    if (req.body.time !== undefined) {
      fields.time = new Date(req.body.time);
      // keep legacy mirrors in sync
      const tzStr = req.body.tz || "Asia/Shanghai";
      fields.ts_utc = fields.time;
      fields.time_local = formatTimeLocal(fields.time, tzStr);
      fields.tz = tzStr;
      fields.ym = fields.time.toISOString().slice(0, 7);
    }

    const updated = await Expense.findByIdAndUpdate(id, fields, { new: true });
    if (!updated) return res.status(404).json({ error: "Not found" });
    res.json(updated);
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "Failed to update expense" });
  }
});

// Delete
app.delete("/api/expenses/:id", requireKey, async (req, res) => {
  try {
    const id = req.params.id;
    const deleted = await Expense.findByIdAndDelete(id);
    if (!deleted) return res.status(404).json({ error: "Not found" });
    res.json({ ok: true });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "Failed to delete expense" });
  }
});

// Category summary (sum amount by category for a range)
app.get("/api/stats/category", requireKey, async (req, res) => {
  try {
    const start = parseDate(req.query.start);
    const end = parseDate(req.query.end);

    const pipeline = [addParsedTimeStage];
    if (chatGuardStage) pipeline.push(chatGuardStage);

    if (req.query.chat_id) {
      const reqChatId = +req.query.chat_id;
      if (!Number.isNaN(reqChatId))
        pipeline.push({ $match: { chat_id: reqChatId } });
    }

    if (start || end) {
      const bound = {};
      if (start) bound.$gte = start;
      if (end) bound.$lte = end;
      pipeline.push({ $match: { _time: bound } });
    }

    pipeline.push(
      {
        $group: {
          _id: "$category",
          total: { $sum: "$amount" },
          count: { $sum: 1 },
        },
      },
      { $sort: { total: -1 } },
      {
        $project: {
          _id: 0,
          category: { $ifNull: ["$_id", "Uncategorized"] },
          total: 1,
          count: 1,
        },
      }
    );

    const agg = await Expense.aggregate(pipeline)
      .option({ allowDiskUse: true })
      .exec();

    res.json(agg);
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: "Failed to build summary" });
  }
});

/* ======================
 * Error handler
 * ====================== */
app.use((err, _req, res, _next) => {
  const status = err.status || 500;
  res.status(status).json({ error: err.message || "Server error" });
});

/* ======================
 * Start
 * ====================== */
app.listen(PORT, () => {
  console.log(`Server listening on http://localhost:${PORT}`);
  if (API_KEY) console.log("API key auth is ON (header: x-api-key)");
  if (CHAT_ID !== null) console.log(`FORCE_CHAT_ID=${CHAT_ID} is active`);
});
