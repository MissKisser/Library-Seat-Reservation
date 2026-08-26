# Chaoxing (学习通) Library Seat Reservation API — 实测清单

> **作者**: seatbot
> **实测日期**: 2026-07-09 (Web: Playwright + Chromium; Mobile: Android 15 / Redmi K70, debug build, 学习通 v?.?.?)
> **本文档权威性**: 接口字段 / endpoint / 返回结构均**实测抓包确认**;灰色时段含义基于**手机端 UI 视觉验证** AND 真实 API 返回(见 §4.4)。

---

## 0. 重大发现 (2026-07-09 11:30 CST) — `/data/apps/seat/getusedtimes` 真的能用!

> 这一节取代之前 "getusedtimes 返回空数组" 的错误结论。

| 字段 | 真相 |
|---|---|
| 接口 | `POST /data/apps/seat/getusedtimes` |
| 参数 | `roomId=11692&seatNum=XXX&day=YYYY-MM-DD&fidEnc=24680a1d287b60c7` |
| **fidEnc 必须是手机端** | `24680a1d287b60c7` (来自 `debug_104.html` 中嵌入的 `<script>var fidEnc='24680a1d287b60c7'`)<br>用 PC web fidEnc `85a5894481db5d7a` 会返回 `data: []`(即使座位真被占了) |
| 返回 | `{ "data": [[start_ms, end_ms], ...], "success": true }` |
| 鉴权 | 任一已登录的 .chaoxing.com cookie 即可(用熊金涛 _uid=314500121 测试通过) |
| **能看到他人预约** | ✅ **是** — `fidEnc` 是房间级别的,**不是**用户级别的 |

**实测 2026-07-09 验证**(uid=314500121, room=11692):

| seat | fidEnc | 返回 |
|---|---|---|
| 104 | `24680a1d287b60c7` (mobile) | `[("19:30","21:30")]` ← 自己预约的 |
| 105 | `24680a1d287b60c7` (mobile) | `[("15:30","17:30"), ("19:00","21:00")]` ← **两个都是别人** |
| 104 | `85a5894481db5d7a` (PC web) | `[]` ← **这就是 bug** |
| 105 | `85a5894481db5d7a` (PC web) | `[]` ← **这就是 bug** |
| 110 | `24680a1d287b60c7` (mobile) | `[]` ← 空座位正确 |

**用法**: seatbot 已封装为 `ChaoxingClient.get_used_times(room_id, seat_num, day)`,
见 `seatbot/client.py`。

**仍待解的边界**: 手机 UI 上看到的 105 还有 08:00-10:00 / 18:00-19:00 是灰色,
但 `getusedtimes` 只返回 15:30-17:30 和 19:00-21:00。08:00-10:00 那条同时在
`/reserve/info` 的 `seatReserve` 里(`uid=346000232`, `status=1` 使用中),
而 18:00-19:00 没在两个接口里出现 — **可能不是预约**(可能是"暂离""已签到"
或别的状态),需要继续排查。

---

## 1. 综述

学习通的座位预约有两套 UI,**字段语义不完全一致**:

| 客户端 | URL 前缀 | 入口 | fidEnc 角色 |
|---|---|---|---|
| Web (PC 浏览器) | `office.chaoxing.com/front/apps/seat/...` | 直接打开 web 版 | `85a5894481db5d7a` (PC web) |
| Web (在移动浏览器内) | `office.chaoxing.com/front/third/apps/seat/...` | 嵌入到 App 内的浏览器 | `24680a1d287b60c7` (mobile) |
| 学习通 Android App | 同 web,但路由通过 `WebAppViewerActivity` 显示 | 主页内点"座位预约"图标 | `24680a1d287b60c7` (mobile) |

**已知 fidEnc**:
- `85a5894481db5d7a` — PC web 端(查询接口用这个 fidEnc 会返回空!)
- `24680a1d287b60c7` — **移动端** (查询 + 预约接口都用这个)

---

## 2. 房间 / 自习室

| 房间 ID | 名称 | 容量 |
|---|---|---|
| **11692** | 2号楼图书馆-3F-3楼备考自习室 | 108 |
| **11693** | 2号楼图书馆-4F-4楼备考自习室 | 108 |

### 2.1 `POST /data/apps/seat/room/info`

**用法**: 拿到一个自习室的 `seatConfig`(全局配置:开放时间、timeUnit、reserveDuration 等)。

```http
POST /data/apps/seat/room/info HTTP/1.1
Content-Type: application/x-www-form-urlencoded
x-requested-with: XMLHttpRequest

id=11692
```

**返回** (节选关键字段):
```json
{
  "data": {
    "seatRoom": {
      "id": 11692, "capacity": 108,
      "firstLevelName": "2号楼图书馆",
      "secondLevelName": "3F",
      "thirdLevelName": "3楼备考自习室"
    },
    "seatConfig": {
      "id": 9051,
      "deptId": 2096,
      "timeType": 0,
      "timeUnit": 30,                 // 30min 一格
      "reserveDuration": 2.0,         // 单次最长 2 小时
      "reserveBeforeDay": 1,
      "reserveBeforeTime": "14:00",   // 每天 14:00 后可预约第二天
      "maxCancelPerDay": 3,
      "maxCancelPerWeek": 4,
      "commonTimeConfig": {
        "monStartTime": "08:00", "monEndTime": "22:00",
        // ... 每周几的开闭时间
      },
      "seatIntervalMap": {}           // 注意:此接口下这个字段是空对象!
    }
  },
  "success": true
}
```

**⚠️ 关键发现**: `seatIntervalMap` 在 `room/info` 接口里**永远是空对象 `{}`**。seatbot v1 注释里提到的"可以从这里查座位全天占用"是**错的**。

### 2.2 `GET /data/apps/seat/room/list` (拖加载列表)

**用法**: 拿到自习室**元数据列表**(房间 id、容量、开放时间)。**不带座位细节**。

```http
GET /data/apps/seat/room/list?time=&cpage=1&pageSize=100&firstLevelName=&secondLevelName=&thirdLevelName=&day=2026-07-09&deptIdEnc=85a5894481db5d7a
```

**返回**:
```json
{
  "data": {
    "totalRow": 2, "totalPage": 1,
    "seatRoomList": [
      { "id": 11692, "capacity": 108, "firstLevelName": "...", "secondLevelName": "3F", "thirdLevelName": "...", ... },
      { "id": 11693, ... }
    ]
  },
  "success": true
}
```

**注意**:
- PC 端 `/front/apps/seat/list` 页面调用此接口时**必须先选自习室**(firstLevelName 等字段非空),否则返回空。
- 手机端 `/front/third/apps/seat/list` 默认就能列出 3F/4F。

---

## 3. 个人预约列表

### 3.1 `GET /data/apps/seat/index?fidEnc=...`

**用法**: 当前登录账号**自己**的所有"当前预约"(进行中 + 未开始 + 最近已结束/已取消)。

```http
GET /data/apps/seat/index?fidEnc=85a5894481db5d7a&r=35.4
```

**返回** (节选):
```json
{
  "data": {
    "seatConfig": { /* 同 room/info */ },
    "curReserves": [
      {
        "id": 188063339, "uid": 314500121, "uname": "熊金涛",
        "roomId": 11692, "seatNum": "083",
        "today": "2026-07-09",
        "startTime": 1783557000000, "endTime": 1783564200000,
        "duration": "2.0", "status": 1       // status: 1=进行中, 0=待履约
      },
      {
        "id": 188056546, "uid": 314500121, "uname": "熊金涛",
        "seatNum": "104",
        "today": "2026-07-09",
        "startTime": 1783596600000, "endTime": 1783603800000,
        "duration": "2.0", "status": 0       // 待履约(未开始)
      }
    ],
    "nearReserves": [
      // 最近已结束/已取消/已违约
    ]
  },
  "success": true
}
```

**字段含义**:
- `curReserves`: 当前本人所有**未结束**的预约(含进行中 + 未开始)
- `nearReserves`: 最近已完成/已取消的预约(状态码见 §7)
- `uname`: 真实姓名(只有自己的 uid 才会带)
- `status`: 0=待履约, 1=使用中, ...

### 3.2 `GET /data/apps/seat/reservelist?indexId=0&pageSize=10&type=-1&fidEnc=...`

**用法**: 当前账号的"预约记录"页(全部历史预约,分页)。

```http
GET /data/apps/seat/reservelist?indexId=0&pageSize=10&type=-1&fidEnc=85a5894481db5d7a&showQrCode=1
```

- `type`: -1=全部, 0=待履约, 1=已履约, 2=已取消, 7=违约
- `indexId`: 分页游标(0=第一页)
- 返回 `data.reserveList[]` 字段同 §3.1,但不带 `uname`

---

## 4. 单个座位详情

### 4.1 `POST /data/apps/seat/reserve/info` ⭐ (核心接口)

**用法**: 拿到一个座位的**当前 active 预约**详情。这是订座位前必调。

```http
POST /data/apps/seat/reserve/info HTTP/1.1
Content-Type: application/x-www-form-urlencoded
x-requested-with: XMLHttpRequest

id=11692&seatNum=105
```

**返回** (节选):
```json
{
  "data": {
    "seatConfig": { /* 同 room/info */ },
    "scannerBluetooth": false,
    "beforeOpenTimeStamp": 1783490400000,
    "seatReserve": {
      "id": 188048107,                  // reserve_id
      "uid": 346000232,                 // 别人预约就只看到 uid(没有 uname)
      "roomId": 11692, "seatNum": "105",
      "today": "2026-07-09",
      "startTime": 1783555200000,       // 08:00
      "endTime": 1783562400000,         // 10:00
      "duration": "2.0",
      "status": 1                       // 1=使用中
    }
  },
  "success": true
}
```

**⚠️ 关键字段语义**:

| 字段 | 含义 | 备注 |
|---|---|---|
| `seatReserve == null` | 该座位当前**完全空闲** | 可预约 |
| `seatReserve.startTime` 和 `endTime` | 该座位**最近一条 active 预约**的时段 | 只能看到一条,**不是全天所有预约** |
| `seatReserve.uid` | 谁的预约(别人的话没有 `uname`) | seatbot v1 用这个判断冲突 |
| `seatReserve.status` | 1=使用中, 0=待履约 | |

**🪤 seatbot v1 / v2 bug**: seatbot 的 `get_active_reservation` 把这个接口的 `seatReserve` 当成"该座位全天占用"来用 — **这是错的**。这是**当前最近的一条 active 预约**,无法查出这个座位一天内的所有预约。

**seatbot v1 / v2 真实依赖**: 通过 `submit` POST 时让学习通自己判断冲突;submit 失败 = 已被人占。

### 4.2 PC web 跳 `code` 后的页面

点击某个座位,PC web 跳到:
- `/front/third/apps/seat/codemyselfuse?seatNum=105&id=11692&fidEnc=...` (有人占的座位)
- `/front/third/apps/seat/codenobody?seatNum=105&id=11692&fidEnc=...` (空闲的座位)

这俩页面调同一个 `POST /reserve/info`。

### 4.3 ⭐ 手机端的"全天 30min 切片"是怎么来的 — 已解决!

**手机端点座位后**,弹窗显示该座位**一天 30min 切片的占用情况**(灰色 = 占用,白色 = 可选)。
数据来源就是 **`POST /data/apps/seat/getusedtimes`**(详见 §0 / §4.4)。

**注意**:
- 灰色时段**大多数**对应 `getusedtimes` 返回的时间戳对(明确预约)
- 极个别灰色时段可能是 "正在使用" 的临时状态(`status=1` 但 `endTime > now`,
  而且用户没有 release 到 `getusedtimes` 池里)— 实际不需要精确区分,反正都是"不可订"

**视觉验证**(2026-07-09,熊金涛账号,105 号座位):

| 时段 | UI 颜色 | 接口证据 |
|---|---|---|
| 08:00-10:00 | 灰色 | `reserve/info` `seatReserve` (uid=346000232, status=1 使用中) — **未在 getusedtimes 返回里** |
| 10:00-15:30 | 白色 | (空) |
| 15:30-17:30 | 灰色 | **`getusedtimes` 返回 [15:30-17:30]** ✅ |
| 17:30-18:00 | 白色 | (空) |
| 18:00-19:00 | 灰色 | **未在 getusedtimes 返回里** — 可能不是真实预约 |
| 19:00-21:00 | 灰色 | **`getusedtimes` 返回 [19:00-21:00]** ✅ |
| 21:00-22:00 | 白色 | (空) |

→ 现在可以**编程判断**某座位全天哪些时段被占用,不再需要看 UI。

### 4.4 `POST /data/apps/seat/getusedtimes` ⭐ (全天占用查询接口)

**用法**: 拿到某个座位在某个日期**所有** active 预约的 [start_ms, end_ms] 列表。
**这是 §0 重大发现对应的主接口**。手机端"全天 30min 切片"灰白渲染就是读这个。

```http
POST /data/apps/seat/getusedtimes HTTP/1.1
Content-Type: application/x-www-form-urlencoded
Referer: https://office.chaoxing.com/front/apps/seat/code?id=11692&seatNum=104

roomId=11692&seatNum=104&day=2026-07-09&fidEnc=24680a1d287b60c7
```

**关键参数 `fidEnc`**:
- 必须是 **`24680a1d287b60c7`** (移动端)
- 用 PC web fidEnc `85a5894481db5d7a` 会返回 `{"data":[],"success":true}` —
  **这是之前整个调研没找到这个接口的根本原因**

**返回**:
```json
{
  "data": [
    [1783596600000, 1783603800000]    // 19:30-21:30 (CST)
  ],
  "success": true
}
```

**实测结果**(uid=314500121, room=11692, day=2026-07-09):

| seat | data |
|---|---|
| 104 | `[[1783596600000, 1783603800000]]` = 19:30-21:30 (自己) |
| 105 | `[[1783582200000, 1783589400000], [1783594800000, 1783602000000]]` = 15:30-17:30, 19:00-21:00 (都是别人) |
| 110 | `[]` |

**seatbot 用法**:
```python
from seatbot.client import ChaoxingClient
c = ChaoxingClient()
intervals = await c.get_used_times(11692, "104", "2026-07-09")
# → [("19:30", "21:30")]
```

**已知边界**:
- 使用中 (`status=1`) 的预约**可能**不出现在返回里 (例如 105 08:00-10:00 在
  `reserve/info` 出现但 `getusedtimes` 不返回) — 实现上不要把 "不在返回里"
  等同于 "可订",还是用 `submit` 让 server 判冲突为准
- 已经过期/已取消的预约会从返回里消失
- 鉴权使用普通登录 cookie (任一 `.chaoxing.com` 已登录 cookie 均可)

---

## 5. 预约 / 取消 / 签到 / 暂离

### 5.1 `POST /data/apps/seat/submit` — 提交预约

**前置**: 必须先在浏览器中通过 `code` 页面拿到 `enc` token 和 `wyToken`(防机器人),30min 内有效。

```http
POST /data/apps/seat/submit
Content-Type: application/x-www-form-urlencoded

roomId=11692&day=2026-07-09&startTime=08:30&endTime=10:30
&seatNum=104&captcha=&type=1&verifyData=1
&wyToken=...&enc=...
```

返回示例:
```json
{ "success": true, "data": "188056546", "msg": null }   // data 是 reserve_id
```

### 5.2 `POST /data/apps/seat/sign` — 签到
```http
POST /data/apps/seat/sign
id=188063339
```
返回 `{"success": true, "msg": "签到成功"}`

### 5.3 `POST /data/apps/seat/leave` — **暂离**（⚠️ 不是签退）

- 语义：暂时离座（status → 3 暂离中），**要求剩余时长 ≥ 暂离时长(20min)**
- 剩余不足 20min / 时段已结束时返回 `{"success":false,"msg":"剩余时长小于暂离时长，无法暂离"}`（2026-08-25 实测）

### 5.3b `POST /data/apps/seat/signback` — **签退 / 退座**（★2026-08-25 发现并实测成功）

```http
POST /data/apps/seat/signback
id=<reserve_id>
```
- 返回 `{"success": true}`，预约立即结束、移出 active —— 真正的"结束使用"，时段进行中任意时刻可用
- 来源：[MGJ520/XXT_Library_Web](https://github.com/MGJ520/XXT_Library_Web) `utils/Xxt_WebApi.py`（"退座"）
- 注意：老版本系统需将 URL 中的 `seat` 改为 `seatengine`（同项目注释）

### 5.4 `POST /data/apps/seat/cancel` — 取消预约
```http
POST /data/apps/seat/cancel
id=188056546
```

### 5.5 `POST /data/apps/seat/submit` **必须通过 Playwright 浏览器**

- `enc` 是浏览器会话级的风控 token,**无法跨 cookie 复用**
- `/data/apps/seat/risk/check/config` 配置风险控制参数
- seatbot 已经实现了 `submit_in_browser()` (Playwright headless Chromium)

---

## 6. 辅助接口

| 接口 | 用途 |
|---|---|
| `GET /data/apps/seat/entrance/config?appType=0&fidEnc=...` | App 入口配置 |
| `GET /data/apps/seat/config?fidEnc=...` | (mobile 端) 房间全局配置 |
| `GET /data/apps/seat/person/role?fidEnc=...` | 用户角色(管理员?) |
| `GET /data/apps/seat/curusedshow?fidEnc=...` | 当前账号预约的快捷摘要 |
| `POST /data/apps/seat/risk/check/config` | 风控配置 |
| `POST /api/data/apps/reserve/wx/config` | 微信相关配置 |

---

## 7. status 状态码

| status | 含义 |
|---|---|
| 0 | 待履约(已预约但还没开始) |
| 1 | 使用中(已签到) |
| 2 | 已履约(正常使用完成) |
| 3 | 暂离中（leave 所致；2026-08-25 修正，旧文档误标"已签退"） |
| 5 | 被监督中（来源: XXT_Library_Web 状态表） |
| 7 | 已取消 / 违约 |
| 8 | 已结束（2026-08-25 实测：时段走完后服务端自动归类的终态，非违约） |

---

## 8. seatbot 架构更新

### 8.1 v1/v2 错误假设

| 错误假设 | 真相 |
|---|---|
| `room/info` 的 `seatIntervalMap` 能查全天占用 | `seatIntervalMap` 永远是 `{}` |
| `get_active_reservation` 返回的就是该座位**全部**占用 | 只返回**最近 1 条 active 预约** |
| ~~`getusedtimes` 能拿到当天所有预约~~ (结论错了) | `getusedtimes` **能**拿到当天所有预约,**前提是 fidEnc 用手机端那个** |
| 灰名单字是后端字段 | 灰色基本等于 `getusedtimes` 返回的预约 + 部分 `status=1` 在用中的 |

### 8.2 查"别人占用某个座位"的可靠方法 (按优先级)

1. **`POST /data/apps/seat/getusedtimes` + mobile fidEnc** — 拿到所有预约区间 ✅
2. **`POST /data/apps/seat/reserve/info`** — 拿到最近 1 条 active 预约 (次优)
3. **手机端 UI 视觉确认** (灰色 = 占用) (最次,自动化困难)
4. **`submit_in_browser`** — 实际下单让 server 判冲突 (兜底)

### 8.3 怎么调度预约避免冲突

- **客户端预检 (新增)**: scheduler 在下发 task 前调 `get_used_times`,剔除与已占用时段冲突的候选
- **server 兜底 (保留)**: 即使预检通过,`submit` 仍可能失败 (race condition),失败 → 标记 task failed
- **本地 user_reserved overlay**: 用户手动预约的时段,scheduler 不重复订

### 8.4 已知 fidEnc 角色表

| fidEnc | 角色 | 适用接口 |
|---|---|---|
| `85a5894481db5d7a` | 熊金涛的 org (PC web 端) | `/index`, `/reservelist`, `/room/list` (返回房间元数据 OK) |
| `24680a1d287b60c7` | 熊金涛的 org (移动端) | **`/getusedtimes` 必须用这个**;`/submit`, `/reserve/info` 也建议用这个 |

---

## 9. 7-09 当天实测 (2026-07-09 09:17 CST)

### 9.1 104 号座位 — 手机端 UI 视觉确认

| 时段 | 状态 | 来源 |
|---|---|---|
| 08:00-08:30 | 灰色 (占用) | (熊金涛 083 现在使用中) |
| 08:30-09:00 | 灰色 (占用) | (同上) |
| 09:00-19:30 | 全部白色 (可选) | |
| **19:30-20:00** | **灰色** | **熊金涛本人预约 (curReserves rid=188056546, status=0 待履约)** |
| **20:00-20:30** | **灰色** | 同上 |
| **20:30-21:00** | **灰色** | 同上 |
| **21:00-21:30** | **灰色** | 同上 |
| 21:30-22:00 | 白色 (可选) | |

### 9.2 105 号座位 — 实测数据 (接口验证,不再是 UI 视觉)

| 时段 | UI 颜色 | `getusedtimes` 返回 | `reserve/info` 返回 | 结论 |
|---|---|---|---|---|
| 08:00-10:00 | 灰色 (4 格) | ❌ 不在 | ✅ `seatReserve` (uid=346000232, status=1 使用中) | 别人使用中,不可订 |
| 10:00-15:30 | 白色 | ❌ 不在 | ❌ 不在 | 可订 |
| **15:30-17:30** | **灰色 (4 格)** | ✅ `[(15:30, 17:30)]` | ❌ 不在 | **别人预约**,不可订 |
| 17:30-18:00 | 白色 | ❌ 不在 | ❌ 不在 | 可订 |
| 18:00-19:00 | 灰色 (2 格) | ❌ 不在 | ❌ 不在 | **UI 灰但接口无** — 可能不是真预约,需进一步确认 |
| **19:00-21:00** | **灰色 (4 格)** | ✅ `[(19:00, 21:00)]` | ❌ 不在 | **别人预约**,不可订 |
| 21:00-22:00 | 白色 | ❌ 不在 | ❌ 不在 | 可订 |

### 9.3 调度器影响

scheduler 14:00 后打算订 7-09:

| task | seat | 时段 | 期望结果 | 依据 |
|---|---|---|---|---|
| wangh | 105 | 08:30-10:30 | **冲突** | getusedtimes/reserve/info: 08:00-10:00 已被占 |
| wangh | 104 | 08:30-10:30 | 成功 | 104 08:00-19:30 全空 |
| wangh | 104 | 15:00-17:00 | 成功 | 同上 |
| xiongjt | 104 | 08:30-10:30 | 成功 | 同上 |
| xiongjt | 104 | 15:00-17:00 | 成功 | 同上 |
| xiongjt | 104 | 19:30-21:30 | **跳过** | user_reserved 已记录 |
| zhaozh | 105 | 10:30-12:30 | 成功 | 105 10:00-15:30 全空 |
| zhaozh | 105 | 19:30-21:30 | **冲突** | getusedtimes: 19:00-21:00 已被占 |
| wangh | 105 | 19:30-21:30 | **冲突** | 同上 |

→ **3 个 task 在 105 晚上时段会失败**(19:30-21:30 与 105 上的 19:00-21:00 冲突)。

**建议**: 修改 scheduler 把 105 晚上时段让出来,只抢 104 + 105 早上。
**实现路径**: 用 `get_used_times(11692, "105", "2026-07-09")` 拿到 [("15:30","17:30"), ("19:00","21:00")],
对候选时段做区间求交,过滤掉冲突的。

---

## 10. 下一步

- [x] ~~继续研究手机端"全天 30min 切片"灰色时段的具体接口~~ → **已解决**: `POST /data/apps/seat/getusedtimes` + mobile fidEnc
- [ ] 把 `submit_in_browser` 的成功/失败结果正确映射回 task status
- [ ] 在 scheduler 的 coverage/expansion 阶段调 `get_used_times`,过滤掉他人已占时段
- [ ] 调研 105 的 18:00-19:00 灰色时段在 `getusedtimes` 和 `reserve/info` 都不返回的原因
- [ ] 给 zhaozh / wangh 配置 `bound_seats` 排除 105 晚上时段(临时方案:手动;长期方案:自动从 getusedtimes 推算)

---

## 11. ⭐ 如何获取 fidEnc (换图书馆/换学校时必读)

> 这是所有坑的根源。未来任何人扩展支持其他图书馆，先读这一节。

### 11.1 背景：fidEnc 是什么，为什么不在 APK 里

`fidEnc` 本质是 `deptIdEnc` — 学习通的**部门/机构加密标识**，由**服务端按用户的学校动态渲染**。

**关键事实**：学习通 App 是一个 WebView 壳（`WebAppViewerActivity`），它加载的是
`office.chaoxing.com` 的远程 HTML 页面。服务端根据登录用户所在学校/部门，
把 `fidEnc` 注入到 HTML 的内联 `<script>var fidEnc='XXXX'</script>` 里，
同时也作为 URL 参数（`deptIdEnc=XXXX`）出现在页面跳转中。
**APK 里没有这个值**，只有通过抓取 WebView 发出的 HTTP 请求才能拿到。

同一所学校、同一个自习室，**PC web 版和移动端 App 版的 `deptIdEnc` 不同**，
这导致用 PC web fidEnc 调 `/getusedtimes` 会返回空数组，产生"接口无法使用"的假象。

| 前端入口 | 路由前缀 | fidEnc (本项目实测) |
|---|---|---|
| PC 浏览器打开 office.chaoxing.com | `/front/apps/seat/...` | `85a5894481db5d7a` |
| 手机 App 内（WebView 加载远程HTML） | `/front/third/apps/seat/...` 或 `/front/apps/seat/select?...` | `24680a1d287b60c7` |

**`/getusedtimes` 必须使用移动端 fidEnc** — 用 PC web fidEnc 服务端直接返回 `data: []`，不报错。

### 11.2 方法一：Chrome DevTools 远程调试（最简单，无需额外工具）

因为学习通本质是 WebView 应用，**Android Chrome DevTools 可以直接检查它的 HTTP 请求**，
就像用 Chrome 开发者工具调试网页一样，无需 mitmproxy 或 root。

**步骤**：

1. 手机开启 USB 调试，连接 PC
2. 打开学习通，进入目标图书馆的座位预约页面（点到显示座位格子的那一层）
3. PC 端 Chrome 地址栏输入 `chrome://inspect/#devices`，找到学习通的 WebView，点 `inspect`
4. 打开 Network 标签，在手机上点任意座位，观察 POST 请求：

   ```
   POST getusedtimes
   Request Body: roomId=11692&seatNum=104&day=2026-07-09&fidEnc=24680a1d287b60c7
   ```

   或看 Referer 头：

   ```
   https://office.chaoxing.com/front/apps/seat/select?deptIdEnc=24680a1d287b60c7&id=11692&...
   ```

5. `fidEnc` / `deptIdEnc` 字段值即为移动端 fidEnc。

### 11.3 方法二：从 mitmproxy 抓包（已有抓包环境时）

如果已按 `docs/superpowers/specs/2026-07-09-investigation-status.md` 搭好 mitmproxy：

1. 手机打开学习通，进入座位选择页，点任意座位
2. 在 mitmdump 输出里搜索 `getusedtimes` 或 `deptIdEnc`：

   ```
   POST https://office.chaoxing.com/data/apps/seat/getusedtimes
   body: roomId=11692&seatNum=104&day=2026-07-09&fidEnc=24680a1d287b60c7
   ```

3. `fidEnc` 字段值即为移动端 fidEnc。

### 11.4 方法三：从 HTML 源码读取（有 mitmproxy / Chrome inspect 时）

保存手机端座位页的 HTML 源码后搜索（服务端会把 fidEnc 内联进 HTML）：

```html
<script>var fidEnc='XXXXXXXXXXXXXXXX'</script>
```

等同于 URL 里的 `deptIdEnc`，两个值相同。

### 11.5 方法四：API 推算（不依赖 App，纯接口）

`/data/apps/seat/room/info` 响应里有 `seatConfig.deptId`（数字，如 `2096`）。
理论上 fidEnc 是对 deptId 的某种编码，但**编码算法未逆向成功**，目前无法从 deptId 直接推算。
如果将来找到算法，在此处更新。

**目前推荐方法一（Chrome DevTools）— 最快、无需额外工具。**

### 11.6 验证是否正确

拿到 fidEnc 后用以下命令验证（替换 `ROOM_ID`、`SEAT_NUM`、`<cookie_str>`）：

```bash
curl -s -X POST "https://office.chaoxing.com/data/apps/seat/getusedtimes" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Cookie: <cookie_str>" \
  -d "roomId=<ROOM_ID>&seatNum=<SEAT_NUM>&day=$(date +%Y-%m-%d)&fidEnc=<YOUR_FID_ENC>"
```

| 返回 | 含义 |
|---|---|
| `{"data": [[...],[...]], "success": true}` | ✅ fidEnc 正确 |
| `{"data": [], "success": true}` | ⚠️ 可能 fidEnc 错，也可能今天该座位确实没预约 |
| `{"success": false}` 或登录错误 | ❌ cookie 过期，需重新登录 |

用已知今天有预约的座位（如本项目 104/105）做黄金测试：`data` 必须非空。

### 11.7 在 seatbot 中配置

```yaml
# config.yaml
library:
  room_id: 11692
  fid_enc: "24680a1d287b60c7"   # 移动端 fidEnc，获取方式见本节
```

代码层面，`ChaoxingClient.get_used_times()` 的 `fid_enc` 参数优先用调用方传入的值
（`cfg.library.fid_enc`），未传则回退到 `ChaoxingClient.FID_ENC_MOBILE` 硬编码常量。
**如果换了学校，同时更新 `config.yaml` 的 `fid_enc` 和 `client.py` 的 `FID_ENC_MOBILE`。**