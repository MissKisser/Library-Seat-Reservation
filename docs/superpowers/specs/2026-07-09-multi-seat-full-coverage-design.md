# Library Seat Reservation Bot — Design Spec (v2: 多目标座位 + 时段满约)

- **Date**: 2026-07-09
- **Author**: 规格重写 session
- **Prev spec**: `2026-07-08-library-seat-reservation-design.md`(单座位护城河)
- **Target**: 超星(学习通)图书馆座位 **自动预约/签到/签退** 系统 — **多目标座位 + 时段满约模式**
- **Library**: 11692 (2号楼图书馆-3F-3楼备考自习室, 108 座)
- **Run target**: 本地电脑 (Win/Mac/Linux) **和** 云服务器/内网 7×24

---

## 0. 一句话需求

> 用户希望图书馆里的 **N 个目标座位** (典型 N=2, 可扩) **在所有想要的时段** (例如早上 8:30-10:30, 下午 15:00-17:00, 晚上 19:30-21:30) 都显示"已有人预约",从而阻止他人抢走。本程序用一组守护账号配合 N 个目标座位,**最大化每个账号的可用预约次数 (≤2h/次)**,把整张时间表拼满。
>
> **场景示例**:2 个目标座位 × 3 个时段/天 × 2 天 = 12 段。
> - **悲观假设** (单账号每天 1 段):需要 12 个独立账号
> - **乐观假设** (单账号允许持有 N 段不同座位不同时段):可能压缩到 4-6 个账号
> - **系统设计**:在数据库层支持全笛卡尔积 (每段可独立指定 account+seat),实际账号数由用户配置决定,系统不替用户做"合并段"的优化承诺

---

## 1. Goals

构建一个 Python 程序,实现:

1. **多目标座位 (Multi-Seat Guard)**: 配置 N 个 `target_seat_num` (用户想保护的座位) ,通过一组守护账号在不同时段抢这些座位,形成"全天多座护城河"
2. **守护账号 ↔ 目标座位 绑定**: 每个守护账号可绑定 0~N 个目标座位 (binding 列表可空表示"通用账号,可用于任何目标座位")
3. **多时段**: 每个 account 在每个绑定的 seat 上各自配置 slots (或继承自全局 target window) ;每段 ≤ 2h (超星后端限制)
4. **自动接力**: 守护账号在某段 `end_time - 5min` 自动签退 + 续约下一段 (跨账号或跨座位) + 签到;重叠时段下,签退后才让出座位
5. **签到 (纯 HTTP)**: 通过超星 Web API + 网易易盾 `enc`/`wyToken` 完成,用户已知风险
6. **Web 面板必须覆盖全配置**: 目标座位列表、守护账号增删改、时段可视化编辑、覆盖连续性检查、按座位维度的覆盖图 (Gantt)、手动操作、日志、告警
7. **本地 + 云端两种部署形态**: `python -m seatbot run` 或 `docker-compose up -d`

> **核心目的**: 用户到图书馆坐在 `target_seat_num` 列表中的某个座位时,该座位 **以及它的相邻座位 (用户特地配置 N>1)** 都始终显示"已预约",从而阻止他人抢走。

---

## 2. Non-Goals

- **不**做真机蓝牙/位置签到 (用户明确选择"纯 HTTP 刷定位包",已知账号风控风险)
- **不**做扫码登录 (配置存明文手机号+密码)
- **不**做用户本人账号的自动预约/签到 (用户自己手动;本程序只跑守护账号)
- **不**做通知 (只日志输出到 stdout + 文件 + Web 日志页)
- **不**做付费 (零依赖外部服务)
- **不**做"账号自动发现哪几个座位还有段可拼"的智能合并 (用户明确要求"为每一个目标座位绑定账号",账号角色明确)

---

## 3. 容量与下限推算 (实测坑,先写清楚)

### 3.1 已知约束

| 约束 | 数值 | 来源 |
|---|---|---|
| 账号单次预约上限 | **≤ 2h** | 学习通后端 `max_reserve_hours` |
| 单账号每天预约次数上限 | **1 段 / 账号 / 座位 / 天** (悲观) 或 **多段** (乐观) | 取决于超星实际校验,**未实测 (用户授权今日 14:00 后实测)** |
| 开放预约窗口 | **每天 14:00 开放次日预约** | 用户描述 |
| 14:00 之前 | 次日预约窗口未开启,需等待 | 用户描述 |
| 单账号预约的座位 | 每段 = 1 个座位 | 后端硬约束 |

### 3.2 数值例子 — 2 座位 × 3 时段 × 2 天

设:
- N = 2 个目标座位 (seat_A, seat_B)
- T = 3 个时段 (早 8:30-10:30, 午 15:00-17:00, 晚 19:30-21:30),每段 2h
- D = 2 天 (今天 + 明天, 或 明天 + 后天,具体看开放预约窗口)

总 (座位 × 时段 × 天) 段数 = N × T × D = **2 × 3 × 2 = 12 段**。

#### 悲观 (1 账号 1 段)

- 每天每 (座 × 段) 都需要 1 个独立账号
- 每个账号只能在自己的时段使用一次,完成后当天不再有 role
- 每天需要 = 2 × 3 = **6 账号/天**
- 2 天 = **12 账号** (如果账号可复用,隔夜 clean 时段记录则可减半,具体由用户决定)

#### 乐观 (1 账号可拼多段不同座位)

- 1 个账号 1 天内可以:早上 A → 下午 B → 晚上 A (3 段)
- 每天需要 = (2×3) / 3 = **2 账号/天**
- 2 天 = **4 账号** (上午/下午/晚上 整 2 天由 4 个账号轮班)

#### 系统设计原则

**不在系统层强制乐观/悲观, 而是让用户能选**:
- **配置级** `one_account_max_concurrent_segments_per_day`:
  - `1` (悲观) — 每个 (account, day) 仅允许 1 段,系统强校验
  - `N≥3` (乐观) — 每个 (account, day) 允许 N 段,系统不阻止 (实际后端拒绝则任务 FAILED)
- 调度器在 `expand_for_day` 时,若超过该上限,自动把溢出段分配给其他账号 (round-robin)

### 3.3 多座位的组合排程

为了真正覆盖 N=2 个座位,T=3 个时段的最优账号编排 (乐观情形):

| 时段 | seat_A | seat_B |
|---|---|---|
| 08:30-10:30 | 账号 1 | 账号 2 |
| 15:00-17:00 | 账号 2 | 账号 1 |
| 19:30-21:30 | 账号 1 | 账号 2 |

只要 **2 个账号** 就能拼满 1 天。**这就是用户说的"捷径"是否成立的关键**——能否实现完全取决于后端是否允许同账号在不同座位的预约共存。

---

## 4. Architecture

```
┌───────────────────────────────────────────────────────────────┐
│  Local / Server (Win/Mac/Linux, 7x24)                        │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ FastAPI Web 面板 (Jinja2 + 极简 HTML, :8080)          │   │
│  │  - /targets        N 个目标座位增删改                 │   │
│  │  - /accounts       守护账号列表                       │   │
│  │  - /accounts/{id}  账号 ↔ 目标座位的绑定矩阵          │   │
│  │  - /coverage       按座位维度的 Gantt 覆盖图          │   │
│  │  - /tasks          任务表 (account × seat × slot)    │   │
│  │  - /seats          整馆 108 座 (高亮 N 个目标)       │   │
│  │  - /logs           日志                               │   │
│  └────────────────────────┬─────────────────────────────┘   │
│                           │ HTTP API                        │
│  ┌────────────────────────▼─────────────────────────────┐   │
│  │ APScheduler 调度器 (in-process)                      │   │
│  │  - bootstrap: 启动时按"现在该哪段"排程               │   │
│  │  - 接力点: T-5min → leave + submit下段 + sign        │   │
│  │    (下段 account/seat 由 planner 决定)               │   │
│  │  - 22:00–次日 08:00 空转                            │   │
│  └────────────────────────┬─────────────────────────────┘   │
│                           │                                  │
│  ┌────────────────────────▼─────────────────────────────┐   │
│  │ SeatBot Engine (核心库)                              │   │
│  │  - ChaoxingClient (httpx async, cookies/UA 池)      │   │
│  │  - EncGenerator (exec JS 算 wyToken/enc)             │   │
│  │  - ReservationPlanner                                │   │
│  │       account × slots × bound_seat → Task 列表      │   │
│  │  - StateStore (SQLite)                               │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  Config: ./config.yaml   Logs: ./logs/*.log   DB: ./seatbot.db │
│                                                              │
│  守护账号 ↔ 目标座位 (来自 library.target_seats 列表)         │
└──────────────────────────────────────────────────────────────┘
```

---

## 5. Components

### 5.1 ChaoxingClient (`seatbot/client.py`)

不变,但 `submit_reserve` 的 `seat_num` 不再固定从全局配置读:

```python
async def submit_reserve(self, room_id, day, start, end, seat_num, enc, wy_token, captcha=""):
    ...
```

`seat_num` 来自 `Task.seat_num` (新字段)。

### 5.2 ReservationPlanner (`seatbot/planner.py`) — 重大改写

旧版:每账号一个全局 slot 列表,所有 task 共享 `library.target_seat_num`。

新版:
- 输入:(account, day, account_seat_bindings: list[str], slot_spec)
- 输出:list[Task] (每个 task 自带 `seat_num`, `day`, `start_time`, `end_time`)
- 规则:
  - 对 account 在每个绑定 seat 上各 expand 一遍 slots
  - 若 account **未绑定任何 seat** (空 binding),则 planner 用 `library.target_seats` 中的所有座位各 expand 一遍 (备用"通用账号")
  - 跨段自动拆 ≤ 2h, 复用现有 `expand_account_slots`

```python
@dataclass
class PlannerInput:
    account: Account
    day: date
    bound_seats: list[str]            # 该账号绑定的目标座位
    fallback_seats: list[str]          # 当 bound_seats 空时用的目标座位
    slot_spec: str | list[str]
    max_reserve_hours: float

class ReservationPlanner:
    def expand_for_day(self, day: date) -> list[Task]:
        seats = self.bound_seats or self.fallback_seats
        if not seats:
            raise PlannerError("no seats bound and no fallback")
        chunks = expand_account_slots(self.slot_spec, self.max_reserve_hours)
        out: list[Task] = []
        for seat in seats:
            for s, e in chunks:
                out.append(Task(
                    id=None,
                    account_id=self.account.id,
                    day=day,
                    start_time=s,
                    end_time=e,
                    seat_num=seat,                  # ★ 新字段
                    status=TaskStatus.PENDING,
                ))
        return out
```

### 5.3 Scheduler (`seatbot/scheduler.py`)

仅在 `_run_submit_sign` 处把 `cfg.library.target_seat_num` 替换为 `t.seat_num`;**接力**逻辑也按 (account, day, start_time) 排序找到**下一个 task** (不再要求同 account),接力可能跨 account / 跨 seat。

```python
async def _run_submit_sign(self, acc: Account, t: Task) -> None:
    ...
    r = await client.submit_in_browser(
        phone=acc.phone, password=acc.password,
        room_id=self.cfg.library.room_id,
        seat_num=t.seat_num,           # ★ 从 task 读
        day=t.day.isoformat(),
        start_time=t.start_time.strftime("%H:%M"),
        end_time=t.end_time.strftime("%H:%M"),
    )
    ...
```

**新的"接力"逻辑**:
```python
async def _maybe_relay(self, t: Task, now: datetime) -> None:
    """在 t.end_time - 5min 触发,leave + 找下段 task + submit/sign。

    下段不一定由同一 account 执行 — 由 (day, start_time > t.start_time, 优先 sorted by account_id) 的下一个 PENDING/READY task 决定。
    """
    # 1. leave current
    await client.leave(t.reserve_id)
    # 2. find next task in store
    nxt = await self.store.find_next_task(day=t.day, after_start=t.end_time, only=("pending","ready","failed"))
    if not nxt: return
    nxt_acc = await self.store.get_account(nxt.account_id)
    await self._run_submit_sign(nxt_acc, nxt)
```

### 5.4 StateStore (`seatbot/store.py`) — Schema 升级

新增 2 张表,改 1 张表:

#### 新表 `target_seats` (N 个目标座位)

```sql
CREATE TABLE target_seats (
  seat_num    TEXT PRIMARY KEY,     -- '084'
  label       TEXT,                  -- 可选昵称 (e.g. '靠窗主座')
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
```

#### 新表 `account_seat_bindings` (account ↔ seat 多对多)

```sql
CREATE TABLE account_seat_bindings (
  account_id  TEXT NOT NULL,
  seat_num    TEXT NOT NULL,
  priority    INTEGER NOT NULL DEFAULT 0,   -- 优先级,小者优先用于该时段
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (account_id, seat_num)
);
```

#### 改 `tasks` 加 `seat_num`

```sql
ALTER TABLE tasks ADD COLUMN seat_num TEXT;
-- 注意:已存在的旧 task (target_seat_num) 需要在迁移时把 data backfill 到 seat_num
```

#### 迁移 (v1→v2)

```python
async def _migrate_v1_to_v2(self) -> None:
    """v1 (单 target_seat_num) → v2 (target_seats + bindings + per-task seat_num)。

    1. 把 accounts.bootstrap_day 保留
    2. 把 accounts.status 保留
    3. 把旧的 library.target_seat_num (从 cfg 读) 转成一条 target_seats 行
    4. 不动现有 tasks (只是其 seat_num 空)
    """
    cfg_seat = self._legacy_target_seat_num  # 注入
    if cfg_seat and not await self._has_seat(cfg_seat):
        await self.add_target_seat(cfg_seat, label="migrated from v1")
    # 给所有现有 task 补 seat_num (从 cfg_seat 抄)
    await self.db.execute(
        "UPDATE tasks SET seat_num=? WHERE seat_num IS NULL AND day<=?",
        (cfg_seat, today_cst().isoformat())
    )
```

### 5.5 Config (`seatbot/config.py`)

```python
class LibraryConfig(BaseModel):
    room_id: int
    room_name: str
    time_unit_minutes: int = 30
    open_time: str = "08:00"
    close_time: str = "22:00"
    max_reserve_hours: float = 2.0
    # 已移除: target_seat_num (改放到 target_seats 表 + web UI 维护)

class AccountConfig(BaseModel):
    id: str
    phone: str
    password: str
    slots: SlotSpec                 # 单 slots 列表
    bound_seats: list[str] = []     # ★ 新增: 绑定的目标座位 (空 = 通用)
    one_account_max_concurrent_segments_per_day: int = 1   # ★ 新增 (默认悲观)
```

### 5.6 Web 面板 (`seatbot/web/`) — 路由重排

| 路由 | 用途 |
|---|---|
| `GET /` | **覆盖图 (Gantt)**:**纵轴 = 目标座位** (而非账号);每行座位显示该座位的 hours/段分配;右上角下拉选择账号维度的覆盖视图 |
| `GET /targets` | **目标座位列表** N 个 seat_num 增删改 |
| `GET /targets/new` | 新建目标座位 (表单:seat_num, label) |
| `POST /targets` | 提交新建 |
| `POST /targets/{seat_num}/delete` | 删除 |
| `GET /accounts` | 守护账号列表 (含每个账号绑定的目标座位 chips) |
| `GET /accounts/new` | 新建 (phone + password + slots + **bound_seats 多选**) |
| `POST /accounts` | 提交新建 |
| `GET /accounts/{id}/edit` | **编辑账号**:phone, password, **slots**(full/custom JSON), bound_seats, max_segments — **slots 支持 Web 直接修改,无需重启** |
| `POST /accounts/{id}` | 提交编辑 |
| `POST /accounts/{id}/delete` | 删除 |
| `POST /accounts/{id}/bindings` | **单独编辑绑定**:多选 `<input type=checkbox>` 选择该账号绑定的目标座位 |
| `GET /coverage` | **覆盖连续性报告**:按座位行 (N 行) × 时段列的矩阵;红色 = 失守 |
| `GET /tasks` | 任务表 (含 seat_num 列) |
| `GET /api/coverage?day=...` | JSON: 按座位 × 时段的覆盖数据 |
| `GET /api/coverage-by-account?day=...` | JSON: 按账号 × 时段 (兼容旧面板) |
| `GET /api/seats/availability?day=...&start=...&end=...&seat=NN` | JSON: 该 seat 在该时段是否空闲 (供时段编辑器实时校验) |

### 5.7 CLI (`seatbot/__main__.py`)

新增命令:

**目标座位管理**
```bash
python -m seatbot targets list
python -m seatbot targets add 084 --label "靠窗主座"
python -m seatbot targets del 085
```

**手动触发单次预约（可控性/调试/补救场景）**
```bash
python -m seatbot reserve --account guard_a --seat 084 --date 2026-07-10 --start 09:00 --end 11:00
# 不指定 --date 时默认预约明天
python -m seatbot reserve --account guard_b --seat 085 --start 15:00 --end 17:00
```

**用户硬预约段管理（scheduler 跳过这些时段）**
```bash
python -m seatbot user-reserved list
python -m seatbot user-reserved add --account my_account --seat 084 --day 2026-07-10 --start 09:00 --end 11:00
python -m seatbot user-reserved del 5
```

---

## 6. Data Flow — A Typical Day

### 6.1 启动

```
1. python -m seatbot run --config config.yaml
2. 加载 config.yaml → Config (含 library + accounts[].bound_seats)
3. 初始化 SQLite → StateStore (v2 schema, 自动迁移 v1)
4. 启动 FastAPI on 0.0.0.0:8080
5. 启动 APScheduler (background)
6. scheduler.bootstrap():
   a. 对每个 account: ReservationPlanner.expand_for_day(day, bound_seats=...)
       → 任务列表,每个 Task 自带 seat_num
   b. 写入 tasks 表 (status=pending)
   c. 找到每个 (account × seat) 当前应该处于的 task, 置 status=ready
7. 调度器触发"接力"任务 (见 6.2)
```

### 6.2 接力 (T-5min,跨座位跨账号允许)

```
now = 09:55, 当前 task = (guard_a, seat=084, 08:00-10:00)
  1. ChaoxingClient.get_active_reservation(guard_a, seat=084) → {reserveId: 188xxx, endTime: ...}
  2. signback(188xxx)      # 签退 guard_a 当前段 (2026-08-25 修正: 真签退=signback, leave=暂离)
  3. submit(seat=085, day, 10:00-12:00, by guard_b)
     — 由任务表里下一个 start_time > 10:00 的 task 决定账号与座位
     → EncGenerator.compute(...) → enc/wyToken
     → POST /submit → {reserveId: 189xxx}
  4. sign(189xxx)             # guard_b 签到
  5. tasks[guard_a@084×08-10].status = complete
     tasks[guard_b@085×10-12].status = active
```

### 6.3 启动补偿 (程序崩溃后重启,跨座接力)

```
now = 11:30
  - 找到今天所有 tasks
  - 对每个 ACTIVE task: get_active_reservation() → 仍在则不动 → 否则 leave → 把 status=FAILED 标记,触发补救
  - 对每个 PENDING task whose start_time <= now < end_time: 立即 submit+sign(用 task 自己的 seat_num)
```

### 6.4 每天14:00滚动触发次日预约

超星预约系统在每天14:00后开放次日预约窗口，系统通过 APScheduler 定时任务精准触发:

```
now = 14:00:10 (Asia/Shanghai)
  1. _afternoon_bootstrap() 触发
  2. tomorrow = today + 1 day
  3. 对每个 account:
     a. ReservationPlanner.expand_for_day(tomorrow, bound_seats=...)
        → 生成明天的 task 列表 (seat_num, start_time, end_time)
     b. 跳过与 user_reserved 冲突的 task
     c. 写入 tasks 表 (status=pending)
  4. tick_account 在每分钟轮询中发现 status=pending 且 start_time > now
     → 在 14:00 时段内立即 submit + sign (预约窗口刚开)

防重机制: (account_id, day, seat_num) 三元组写入 _bootstrap_done_for set,
重复调用幂等。
```

**滚动接力示例 (2座位 × 3时段, 2账号乐观模式)**:

| 日期 | 14:00触发 | 预约目标 |
|---|---|---|
| Day 0 (今天) | → 生成 Day 1 的 6 段任务并立即提交 | Day 1: 座位A/B × 09:00/15:00/19:30 |
| Day 1 (明天) | → 生成 Day 2 的 6 段任务并立即提交 | Day 2: 座位A/B × 09:00/15:00/19:30 |

全程无人工干预, 任意天均有明天的段被占住。

---

## 7. Configuration Schema

`config.yaml`:

```yaml
library:
  room_id: 11692
  room_name: 2号楼图书馆-3F-3楼备考自习室
  time_unit_minutes: 30
  open_time: "08:00"
  close_time: "22:00"
  max_reserve_hours: 2.0

# 目标座位 (N 个) — Web 面板维护,这里是 seed
target_seats:
  - seat_num: "084"           # 主座 (用户自己坐)
    label: "靠窗主座"
  - seat_num: "085"           # 备用 1 (保护相邻)
    label: "旁边"
  # 也可 - seat_num: "086"   进一步扩展

# 守护账号 (M 个),每个绑定 0..N 个目标座位
accounts:
  # 账号 1:绑 084 — 早上 + 晚上
  - id: guard_a
    phone: "13800000001"
    password: "ENC_OR_PLAINTEXT"
    slots: ["08:30-10:30", "19:30-21:30"]
    bound_seats: ["084"]
  # 账号 2:绑 085 — 早上 + 下午 + 晚上 (跨乐观,看后端是否允许)
  - id: guard_b
    phone: "13800000002"
    password: "..."
    slots: ["08:30-10:30", "15:00-17:00", "19:30-21:30"]
    bound_seats: ["085"]
  # 账号 3:绑两座 — 覆盖 084+085 的"无缝接力"
  - id: guard_c
    phone: "13800000003"
    password: "..."
    slots: ["08:00-10:30", "12:30-15:00", "17:00-19:30"]
    bound_seats: ["084", "085"]   # ★ 同时负责 2 个座位
  # 悲观兜底:每天 1 段的备用账号
  - id: guard_extra
    phone: "13800000004"
    password: "..."
    slots: ["10:30-12:30"]
    bound_seats: ["084"]

runtime:
  stagger_seconds: [0, 3]
  relogin_on_401: true
  random_ua: true
  one_account_max_concurrent_segments_per_day_default: 1  # 全局默认,账户级可覆盖
  log_dir: ./logs
  db_path: ./seatbot.db
  web_host: 0.0.0.0
  web_port: 8080
```

`one_account_max_concurrent_segments_per_day` 语义:
- `1`:每个 (account, day) 最多 1 个 task,可被 scheduler 强制 (拒绝超过则错误)
- `N>=3`:乐观,允许 N 段并存,后端拒绝则任务 FAILED

**配置约定**:
- 目标座位 N 与守护账号 M 是解耦的
- 一个账号可绑 0-N 个座位;绑 0 时默认使用所有 enabled 的 target_seats
- 一个座位可被 0-M 个账号绑定
- 时段重叠(座位上同一时段多个账号)按 `account_seat_bindings.priority` 小的优先,其余作为冗余备份

---

## 8. Web Panel — 关键页面

### 8.1 目标座位管理 (`/targets`)

```
┌─ Add Target Seat ────────────┐
│ Seat #: [ 084 ]              │
│ Label:  [ 靠窗主座  ]        │
│ [ Add ]                       │
└────────────────────────────────┘
┌─ Active Target Seats ─────────────────────────────────┐
│ ▢ 084   靠窗主座          [edit] [delete]              │
│ ▢ 085   旁边              [edit] [delete]              │
└──────────────────────────────────────────────────────────┘
```

### 8.2 账号 ↔ 座位 绑定矩阵 (`/accounts/{id}/bindings`)

```
Account: guard_a
Bound seats:  [x] 084 (靠窗主座)    [ ] 085 (旁边)    [ ] 086
                                                          [ Save ]
```

### 8.3 覆盖图 Gantt (`/`),**纵轴 = 座位**

```
                08  09  10  11  12  13  14  15  16  17  18  19  20  21
084 靠窗主座    [==guard_a==]      [===guard_c===]   [==guard_a==]
085 旁边        [==guard_b==]      [=guard_b=]       [=guard_b=]
086 (空)        · · · · · · · · · · · · · · · · · · · · ·
                ↑           ↑                  ↑
                当前 09:23   接力点 10:25       接力点 19:25
```

颜色:绿 = 已签到 / 黄 = 已预约未签到 / 灰 = 待执行 / 红 = 失守

### 8.4 整馆座位图 (`/seats`)

高亮显示所有 N 个目标座位 (e.g. 084 红色边框, 085 橙色边框),其他普通座位灰显。

---

## 9. Error Handling

| 错误 | 行为 |
|---|---|
| `submit` 返回 `座位已被预约` / `time conflict` | 立即尝试同一时段的下一个守护账号 (跨座位也允许);全部失败则标记该 (seat × slot) 失守,Web 红色 |
| `submit` 返回 `风控校验失败` (enc/wyToken 失效) | 自动重新生成 enc,重试 1 次 |
| `submit` 返回 `未登录` (cookie 失效) | 调 fanyalogin 重登,重试当前动作 1 次 |
| `submit` 返回 `已有未完成预约` (账号当天已有另一段) | 任务 FAILED,日志 WARN (用户应增加 `one_account_max_concurrent_segments_per_day` 数量或换成多账号) |
| `sign` 返回 `不在签到范围` / `蓝牙校验失败` | **不重试**,日志 ERROR。用户接受"刷定位包"风险 |
| 任何守护账号连续 3 次失败 | 标记该守护账号 `paused`,面板显示告警;其他守护账号继续保护 |
| `enc` 算法更新 (网易易盾 JS 变更) | 运行时检测到异常,自动回退到 Playwright headless 加载前端 JS 计算 |
| 程序崩溃 | systemd / Docker 自动重启,从 SQLite 恢复状态 |
| 22:00-次日 08:00 期间 | 调度器空转 |
| 14:00 之前尝试预约次日 | submit 必失败,任务 FAILED,日志 WARN `预约窗口未开` |

**已知风险** (写入 spec, 用户已确认):
- 纯 HTTP 签到: 缺少真实蓝牙基站 + 位置,超星风控可能识别为作弊,守护账号可能被拉入黑名单
- 一次性多守护账号并发: 同一 IP 多账号,可能被识别为"脚本"
- 同账号多段: **未实测** — 是否被允许取决于超星后端实际行为;测试脚本 `scripts/verify_multi_seat_one_account.py`
- 用户已明确接受这些风险

---

## 10. Risk & Mitigation

| 风险 | 缓解 |
|---|---|
| 同账号当天多段被拒绝 (实测后才知道) | planner 在 `expand_for_day` 时读取 `one_account_max_concurrent_segments_per_day`,超过则 round-robin 分配给其他账号;web 仪表盘提示"按 1 账号 1 段计, 满约需要 X 个账号" |
| `enc` / `wyToken` 算法变更 | EncGenerator 内置 JS 加载 + Playwright 回退 |
| cookie 失效 | `relogin_on_401: true` 自动重登 |
| 配置文件泄露 (明文密码) | README 提示 `chmod 600 config.yaml` |
| SQLite 损坏 | 每次启动备份 `seatbot.db` 到 `seatbot.db.bak` |
| 多个账号同时启动被风控 | `stagger_seconds: [0, 3]` 错峰 + 随机 UA/Referer |
| 时段冲突 (签退前下个时段被人抢) | 不重试,日志 ERROR,面板告警 |

---

## 11. Deployment

不变 (沿用 v1 的本地 / docker-compose / systemd)。

---

## 12. File Layout

```
Library-Seat-Reservation/
├── seatbot/
│   ├── client.py                  # 不变
│   ├── enc.py                     # 不变
│   ├── planner.py                 # ★ 改写 (account × bound_seats → task list)
│   ├── scheduler.py               # 改 _run_submit_sign + _maybe_relay
│   ├── store.py                   # ★ 新增 target_seats + account_seat_bindings + tasks.seat_num + 迁移
│   ├── models.py                  # ★ Task 增加 seat_num;新增 SeatTarget / SeatBinding dataclass
│   ├── config.py                  # ★ LibraryConfig 去掉 target_seat_num,新增 one_account_max_*
│   ├── web/
│   │   ├── app.py                 # 不变
│   │   ├── routes.py              # ★ 增 /targets/* + 改 /accounts/* + Gantt 切换维度
│   │   ├── templates/
│   │   │   ├── base.html
│   │   │   ├── dashboard.html     # ★ 行=座位
│   │   │   ├── targets_list.html
│   │   │   ├── targets_form.html
│   │   │   ├── accounts_form.html # ★ bound_seats 多选
│   │   │   ├── accounts_list.html # ★ chips 显示绑定
│   │   │   ├── coverage.html      # ★ 行=座位
│   │   │   ├── tasks_list.html    # ★ 加 seat_num 列
│   │   │   ├── seats.html
│   │   │   └── logs.html
│   │   └── static/style.css
│   └── utils/timeutil.py          # 不变
├── config.example.yaml            # ★ 改注释 + 改字段名
├── scripts/
│   └── verify_multi_seat_one_account.py   # ★ 新增 — 实测多段限制
├── docs/superpowers/specs/
│   └── 2026-07-09-multi-seat-full-coverage-design.md  # 本文件
└── ...
```

---

## 13. Open Questions / Future

- **v2 实测验证**:今天 (7-09) 14:00 后用 `scripts/verify_multi_seat_one_account.py` 测同账号是否可持 2 段;结果决定 planner 默认值是 `1` 还是 `3`
- **v2**: 配置加密 (`cryptography.fernet`)
- **v2**: 通知 (Server 酱 / 钉钉 webhook)
- **v2**: 多图书馆支持 (3F + 4F 切换)
- **v2**: 任务模板 (周一三五 + 周二四 不同配置)
- **v3**: 真机蓝牙/位置代理 (Android + ADB-WIFI)
- **v3**: 自动按"账号 ↔ 时段 ↔ 座位"做最优编排的 ILP 求解 (用户明确目前不想要)
