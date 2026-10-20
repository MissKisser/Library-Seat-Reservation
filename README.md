# SeatBot — 超星图书馆座位自动预约 (座位保护模式)

> 让**目标座位**在营业时段内始终显示"已有人预约"，阻止他人抢走。
> 多守护账号 + 错时段重叠覆盖 = 全天护城河。

设计与实施细节见本地 `docs/` 目录（依文档规范不入库，总览见 [docs/docs.md](docs/docs.md)）。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .[dev]
playwright install chromium
cp config.example.yaml config.yaml
# 编辑 config.yaml: 填入馆舍参数（room_id / 营业时间）与守护账号手机号密码
python -m seatbot init-db
python -m seatbot run
# 访问 http://localhost:8080
#   - 首页: Gantt 覆盖图 (列=30min 时段, 行=守护账号)
#   - 目标座位: 面板 /targets 添加（数据库为真源）
#   - 守护账号: 面板 /accounts 增删改（新建会真实登录学习通一次）
#   - 绑定矩阵: 面板 /bindings 分配时段并检查覆盖缺口
```

> **配置文件的定位**：`config.yaml` 只提供馆舍参数与凭据，并在首次启动时播种一次；
> 目标座位、守护矩阵、账号增删的**真源是数据库**，一律在 Web 面板完成
> （详见 `AGENTS.md` 第八条）。面板已删除的条目不会被配置文件复活。

## 工作原理

1. **目标座位**：你要保护的座位号，在面板 `/targets` 注册（`config.yaml` 的 `target_seats` 仅作首次种子）
2. **守护账号**（1-N 个）：程序替你抢目标座位的辅助超星账号
3. **时段 slots**：每个账号对每个座位配置若干 `"HH:MM-HH:MM"`，单段 ≤ `max_reserve_hours`（默认 2h）
4. **覆盖**：绑定矩阵的合并应覆盖该座位的期望时段；缺口在 `/bindings` 页与首页横幅显示
5. **每日 14:00 起**：程序批量提交次日全部时段
6. **签到/签退**：时段开始后系统自动签到（服务端要求时段已开始）；时段结束前
   `relay_lead_seconds`（默认 5 分钟）签退并把座位接力给下一段

### 守护矩阵怎么配

矩阵的唯一入口是账号的 `seat_slots` 字段：**键 = 座位号，值 = 该账号负责的时段**。

```yaml
accounts:
  - id: guard_a
    seat_slots:
      "001": ["09:00-11:00", "15:00-17:00"]   # 该账号在 001 守两段
      "002": ["19:00-21:00"]                   # 该账号在 002 守晚段
```

规则要点：

- 面板可切「全局统一」（每天相同时段）/「按天自定义」（周一至周日分别勾选），两种形态数据层等价
- planner 按 `(座位, 时段)` 精确展开，**不做笛卡尔积**；某个账号没绑定的座位不由它守护
- 缺省的天 = 该天不守护
- **单段 ≤ 2h**：期望时段录入层会自动切片成 ≤2h 子段；账号矩阵层与 planner 对超 2h 段
  **直接拒绝**（报错提示），需在录入时显式拆成多段
- 同一账号的时段不得跨座位重叠（超星会直接拒绝："该时间段您已有预约！"）
- 期望时段（每个座位要守护哪些时段）在 `/bindings` 页设置，作为覆盖检查的基准

具体矩阵随时可在面板调整，本文不记录快照；账号余量与一键重排见 `/bindings` 页。

### 若账号本人要自约某段

本人已手动预约的时段由实况同步自动登记进 `user_reserved`，scheduler 生成任务时跳过对应冲突段
（规则见 `docs/admin/user-reserved.md`）。该表无手工入口，由实况同步全自动维护。

## 命令

```bash
python -m seatbot run --config config.yaml
python -m seatbot once --account zhangsan
python -m seatbot login --account zhangsan
python -m seatbot status
python -m seatbot init-db
```

> ⚠️ `run` 会注册调度器作业：每日 14:00 批量提交次日预约、时段内自动签到/签退，
> 启动补跑还可能立即提交次日待预约任务——都是**真实预约**。详见 `AGENTS.md` 第三条。

## 部署

### 本地 (Windows / macOS / Linux)

```bash
git clone <repo>
cd Library-Seat-Reservation
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e .[dev]
playwright install chromium         # enc fallback 用的浏览器
cp config.example.yaml config.yaml
# 编辑 config.yaml: 填入馆舍参数与守护账号手机号密码
python -m seatbot init-db
python -m seatbot run               # 访问 http://localhost:8080
```

> **Web 前端开发（可选）**：Web 面板用 Tailwind v3 构建，构建产物 `seatbot/web/static/style.css`
> 已入库，**部署无需 Node**。仅在修改设计 token（`src/input.css` / `tailwind.config.js`）或
> 组件类后需要重新构建：
> ```bash
> npm install && npm run build      # 产出 seatbot/web/static/style.css（提交产物）
> npm run dev                       # watch 模式，开发时实时重建
> ```

### 云服务器 (Linux + Docker)

容器内**所有运行数据都在 `/data` 卷内**（配置、SQLite、日志），容器重建不丢数据。

```bash
# 在服务器上:
git clone <repo> && cd Library-Seat-Reservation
mkdir -p data
# 容器以非 root（uid 10001）运行，数据目录属主需与其一致
sudo chown -R 10001:10001 data
cp config.example.yaml data/config.yaml
# 编辑 data/config.yaml：
#   web_host 必须为 0.0.0.0（容器内 127.0.0.1 只在容器内部可达，端口映射会连不上）
#   外网访问还需 runtime.web_token（推荐，填一个令牌）或在 runtime.allowed_hosts
#   列入服务器的 IP/域名——面板默认只放行回环来源（127.0.0.1/localhost/::1）
docker compose up -d
# 访问 http://<server-ip>:8080
```

> 不放 `data/config.yaml` 时，容器会首次启动自动写入一份默认配置（`web_host: 0.0.0.0`），
> 但该默认配置**只允许回环来源访问面板**：从服务器 IP/域名访问前必须补上
> `runtime.web_token` 或 `runtime.allowed_hosts`。

### Linux systemd

```bash
sudo useradd -r -m -s /bin/false seatbot
sudo cp -r . /opt/seatbot
sudo chown -R seatbot:seatbot /opt/seatbot
cd /opt/seatbot && sudo -u seatbot python -m venv .venv
sudo -u seatbot -H /opt/seatbot/.venv/bin/pip install -e .
sudo -u seatbot -H /opt/seatbot/.venv/bin/playwright install chromium
sudo cp config.example.yaml /opt/seatbot/config.yaml
sudo chown seatbot:seatbot /opt/seatbot/config.yaml
sudo chmod 600 /opt/seatbot/config.yaml          # 内含账号凭据
# 编辑 /opt/seatbot/config.yaml: 填入馆舍参数与守护账号手机号密码
sudo cp systemd/seatbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seatbot
sudo journalctl -u seatbot -f
```

> **面板访问**：默认绑 `127.0.0.1`，仅本机可访问（`http://127.0.0.1:8080`）。
> 需要远程访问时，在 `config.yaml` 里把 `web_host` 改为 `0.0.0.0`，并设置 `runtime.web_token`
> （推荐）或在 `runtime.allowed_hosts` 列入服务器 IP/域名——面板默认只放行回环来源，
> 之后访问 `http://<server-ip>:8080`。服务单元已固定 `TZ=Asia/Shanghai`（日志时间戳与调度一致）。

## 已知风险

- 纯 HTTP 签到：缺少真实蓝牙基站 + 位置，超星风控可能识别为作弊，守护账号可能被拉入黑名单
- 多账号并发：同一 IP 多账号，容易被识别为脚本
- 用户已明确接受这些风险

## 安全提示

- `config.yaml` 含明文账号密码，`chmod 600 config.yaml`
- Web 面板**默认无鉴权**，且默认只放行回环来源（`127.0.0.1` / `localhost` / `::1`）
- 需要对外暴露时：设置 `runtime.web_token`（面板令牌）或 `runtime.allowed_hosts`（Host 白名单），
  并建议在前面套 nginx + basic auth，或保持 `web_host: 127.0.0.1` 只监听本机
- 跨站写请求有同源校验（Origin/Referer 与 Host 不一致一律 403），但本机可达的调用方不受此保护

## 开发

```bash
pytest -v                   # 单元测试
pytest -m integration -v    # 集成测试 (需真实账号)
ruff check seatbot tests
```
