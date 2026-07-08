# 学习通 (Chaoxing) 图书馆座位预约 API 调研笔记

> **撰写时间**: 2026-07-08
> **作者**: ZCode + 用户 178XXXX3792 / 151XXXX0087
> **目标图书馆**: id=11692, 2号楼图书馆 3F 3楼备考自习室 (108 座位, 30min 单元, 2h 单段)
> **本研究基于实战测试**，不依赖官方文档（官方不公开）。

## 一、调试方法论

### 1.1 推荐调试路径

```
1. MCP 浏览器开真实窗口到目标 URL
2. 注册 page.on("request"/"response") 拦截所有 /data/apps/seat/* 请求
3. 触发用户操作（点击 30 分钟格子、点击 "开始使用"）
4. 读出每个请求的 URL + body + response
5. 用 httpx 在脚本里重放这些接口
6. 直到某一步弹验证码 → 改用 Playwright headless 驱动真实 UI
7. 拦截 /submit 请求的 body 拿到 enc，然后在 in-browser 完成整个 submit
```

### 1.2 在浏览器 DevTools Console 抓所有 seat API 的 JS 注入

```javascript
const seen = [];
const orig = window.fetch;
window.fetch = async (...args) => {
  const [url, opts] = args;
  if (url.includes('/data/apps/seat/')) {
    seen.push({ url, body: opts?.body, ts: Date.now() });
  }
  const r = await orig(...args);
  if (url.includes('/data/apps/seat/')) {
    const last = seen[seen.length - 1];
    last.status = r.status;
    last.response = await r.clone().text();
    console.log('SEAT API', last);
  }
  return r;
};
```

跑用户操作后 console 会打印所有 `/data/apps/seat/*` 请求和响应。

## 二、已知的 API 端点

### 2.1 房间元数据

| 端点 | 方法 | 用途 | 实战 |
|---|---|---|---|
| `/data/apps/seat/room/info` | POST | 房间配置 + `seatIntervalMap` | ✅ 拿到 108 座 / 2h 单段 / 30min 单元 |
| `/data/apps/seat/config` | GET | 全局 seat config | 仅浏览器使用 |
| `/data/apps/seat/curusedshow` | GET | 当前用户提示 | 仅 UI |
| `/data/apps/seat/person/role` | GET | 用户角色 | roleId=3 |

### 2.2 房间/座位列表

| 端点 | 方法 | 用途 | 实战 |
|---|---|---|---|
| `/data/apps/seat/room/list?time=&cpage=1&pageSize=100&firstLevelName=&secondLevelName=&thirdLevelName=&day=YYYY-MM-DD&deptIdEnc=` | GET | 列出所有可用自习室 | ❌ 需要 `deptIdEnc` 和 `firstLevelName` 才有数据 |
| `/data/apps/seat/seatInfo` | - | 推测 108 座位详情 | 404 |
| `/data/apps/seat/list` | - | 推测某 room 座位列表 | 404 |
| ❓ 真实 floor layout 端点 | - | SVG 渲染的 108 座位 | **未找到公开 API** |

### 2.3 预约操作

| 端点 | 方法 | 用途 | 实战 |
|---|---|---|---|
| `/data/apps/seat/submit` | POST | 提交预约 | ✅ 抢到 id=188046390 |
| `/data/apps/seat/reserve/info` | GET | 查某座位 active reservation | ✅ 拿到 id/uid/start/end/status |
| `/data/apps/seat/sign` | POST | 签到 | ✅ `{success:true}` |
| `/data/apps/seat/leave` | POST | 签退 | ✅ `{success:true}` (status→3) |
| `/data/apps/seat/cancel` | POST | 取消（仅本人） | ✅ `{success:true}` |

### 2.4 风控/前置

| 端点 | 方法 | 用途 | 实战 |
|---|---|---|---|
| `/data/apps/seat/risk/check/config` | GET | 风险评估预检 | 200 (低风险) |
| cstaticdun.126.net/load.min.js | GET | 网易易盾 JS | 加载 (风控 SDK) |

### 2.5 登录

| 端点 | 方法 | 用途 | 实战 |
|---|---|---|---|
| `passport2.chaoxing.com/login?newversion=true&refer=...` | GET | 登录页 | ✅ |
| `passport2.chaoxing.com/fanyalogin` | POST | 真实登录（需 AES 加密） | ❌ 纯 HTTP 不可行 |

## 三、关键数据结构

### 3.1 `get_room_info` 返回

```json
{
  "success": true,
  "data": {
    "seatConfig": {
      "reserveDuration": 2.0,        // 单段最长 2 小时
      "timeUnit": 30,                 // 30 分钟为单位
      "preSignDuration": 20,          // 提前 20min 可签到
      "signDuration": 20,             // 签到后 20min 内必须到
      "leaveDuration": 20,            // 提前 20min 可签退
      "reserveBeforeDay": 1,          // 提前 1 天可预约
      "reserveBeforeTime": "14:00",   // 14:00 后可预约明天的
      "dinnerStartTime": "17:30",
      "dinnerEndTime": "19:00",
      "openAutoRemoveBlackList": 0
    },
    "seatRoom": {
      "id": 11692,
      "firstLevelName": "2号楼图书馆",
      "secondLevelName": "3F",
      "thirdLevelName": "3楼备考自习室",
      "capacity": 108
    },
    "seatIntervalMap": {},            // ❌ 大多时候空（需 day+start+end 才返回）
    "seatLabels": [],                  // ❌ 大多时候空
    "beforeOpenTimeStamp": 1783404000000
  }
}
```

### 3.2 `/submit` 表单字段

```
roomId=11692
day=2026-07-08
startTime=21:00
endTime=22:00
seatNum=084                       # 3 位补零
captcha=                          # 通常空
type=1
verifyData=1
wyToken=                          # 通常空
enc=<32位hex>                     # ⚠ 一次性，与浏览器 session 绑定
```

提交后响应：
```json
{
  "success": true,
  "data": {
    "seatReserve": {
      "id": 188046690,
      "uid": 314500212,
      "roomId": 11692,
      "seatNum": "084",
      "startTime": 1783515600000,
      "endTime": 1783519200000,
      "duration": "1.0",
      "status": 0,
      ...
    }
  }
}
```

### 3.3 `/reserve/info` 返回

```json
{
  "success": true,
  "data": {
    "seatReserve": {
      "id": 188046690,             // 预约 ID
      "uid": 314500212,            // 预约人 _uid
      "roomId": 11692,
      "seatNum": "084",
      "startTime": 1783515600000,  // ms 时间戳
      "endTime": 1783519200000,
      "duration": "1.0",           // 小时
      "status": 0,                 // 0=待签到 1=使用中 3=已签退
      "deptId": 2096,
      "today": "2026-07-08"
    }
  }
}
```

### 3.4 `sign/leave/cancel` 通用响应

成功：
```json
{ "success": true, "data": null, "msg": null }
```

失败：
```json
{ "success": false, "msg": "您在页面停留过久..." }  // enc 失效 (代码:303)
{ "success": false, "msg": "验证码错误" }
```

## 四、关键 Cookies (抓包得到的 17 个)

| Cookie | 域 | HttpOnly | 说明 |
|---|---|---|---|
| `_uid` | .chaoxing.com | ❌ | 用户 ID |
| `UID` | .chaoxing.com | ❌ | 同 _uid |
| `vc3` | .chaoxing.com | ✅ | **核心登录凭证**，纯 HTTP 抓不到 |
| `JSESSIONID` | office.chaoxing.com | ✅ | 服务端会话 |
| `p_auth_token` | .chaoxing.com | ✅ | JWT token |
| `_d` | .chaoxing.com | ❌ | 时间戳 |
| `oa_uid` | office.chaoxing.com | ❌ | 办公账号 UID |
| `oa_name` | office.chaoxing.com | ❌ | URL-encoded 姓名 |
| `oa_enc` | office.chaoxing.com | ❌ | 加密 |
| `oa_deptid` | office.chaoxing.com | ❌ | 部门 ID |
| `fid` | .chaoxing.com | ❌ | 部门 ID |
| `uf` | .chaoxing.com | ❌ | 加密串（很长） |
| `cx_p_token` | .chaoxing.com | ❌ | 通行证 |
| `DSSTASH_LOG` | .chaoxing.com | ❌ | 日志 ID |
| `xxtenc` | .chaoxing.com | ❌ | 旧学习通 token |
| `route` | passport2-api.chaoxing.com | ❌ | 路由 |
| `retainlogin` | passport2.chaoxing.com | ❌ | 保留登录 |

**抓 HttpOnly cookie 必备**：用 Playwright CDP `Network.getAllCookies`，document.cookie 拿不到。

## 五、风控层级

```
Layer 1: IP/UA 信任度          → 决定基础白名单
Layer 2: 浏览器指纹            → 决定是否要拼图/滑块
Layer 3: 行为模式（频率/路径） → 决定是否被弹 number_validator
Layer 4: enc 一次性 token      → 即便通过 1-3，/submit 还要 enc 有效
```

## 六、关键发现

### 6.1 `fanyalogin` 不再支持纯 HTTP 登录

老代码：把 phone + password 直接 POST 就行
新代码（v1.1+）：需要 `uname` 和 `password` 字段经过 **AES 加密**（key = `u2oh6Vu^HWe4_AES`），外加 `t`、`forbidotherlogin`、`validate`、`doubleFactorLogin`、`independentId`、`independentNameId` 几个字段，这些都由页面 JS 计算产生。

**解决**：用 Playwright headless 跑真实登录页，然后从 context 抓 cookies 灌进 httpx。

### 6.2 `enc` 一次性、不能跨会话复用

`enc` 在 /submit 请求里是个 32 字符的 hex 哈希（例：`007eefc29015f08020bb57ca9a156ac0`），**和当前浏览器 session 的 risk-control 状态绑定**。如果用脚本 POST 时把这个 enc 重用，服务器返回：
```
{"success": false, "msg": "您在页面停留过久，本次操作安全验证已超时。请刷新后再提交预约(代码:303)"}
```

**唯一可靠路径**：headless 浏览器自己点 "开始使用"，让 enc 在同一会话里生成 + 提交 + 拿到响应。

### 6.3 headless 不一定会被弹验证码

**实测**：在我们这台机器 + 测试账号 `151XXXX0087` 的组合下，headless 登录 + 抢座 + 签到 + 签退**全部一次过**，没弹验证码。

但**风控会"沉睡"**：
- 短时间内连续 login（< 30 秒）→ 第二次会被弹 number validator
- 第一次 headless submit 成功后，30 秒内再次 submit → 容易被拦

**应对**：scheduler 已加 30 秒 sync_jobs 热加载，但 submit 间隔需要靠 stagger_seconds 错开（默认 0-3s 不够，最好调到 10-30s）。

### 6.4 `/room/info` 的 `seatIntervalMap` 经常返回空

只有当调用时同时传 `day` + `startTime` + `endTime` 才能拿到座位占用映射。**这在调试时是大坑**——单纯调 `/room/info` 看不到座位状态。

### 6.5 `/room/list` 需要 `deptIdEnc` 才能返回房间

不传 `deptIdEnc` → 返回 `seatRoomList: []`。这个 `deptIdEnc` 是从登录后的 `passport2.chaoxing.com` 写进 cookie 的。**我们没找到通过纯 HTTP 注入这个值的办法**。

### 6.6 没有公开的 108 座位 floor layout API

座位真实布局（SVG）是浏览器 JS 在前端拼出来的，没有对应的 JSON 端点。所以 seatbot 用 `±10 reserve/info` 扫描的方法只能看到**目标座位周围 21 个座位**的状态，无法还原 108 座位的真实排列。

## 七、盲点（不知道的部分）

1. **真实 108 座位的 floor layout API** —— SVG 渲染的 source endpoint 是哪一个？没找到
2. **deptIdEnc 是怎么算的** —— 看起来是从 `passport2.chaoxing.com` 登录后服务端写入的
3. **多人抢同一座位的竞争协议** —— 同时 submit 的话是 FIFO 还是 last-write-wins？没测
4. **易盾白名单的 TTL** —— 几天？几小时？每次登录会重置吗？未知

## 八、生产部署注意事项

1. **不要保持 Chrome 窗口常开**：seatbot 自己 headless 就够
2. **不要把 server 部署到远程 IP**：远程 IP 没在易盾白名单，会弹验证码
3. **首次部署到新 IP 时需要人工过一次验证码**（用你电脑浏览器登录 + 过滑块）
4. **同一台机器 + 多个 guard 账号**：通过 stagger_seconds 错开（建议 10-30s），不要并发 submit
5. **database 是单一事实来源**：Web 面板是主要管理方式
6. **不要重复 commit 数据清理**：测试时 cancel 完 reservation 即可，不用再清理 DB

## 九、相关 commit 历史

| commit | 说明 |
|---|---|
| `039be9b` | fix(seats): retry login up to 3 times with back-off in /api/seats |
| `47cbdd2` | feat(seats): rebuild /seats page as 21-seat grid around target_seat_num |
| `8b52352` | fix(scheduler): use tz-aware CST datetimes in tick + relay |
| `9cf2f61` | feat: in-browser reserve + sign + leave, end-to-end verified |
| `9efc12b` | feat: DB-driven accounts + browser-based login |
| `38ceaf0` | feat: seat-guard mode (single target_seat + multiple accounts) |
