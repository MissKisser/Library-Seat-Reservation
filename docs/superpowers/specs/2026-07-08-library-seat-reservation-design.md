# Library Seat Reservation Bot — Design Spec

- **Date**: 2026-07-08
- **Author**: brainstorming session
- **Target**: 超星(学习通)图书馆座位自动预约/签到/签退系统
- **Library ID**: 11692 (2号楼图书馆-3F-3楼备考自习室, 108 座)
- **Run target**: 本地电脑 (Win/Mac/Linux) **和** 云服务器/内网 7×24

---

## 1. Goals

构建一个 Python 程序,实现:

1. **多账号**: 在配置文件中管理 N 个超星账号,每个账号绑定一个固定座位
2. **多时段**: 支持"全选当日所有 7 段(08–22 每 2h)"和"选定若干连续/不连续时段"两种模式
3. **自动接力**: 每 2h 为一段,时段结束前 5 分钟自动签退 + 续约下一段 + 签到
4. **签到(纯 HTTP)**: 通过超星 Web API + 网易易盾 `enc`/`wyToken` 完成,用户已知风险
5. **Web 面板**: FastAPI + Jinja2,提供账号/任务/座位 CRUD、手动补发、实时座位状态、日志
6. **本地 + 云端两种部署形态**: `python -m seatbot run` 或 `docker-compose up -d`

---

## 2. Non-Goals

- **不**做真机蓝牙/位置签到(用户明确选择"纯 HTTP 刷定位包",已知账号风控风险)
- **不**做扫码登录(配置存明文手机号+密码)
- **不**做多座位动态抢座(账号↔座位 1:1 静态绑定,不存在多账号抢同一座位的竞争)
- **不**做通知(只日志输出到 stdout + 文件)
- **不**做付费(零依赖外部服务)

---

## 3. Architecture

```
┌────────────────────────────────────────────────────────────┐
│  Local / Server (Win/Mac/Linux, 7x24)                      │
│                                                            │
│  ┌────────────────────────────────────────────────────┐   │
│  │ FastAPI Web 面板 (Jinja2 + 极简 HTML, :8080)         │   │
│  │  - 账号/任务/座位 CRUD + 手动补发                    │   │
│  │  - 3F + 4F 实时座位状态(从超星拉)                    │   │
│  │  - 日志查看                                          │   │
│  └────────────────────────┬───────────────────────────┘   │
│                           │ HTTP API                       │
│  ┌────────────────────────▼───────────────────────────┐   │
│  │ APScheduler 调度器 (in-process)                     │   │
│  │  - bootstrap: 启动时按"现在该哪个段"排程             │   │
│  │  - 接力点: T-5min → leave + submit下段 + sign       │   │
│  │  - 22:00–次日 08:00 空转(无任务)                   │   │
│  └────────────────────────┬───────────────────────────┘   │
│                           │                                │
│  ┌────────────────────────▼───────────────────────────┐   │
│  │ SeatBot Engine (核心库)                             │   │
│  │  - ChaoxingClient (httpx async, cookies/UA 池)     │   │
│  │  - EncGenerator (exec JS 算 wyToken/enc)            │   │
│  │  - ReservationPlanner (时段切分 ≤2h / 状态机)       │   │
│  │  - StateStore (SQLite)                              │   │
│  └────────────────────────────────────────────────────┘   │
│                                                            │
│  Config: ./config.yaml   Logs: ./logs/*.log   DB: ./seatbot.db │
└────────────────────────────────────────────────────────────┘
```

---

## 4. Components

### 4.1 ChaoxingClient (`seatbot/client.py`)

- 基于 `httpx.AsyncClient`,全局共享一个 client per account
- 维护 `cookies` / `headers` (UA, Referer)
- 关键方法:
  - `login(phone, password) -> None` 调用 `passport2.chaoxing.com/fanyalogin`
  - `get_room_info(room_id) -> RoomConfig` (`/data/apps/seat/room/info`)
  - `submit_reserve(room_id, day, start, end, seat_num, enc, wyToken) -> ReserveResult` (`/data/apps/seat/submit`)
  - `sign(reserve_id) -> SignResult` (`/data/apps/seat/sign`)
  - `leave(reserve_id) -> LeaveResult` (`/data/apps/seat/leave`)
  - `cancel(reserve_id) -> CancelResult` (`/data/apps/seat/cancel`)
  - `get_seat_status(room_id, day) -> List[SeatStatus]` (用于面板实时座位图)
  - `get_active_reservation() -> ReserveRecord?` (查"我现在是不是还有预约")

### 4.2 EncGenerator (`seatbot/enc.py`)

- 用 `PyExecJS` (QuickJS / Node 子进程) 加载 `YiDunProtector-Web-2.1.4.js`
- 暴露 `compute(room_id, seat_num, day, start, end) -> {enc, wyToken}`
- **失败回退**: 启动 Playwright headless 跑 `code.html?id=...&seatNum=...` 拿前端生成的 `enc/wyToken`
- 缓存: 同一组参数 60s 内复用,避免重复计算

### 4.3 ReservationPlanner (`seatbot/planner.py`)

负责把用户的高层 slot 配置拆分为 ≤2h 的底层 task:

```python
def expand(account, day) -> List[Task]:
    # 1. account.slots 解析
    #    - "full" → 8:00-22:00 每 2h 一段
    #    - ["08:00-12:00", "14:00-22:00"] → 拆到 ≤2h
    # 2. 输出 Task(day, start, end) 列表
```

### 4.4 Scheduler (`seatbot/scheduler.py`)

基于 APScheduler, in-process 运行:

- **启动补偿**: 程序刚启动时,根据"当前时间",找到每个账号**应该处于哪一段**:
  - 如果该段 `pending` → 立即 `submit + sign`
  - 如果该段 `active` → 检查 `sign` 状态,未签到则补签
  - 如果该段已 `complete` 且下段 `pending` → 提前 `submit 下段 + sign`(不留间隙)
- **接力循环**: 对每个 active 任务,在 `end_time - 5min` 触发:
  ```
  leave(current) → submit(next) → sign(next)
  ```
- **空转**: 22:00–次日 08:00,scheduler 仍然运行,只是没有 active task
- **次日凌晨**: 00:00:05 触发新一天的 `bootstrap`(对所有账号 expand 当日 tasks); 用 `bootstrap_meta(account_id, day)` 表(或 `accounts.bootstrap_day`)记录"今日已 bootstrap",避免跨 0 点的偶发重启重复创建 tasks

### 4.5 StateStore (`seatbot/store.py`)

SQLite, 4 张表:

```sql
CREATE TABLE accounts (
  id          TEXT PRIMARY KEY,    -- 'zhangsan'
  phone       TEXT NOT NULL,
  password    TEXT NOT NULL,
  seat_num    TEXT NOT NULL,       -- '084'
  slots_json  TEXT NOT NULL,       -- 'full' or '["08:00-12:00", ...]'
  status      TEXT DEFAULT 'active', -- active / paused
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

CREATE TABLE tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL,
  day         TEXT NOT NULL,       -- '2026-07-09'
  start_time  TEXT NOT NULL,       -- '08:00'
  end_time    TEXT NOT NULL,       -- '10:00'
  status      TEXT NOT NULL,       -- pending/ready/submitting/active/leaving/complete/failed
  reserve_id  INTEGER,             -- 187985188
  last_error  TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

CREATE TABLE actions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  account_id  TEXT NOT NULL,
  action      TEXT NOT NULL,       -- submit/sign/leave/cancel/login
  request     TEXT,
  response    TEXT,
  success     INTEGER NOT NULL,
  message     TEXT
);

CREATE TABLE logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  level       TEXT NOT NULL,       -- INFO/WARN/ERROR
  account_id  TEXT,
  message     TEXT NOT NULL
);
```

### 4.6 Config (`seatbot/config.py`)

YAML 加载, schema 见 §6。

### 4.7 Web 面板 (`seatbot/web/`)

FastAPI + Jinja2, **无前端构建步骤**。详见 §7。

### 4.8 CLI (`seatbot/__main__.py`)

```bash
python -m seatbot run --config config.yaml   # 启动 Web + 调度
python -m seatbot once --account zhangsan --action all   # 跑一次(调试)
python -m seatbot login --account zhangsan   # 单独登录测试
python -m seatbot status                     # 打印状态
python -m seatbot init-db                    # 初始化 SQLite
```

---

## 5. Data Flow — A Typical Day

### 5.1 启动 (本地或服务器)

```
1. python -m seatbot run --config config.yaml
2. 加载 config.yaml → Config
3. 初始化 SQLite → StateStore
4. 启动 FastAPI on 0.0.0.0:8080
5. 启动 APScheduler (background)
6. scheduler.bootstrap():
   a. 对每个 account: ReservationPlanner.expand(account, today) → tasks
   b. 写入 tasks 表 (status=pending)
   c. 找到每个账号**当前**应该处于的 task, 置 status=ready
7. 调度器触发"接力"任务 (见 5.2)
```

### 5.2 接力 (T-5min)

```
account=zhangsan, current task=08:00-10:00, now=09:55
  1. ChaoxingClient.get_active_reservation(zhangsan)
       → {reserveId: 188xxx, endTime: 1783476000000, ...}
  2. leave(188xxx)           # 签退
  3. submit(08:00-10:00 已被 leave 释放) → 不会发生
  4. submit(10:00-12:00)     # 续下段
       → 需要 EncGenerator.compute(...) → enc/wyToken
       → POST /submit → {reserveId: 189xxx}
  5. sign(189xxx)            # 签到
  6. tasks[1].status=complete, tasks[2].status=active
  7. logs: INFO "relay OK 08-10 → 10-12"
```

### 5.3 启动补偿 (程序崩溃后重启)

```
now=11:30, zhangsan 期望正处于 10:00-12:00
  1. bootstrap(): 找到 task 10:00-12:00
  2. 查 get_active_reservation() → 仍在 (没签退)
  3. 检查 sign 状态 → 已签到 → 不动
  4. 等到 11:55 触发接力 → leave + submit 12:00-14:00 + sign
```

如果重启时**任务已过期**且**没续约**(比如程序挂了 30 分钟):

```
now=12:35, zhangsan 期望正处于 12:00-14:00 但没人签到
  1. 看到当前时段 12:00-14:00 状态=pending
  2. 立即 submit(12:00-14:00) → 失败 (服务器端"已开过签到窗口"或 "超 14:00")
  3. 日志 ERROR
  4. 标记 failed,等下一个可签时段(可能跳到 14:00-16:00)
```

---

## 6. Configuration Schema

`config.yaml`:

```yaml
library:
  room_id: 11692                     # 2号楼图书馆-3F
  room_name: 2号楼图书馆-3F-3楼备考自习室
  time_unit_minutes: 30
  open_time: "08:00"
  close_time: "22:00"
  max_reserve_hours: 2.0             # 超星单次最大预约

accounts:
  - id: zhangsan
    phone: "13800000001"
    password: "ENC_OR_PLAINTEXT"     # 明文, 文件权限 600
    seat_num: "084"                  # 3位数字字符串
    slots: full                      # 或 ["08:00-12:00", "14:00-22:00"]
  - id: lisi
    phone: "13800000002"
    password: "..."
    seat_num: "085"
    slots: ["08:00-12:00", "14:00-22:00"]

runtime:
  stagger_seconds: [0, 3]            # 多账号错峰启动 0-3s
  relogin_on_401: true
  random_ua: true                    # 不同账号不同 UA
  log_dir: ./logs
  db_path: ./seatbot.db
  web_host: 0.0.0.0
  web_port: 8080
```

`slots` 语义:
- `full`: 当日 8:00-22:00 每 2h 一段,共 7 段
- `["08:00-12:00", ...]`: 选定的连续/不连续时段,自动拆为 ≤2h 子段

---

## 7. Web Panel

**栈**: FastAPI + Jinja2 + 极简原生 CSS (无 Tailwind / Vue)。所有页面服务端渲染。

**页面**:

| 路由 | 用途 |
|---|---|
| `GET /` | Dashboard: 账号卡片 + 今日任务概览 + 错误告警 |
| `GET /accounts` | 账号列表 |
| `GET /accounts/new` | 新建账号表单 |
| `POST /accounts` | 提交新建 |
| `GET /accounts/{id}/edit` | 编辑 |
| `POST /accounts/{id}` | 提交编辑 |
| `POST /accounts/{id}/delete` | 删除 |
| `GET /tasks` | 任务列表 (按 day/account 过滤) |
| `POST /tasks` | 新建任务(指定 day/start/end) |
| `POST /tasks/{id}/sign` | 手动签到 |
| `POST /tasks/{id}/leave` | 手动签退 |
| `POST /tasks/{id}/cancel` | 手动取消 |
| `POST /tasks/quick-reserve` | 手动指定时段预约 |
| `GET /seats` | 3F + 4F 实时座位图 (每 30s 轮询 `get_seat_status`) |
| `GET /logs` | 日志查看 (按 level/account 过滤) |
| `GET /api/seats/{room_id}` | JSON 供前端轮询 |

**手动补发**(关键):
- 在 Dashboard 账号卡片上,放 4 个按钮:[立即签到] [立即签退] [立即续约] [取消当前]
- 点击后通过 FastAPI 后端调用 `ChaoxingClient` 对应方法,同步返回结果

**鉴权**: 无 (用户明确: 仅限内网)

---

## 8. Error Handling

| 错误 | 行为 |
|---|---|
| `submit` 返回 `座位已被预约` / `time conflict` | **不重试**,日志 ERROR,等下一个调度点(签退释放后才能续) |
| `submit` 返回 `风控校验失败` (enc/wyToken 失效) | 自动重新生成 enc,重试 1 次 |
| `submit` 返回 `未登录` (cookie 失效) | 调 fanyalogin 重登,重试当前动作 1 次 |
| `sign` 返回 `不在签到范围` / `蓝牙校验失败` | **不重试**,日志 ERROR。已知:用户接受"刷定位包"风险 |
| 任何连续 3 次失败 | 标记账号 `paused`,面板显示告警 |
| `enc` 算法更新 (网易易盾 JS 变更) | 运行时检测到异常,自动回退到 Playwright headless 加载前端 JS 计算 |
| 程序崩溃 | systemd / Docker 自动重启,从 SQLite 恢复状态(启动补偿 §5.3) |
| 22:00-次日 08:00 期间 | 调度器空转,无任务,等待次日 00:00:05 重新 bootstrap |

**已知风险**(写入 spec,用户已确认):
- 纯 HTTP 签到: 缺少真实蓝牙基站 + 位置,超星风控可能识别为作弊,**账号可能被拉入黑名单**
- 一次性多账号并发: 同一 IP 多账号,可能被识别为"脚本"
- 用户已明确接受这些风险

---

## 9. Risk & Mitigation

| 风险 | 缓解 |
|---|---|
| `enc`/`wyToken` 算法变更 | EncGenerator 内置 JS 加载 + Playwright 回退; 监控 `/submit` 错误率,异常时告警 |
| cookie 失效 | `relogin_on_401: true` 自动重登 |
| 配置文件泄露 (明文密码) | README 提示 `chmod 600 config.yaml`;后续可加 `cryptography.fernet` 加密 (YAGNI for v1) |
| SQLite 损坏 | 每次启动备份 `seatbot.db` 到 `seatbot.db.bak` |
| 多个账号同时启动被风控 | `stagger_seconds: [0, 3]` 错峰 + 随机 UA/Referer |
| 时段冲突(签退前下个时段被人抢) | 不重试,日志 ERROR,面板告警;让用户重新选时段 |

---

## 10. Deployment

### 10.1 本地测试

```bash
git clone <repo>
cd Library-Seat-Reservation
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
cp config.example.yaml config.yaml
# 编辑 config.yaml 填入账号
python -m seatbot init-db
python -m seatbot run
# 访问 http://localhost:8080
```

### 10.2 云服务器

提供 `Dockerfile` + `docker-compose.yml` + `systemd/seatbot.service`:

```bash
docker compose up -d
# 或
sudo cp systemd/seatbot.service /etc/systemd/system/
sudo systemctl enable --now seatbot
```

---

## 11. File Layout

```
Library-Seat-Reservation/
├── seatbot/
│   ├── __init__.py
│   ├── __main__.py
│   ├── client.py             # ChaoxingClient
│   ├── enc.py                # EncGenerator
│   ├── planner.py            # ReservationPlanner
│   ├── scheduler.py          # APScheduler wrapper
│   ├── store.py              # StateStore (SQLite)
│   ├── models.py             # dataclasses
│   ├── config.py             # YAML loader
│   ├── web/
│   │   ├── __init__.py
│   │   ├── app.py
│   │   ├── routes.py
│   │   ├── templates/
│   │   │   ├── base.html
│   │   │   ├── dashboard.html
│   │   │   ├── accounts.html
│   │   │   ├── tasks.html
│   │   │   ├── seats.html
│   │   │   └── logs.html
│   │   └── static/
│   │       └── style.css
│   └── utils/
│       ├── ua.py             # User-Agent 池
│       └── timeutil.py
├── config.example.yaml
├── Dockerfile
├── docker-compose.yml
├── systemd/seatbot.service
├── requirements.txt
├── pyproject.toml
├── README.md
└── docs/
    └── superpowers/
        └── specs/
            └── 2026-07-08-library-seat-reservation-design.md
```

---

## 12. Open Questions / Future

- **v2**: 配置加密 (`cryptography.fernet`)
- **v2**: 通知 (Server 酱 / 钉钉 webhook)
- **v2**: 多图书馆支持 (3F + 4F 切换;当前只支持单馆)
- **v2**: 任务模板 (周一三五 + 周二四 不同配置)
- **v3**: 真机蓝牙/位置代理(用 Android 设备 + ADB-WIFI)
