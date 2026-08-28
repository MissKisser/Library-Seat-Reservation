# SeatBot — 超星图书馆座位自动预约 (座位保护模式)

> 让某个**目标座位** (`target_seat_num`) 在 08:00-22:00 全天显示"已有人预约", 阻止他人抢走。
> 多守护账号 + 错时段重叠覆盖 = 全天护城河。

设计与实施细节见本地 `docs/` 目录（依文档规范不入库，总览见 [docs/README.md](docs/README.md)）。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .[dev]
playwright install chromium
cp config.example.yaml config.yaml
# 编辑 config.yaml: 填入 target_seat_num + 至少 2 个守护账号 + 错时段 slots
python -m seatbot init-db
python -m seatbot run
# 访问 http://localhost:8080
#   - 首页: Gantt 覆盖图 (列=30min 时段, 行=守护账号)
#   - 目标座位: 修改 target_seat_num
#   - 守护账号: 增删改 + 测试登录
#   - 覆盖报告: 检查失守时段
```

## 工作原理

1. **目标座位** (`library.target_seat_num`): 你实际要坐的座位号 (例如 84)
2. **守护账号** (1-N 个): 程序替你抢目标座位的辅助超星账号
3. **时段 slots**: 每账号配置若干 `"HH:MM-HH:MM"`, 程序自动拆为 ≤2h 子段
4. **覆盖**: 所有账号的 slots 合并应覆盖 08:00-22:00; 建议相邻账号首尾重叠 30min, 保证无缝衔接
5. **每日 14:00 起**: 程序自动抢次日全部时段
6. **签到/签退**: 每个时段 start_time 后 `pre_sign_duration`(默认 20min) 内签到, 接近 end_time 时签退并续约下一段

### 推荐配置：3 守护账号 × per-seat slots（2026-08-24 实战版）

**业务约束**（用户明确）：
- 单账号每天最多 3 段；单账号对同一座位同一时段只 1 段
- 2 座位 × 3 时段 = 6 段/天，3 账号各 2 段刚好覆盖（每段 ≤2h）
- 时段：09:00-11:00 / 15:00-17:00 / 19:00-21:00（21:00-22:00 接受失守，使用率低）

```yaml
accounts:
  - id: xiongjt                    # 账号1: 守 104/早 + 105/午
    seat_slots:
      "104": ["09:00-11:00"]        # 104 号位 早班
      "105": ["15:00-17:00"]        # 105 号位 午班
    bound_seats: ["104", "105"]
  - id: wangh                      # 账号2: 守 104/午 + 105/晚
    seat_slots:
      "104": ["15:00-17:00"]
      "105": ["19:00-21:00"]
    bound_seats: ["104", "105"]
  - id: zhaozh                     # 账号3: 守 104/晚 + 105/早
    seat_slots:
      "104": ["19:00-21:00"]
      "105": ["09:00-11:00"]
    bound_seats: ["104", "105"]
```

覆盖矩阵（每行 = 一个 (座位, 时段)，每列 = 守护账号）：

```
              09:00-11:00  15:00-17:00  19:00-21:00
104 号位       xiongjt       wangh         zhaozh
105 号位       zhaozh        xiongjt       wangh
```

**6 段精确生成**：每账号每天 2 段、共 6 段、无笛卡尔积、超额零段。

### 设计文档 §4.4 范例（仅作参考，本项目不采用）

设计文档 `2026-07-08-library-seat-reservation-design.md §4.4` 给的范例是**笛卡尔积模式**（每账号 slots × bound_seats = 多倍段数），会生成 22 段/天（实测），对超星服务端接受度要求高，风险大于上面的 per-seat 配置。**本项目推荐使用 per-seat slots**，见上一节。

```yaml
# 不推荐 — 笛卡尔积模式 (设计文档 §4.4 范例)
accounts:
  - id: guard_a
    slots: ["09:00-11:00", "13:00-15:00", "17:30-19:30"]
    bound_seats: ["104", "105"]   # 会展开成 6 段 (3 时段 × 2 座)
  - id: guard_b
    slots: ["10:30-12:30", "15:00-17:00", "19:30-21:30"]
    bound_seats: ["104", "105"]   # 6 段
  - id: guard_c
    slots: ["08:00-10:30", "12:00-14:00", "19:00-21:30"]
    bound_seats: ["104", "105"]   # 6 段 (其中 19:00-21:30 被拆成 2 段)
# 共 22 段/天
```

### 若账号本人要自约某段

把该段加入 `user_reserved`（scheduler 在 bootstrap 时跳过对应守护账号的冲突段，scheduler.py:113-119）。但 `user_reserved` 是**按账号 ID 索引**的——仅跳过**该账号**的对应段；其他守护账号仍会抢同一时段（需手动调整其他账号的 slots 避免冲突）。
## 已知风险

- 纯 HTTP 签到: 缺少真实蓝牙基站 + 位置, 超星风控可能识别为作弊, 守护账号可能被拉入黑名单
- 多账号并发: 同一 IP 多账号, 容易被识别为脚本
- 用户已明确接受这些风险

## 命令

```bash
python -m seatbot run --config config.yaml
python -m seatbot once --account zhangsan --action all
python -m seatbot login --account zhangsan
python -m seatbot status
python -m seatbot init-db
```

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
# 编辑 config.yaml: 填入 target_seat_num + 守护账号 + 时段
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

```bash
# 在服务器上:
git clone <repo> && cd Library-Seat-Reservation
mkdir -p data logs
cp config.example.yaml data/config.yaml
# 上传 / 编辑 data/config.yaml
docker compose up -d
# 访问 http://<server-ip>:8080
```

### Linux systemd

```bash
sudo useradd -r -s /bin/false seatbot
sudo cp -r . /opt/seatbot
sudo chown -R seatbot:seatbot /opt/seatbot
cd /opt/seatbot && sudo -u seatbot python -m venv .venv
sudo -u seatbot /opt/seatbot/.venv/bin/pip install -e .
sudo cp systemd/seatbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seatbot
sudo journalctl -u seatbot -f
```

## 安全提示

- `config.yaml` 含明文账号密码, `chmod 600 config.yaml`
- Web 面板无鉴权, **仅限内网访问**
- 建议在前面套 nginx + basic auth 或仅监听 127.0.0.1

## 已知风险

- 纯 HTTP 签到, 缺少蓝牙/位置, 超星风控可能识别为作弊 → 账号可能进黑名单
- 多账号并发, 同一 IP 多账号, 容易被识别为脚本
- 用户已明确接受这些风险

## 开发

```bash
pytest -v                   # 单元测试
pytest -m integration -v    # 集成测试 (需真实账号)
ruff check seatbot tests
```