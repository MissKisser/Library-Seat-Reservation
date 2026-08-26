# Library Seat Reservation Bot — Design Spec

- **Date**: 2026-07-08
- **Author**: brainstorming session
- **Target**: 超星(学习通)图书馆座位 **自动预约/签到/签退** 系统 — **单座位保护模式**
- **Library ID**: 11692 (2号楼图书馆-3F-3楼备考自习室, 108 座)
- **Run target**: 本地电脑 (Win/Mac/Linux) **和** 云服务器/内网 7×24

## 0. 一句话需求

> 用户希望坐在图书馆某个固定座位(`target_seat_num`)学习时,该座位**始终显示"已有人预约"**,从而阻止他人抢走。本程序用 N 个守护账号在 `target_seat_num` 上错时接力预约/签到/签退,形成全天护城河。

---

## 1. Goals

构建一个 Python 程序,实现:

1. **单目标座位保护 (Seat Guard)**: 配置 1 个 `target_seat_num`(用户实际坐的座位),通过 N 个守护账号在不同时段抢同一座位,形成"全天护城河"
2. **多守护账号**: 配置文件管理 N 个超星账号(守护账号),每个守护账号覆盖一组时段;不同时段之间允许重叠以保证连续覆盖
3. **多时段**: 支持"全选当日 08–22 每 2h"和"选定若干连续/不连续时段"两种模式;每段 ≤ 2h(超星限制)
4. **自动接力**: 守护账号在某段 `end_time` 前自动签退 + 续约下一段 + 签到;重叠时段下,签退后才让出座位
5. **签到(纯 HTTP)**: 通过超星 Web API + 网易易盾 `enc`/`wyToken` 完成,用户已知风险
6. **Web 面板必须覆盖全配置**: 座位号、守护账号增删改、时段可视化编辑、覆盖连续性检查、覆盖图(Gantt)、手动操作、日志、告警
7. **本地 + 云端两种部署形态**: `python -m seatbot run` 或 `docker-compose up -d`

> **核心目的**: 用户到图书馆坐在 `target_seat_num` 时,该座位**始终有其他守护账号显示"已预约"**,从而阻止他人抢走。

---

## 2. Non-Goals

- **不**做真机蓝牙/位置签到(用户明确选择"纯 HTTP 刷定位包",已知账号风控风险)
- **不**做扫码登录(配置存明文手机号+密码)
- **不**做多座位保护(每次只保护 1 个目标座位;若想保护多个,启动多个实例)
- **不**做用户本人账号的自动预约/签到(用户自己手动;本程序只跑守护账号)
- **不**做通知(只日志输出到 stdout + 文件 + Web 日志页)
- **不**做付费(零依赖外部服务)

---

## 3. Architecture

```
┌────────────────────────────────────────────────────────────┐
│  Local / Server (Win/Mac/Linux, 7x24)                      │
│                                                            │
│  ┌────────────────────────────────────────────────────┐   │
│  │ FastAPI Web 面板 (Jinja2 + 极简 HTML, :8080)         │   │
│  │  - 覆盖图(Gantt) / 目标座位 / 守护账号 CRUD         │   │
│  │  - 时段编辑器 + 覆盖连续性校验                       │   │
│  │  - 整馆 108 座实时状态(高亮 target_seat_num)        │   │
│  │  - 日志 + 失守告警                                  │   │
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
│  │  - ReservationPlanner (账号+slots → ≤2h tasks       │   │
│  │                          全部指向 target_seat_num)  │   │
│  │  - StateStore (SQLite)                              │   │
│  └────────────────────────────────────────────────────┘   │
│                                                            │
│  Config: ./config.yaml   Logs: ./logs/*.log   DB: ./seatbot.db │
│                                                            │
│  所有守护账号 → target_seat_num (library.target_seat_num)  │
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
  - `submit_reserve(room_id, day, start, end, seat_num, enc, wyToken) -> ReserveResult` (`/data/apps/seat/submit`) — `seat_num` 由 `LibraryConfig.target_seat_num` 提供(不是账号自带)
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

负责把每个守护账号的高层 slot 配置拆分为 ≤2h 的底层 task(全部指向 `target_seat_num`):

```python
def expand(account, day, target_seat_num) -> List[Task]:
    # 1. account.slots 解析
    #    - "full" → 8:00-22:00 每 2h 一段
    #    - ["09:00-11:00", "13:00-15:00"] → 拆到 ≤2h
    # 2. 输出 Task(account_id, day, start, end, seat_num=target_seat_num) 列表
```

### 4.4 Scheduler (`seatbot/scheduler.py`)

基于 APScheduler, in-process 运行,任务全是"为某守护账号在 `target_seat_num` 的某时段抢座":

- **启动补偿**: 程序刚启动时,根据"当前时间",找到每个守护账号**应该处于哪一段**:
  - 如果该段 `pending` → 立即 `submit + sign`
  - 如果该段 `active` → 检查 `sign` 状态,未签到则补签
  - 如果该段已 `complete` 且下段 `pending` → 提前 `submit 下段 + sign`(不留间隙)
- **接力循环**: 对每个 active 任务,在 `end_time - 5min` 触发:
  ```
  leave(current) → submit(next, same target_seat_num) → sign(next)
  ```
  `next` 时段可能由不同守护账号执行(对应不同的 slots 配置)
- **空转**: 22:00–次日 08:00,scheduler 仍然运行,只是没有 active task
- **次日凌晨**: 00:00:05 触发新一天的 `bootstrap`(对所有守护账号 expand 当日 tasks); 用 `bootstrap_meta(account_id, day)` 表(或 `accounts.bootstrap_day`)记录"今日已 bootstrap",避免跨 0 点的偶发重启重复创建 tasks

### 4.5 StateStore (`seatbot/store.py`)

SQLite, 4 张表:

```sql
CREATE TABLE accounts (
  id          TEXT PRIMARY KEY,    -- 'guard_a'
  phone       TEXT NOT NULL,
  password    TEXT NOT NULL,
  slots_json  TEXT NOT NULL,       -- 'full' or '["09:00-11:00", ...]'
  status      TEXT DEFAULT 'active', -- active / paused
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
  -- 注意:不再存 seat_num。所有守护账号抢同一个 target_seat_num(由 library 配置持有)
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
target_seat_num=84, current task=guard_a@08:00-10:00, now=09:55
  1. ChaoxingClient.get_active_reservation(guard_a, seat=84)
       → {reserveId: 188xxx, endTime: 1783476000000, ...}
  2. signback(188xxx)      # 签退 guard_a 当前段 (2026-08-25 修正: 真签退=signback, leave=暂离)
  3. submit(seat=84, day, 10:00-12:00, by guard_b)  # 由下个时段配置的守护账号续约
       → 需要 EncGenerator.compute(...) → enc/wyToken
       → POST /submit → {reserveId: 189xxx}
  4. sign(189xxx)            # guard_b 签到
  5. tasks[guard_a@08-10].status=complete, tasks[guard_b@10-12].status=active
  6. logs: INFO "relay OK guard_a@08-10 → guard_b@10-12 (seat 84)"
```

注:由于相邻账号 slots 通常重叠 30min,接力时上家尚未签退,下家已签到(覆盖空档)。

### 5.3 启动补偿 (程序崩溃后重启)

```
now=11:30, target_seat_num=84
  1. bootstrap(): 找到所有当前时段的 tasks
  2. 对每个 active task: get_active_reservation() → 仍在 (没签退)
  3. 检查 sign 状态 → 已签到 → 不动
  4. 等到每个 task 接近 end_time → 接力 → leave + submit 下段(同 seat 84,可能换账号) + sign
```

如果重启时**任务已过期**且**没续约**(比如程序挂了 30 分钟):

```
now=12:35, target_seat_num=84, 当前应覆盖 12:00-14:00 但没人签到
  1. 看到当前时段 12:00-14:00 状态=pending
  2. 立即 submit(seat=84, 12:00-14:00) → 失败 (服务器端"已开过签到窗口"或 "超 14:00")
  3. 日志 ERROR
  4. 标记该 task failed,该时段"失守",Web 覆盖图红色标记
  5. 若配置了多守护账号覆盖同一时段,自动尝试下一守护账号;仍失败则保留失守状态
```

---

## 6. Configuration Schema

`config.yaml`:

```yaml
library:
  room_id: 11692                     # 2号楼图书馆-3F
  room_name: 2号楼图书馆-3F-3楼备考自习室
  target_seat_num: "84"              # 【核心】要保护的座位号;所有守护账号都抢这个
  time_unit_minutes: 30
  open_time: "08:00"
  close_time: "22:00"
  max_reserve_hours: 2.0             # 超星单次最大预约

# 守护账号:不同时段抢同一个 target_seat_num,形成全天覆盖
# 编号必须 1-N 顺序,系统按顺序尝试;账号间时段允许重叠以保证无缝衔接
accounts:
  - id: guard_a
    phone: "13800000001"
    password: "ENC_OR_PLAINTEXT"     # 明文, 文件权限 600
    slots: ["09:00-11:00", "13:00-15:00", "17:30-19:30"]
  - id: guard_b
    phone: "13800000002"
    password: "..."
    slots: ["10:30-12:30", "15:00-17:00", "19:30-21:30"]
  - id: guard_c
    phone: "13800000003"
    password: "..."
    slots: ["08:00-10:30", "12:00-14:00", "19:00-21:30"]

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
- `["09:00-11:00", "13:00-15:00", ...]`: 选定的连续/不连续时段,自动拆为 ≤2h 子段

**配置约定**:
- 所有守护账号抢同一个 `target_seat_num`
- 相邻账号的 `slots` 建议**首尾重叠 30 分钟**(A→09-11, B→10:30-12:30),保证 A 签退前 B 已签到,无人能抢空档
- 若某时段所有守护账号都失败,该时段"失守";Web 覆盖图红色标记,日志 ERROR

---

## 7. Web Panel

**栈**: FastAPI + Jinja2 + 极简原生 CSS (无 Tailwind / Vue)。所有页面服务端渲染。

**所有配置必须在 Web 面板可改;配置文件只是初始 seed。**

**页面**:

| 路由 | 用途 |
|---|---|
| `GET /` | **覆盖图(Gantt)**: 横轴=今日 08:00-22:00(30min/格),纵轴=守护账号列表;每格显示该账号在该时段的状态(已预约/已签到/失守/待执行) |
| `GET /seat-config` | **座位保护配置**: 设置 `target_seat_num` |
| `POST /seat-config` | 提交目标座位号 |
| `GET /accounts` | 守护账号列表(含每账号覆盖时段) |
| `GET /accounts/new` | 新建守护账号表单(phone + password + slots 编辑器) |
| `POST /accounts` | 提交新建(自动尝试登录验证) |
| `POST /accounts/{id}/test-login` | **测试登录**: 返回登录成功/失败 |
| `GET /accounts/{id}/edit` | 编辑(含 slots 表格) |
| `POST /accounts/{id}` | 提交编辑 |
| `POST /accounts/{id}/delete` | 删除 |
| `POST /accounts/{id}/slots` | **时段编辑**: 单独编辑某账号的 slots 列表;提交后自动校验所有账号合并后是否覆盖 08-22 |
| `GET /coverage` | **覆盖连续性报告**: 列出未覆盖时段和重叠时段;红色标记"失守" |
| `GET /tasks` | 任务列表(按 day/account 过滤) |
| `POST /tasks` | 新建任务(指定 day/start/end + account_id) |
| `POST /tasks/{id}/sign` | 手动签到 |
| `POST /tasks/{id}/leave` | 手动签退 |
| `POST /tasks/{id}/cancel` | 手动取消 |
| `POST /tasks/quick-reserve` | 手动指定时段+账号预约 |
| `GET /seats` | 实时座位图(整馆 108 座 30s 轮询;突出显示 target_seat_num) |
| `GET /logs` | 日志查看(按 level/account 过滤) |
| `GET /api/coverage` | JSON: 覆盖图数据(供前端轮询) |
| `GET /api/seats/availability` | JSON: `target_seat_num` 在指定 day/start/end 是否可用 (前端选时段时实时校验) |
| `GET /api/seats/{room_id}` | JSON: 整馆座位状态 |

**时段编辑**(`POST /accounts/{id}/slots`):
- 接受 `["09:00-11:00", "13:00-15:00", ...]` 列表
- **选时段时实时校验**:前端在编辑时段前调用 `/api/seats/availability?day=YYYY-MM-DD&start=HH:MM&end=HH:MM&seat=NN`,返回 `target_seat_num` 在该时段是否被他人占;若被占,前端直接置灰该格不可选
- 提交后立即在 DB 中 upsert
- `GET /coverage` 自动合并所有账号 slots,显示 08-22 是否完全覆盖;若有空档仅警告(不阻止保存 — 用户明确:覆盖连续性由手选时实时校验,不强强制)

**实时校验后端**:
- `/api/seats/availability` 调用超星 `get_room_info` 取 `seatIntervalMap`,判断 `target_seat_num` 在请求时段是否已有 reserve_id
- 若已被占,返回 `{available: false, occupied_by: "...", until: "HH:MM"}`,前端置灰
- 校验可能有 ~1s 延迟(用户接受)

**手动补发**(关键):
- 在 Dashboard 覆盖图上,点击某格(账号+时段),弹出 4 个按钮:[立即签到] [立即签退] [立即续约] [取消当前]
- 点击后通过 FastAPI 后端调用 `ChaoxingClient` 对应方法,同步返回结果

**鉴权**: 无 (用户明确: 仅限内网)

---

## 8. Error Handling

| 错误 | 行为 |
|---|---|
| `submit` 返回 `座位已被预约` / `time conflict` | 立即尝试同一时段的下一个守护账号;全部失败则标记该时段失守,Web 红色 |
| `submit` 返回 `风控校验失败` (enc/wyToken 失效) | 自动重新生成 enc,重试 1 次 |
| `submit` 返回 `未登录` (cookie 失效) | 调 fanyalogin 重登,重试当前动作 1 次 |
| `sign` 返回 `不在签到范围` / `蓝牙校验失败` | **不重试**,日志 ERROR。已知:用户接受"刷定位包"风险 |
| 任何守护账号连续 3 次失败 | 标记该守护账号 `paused`,面板显示告警;其他守护账号继续保护 |
| `enc` 算法更新 (网易易盾 JS 变更) | 运行时检测到异常,自动回退到 Playwright headless 加载前端 JS 计算 |
| 程序崩溃 | systemd / Docker 自动重启,从 SQLite 恢复状态(启动补偿 §5.3) |
| 22:00-次日 08:00 期间 | 调度器空转,无任务,等待次日 00:00:05 重新 bootstrap |
| 时段空档(覆盖图未覆盖 08-22 整段) | Web `/coverage` 列出空档时段;面板显示告警(可配置为强制 422 阻止保存) |

**已知风险**(写入 spec,用户已确认):
- 纯 HTTP 签到: 缺少真实蓝牙基站 + 位置,超星风控可能识别为作弊,**守护账号可能被拉入黑名单**
- 一次性多守护账号并发: 同一 IP 多账号,可能被识别为"脚本"
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
