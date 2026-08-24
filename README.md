# SeatBot — 超星图书馆座位自动预约 (座位保护模式)

> 让某个**目标座位** (`target_seat_num`) 在 08:00-22:00 全天显示"已有人预约", 阻止他人抢走。
> 多守护账号 + 错时段重叠覆盖 = 全天护城河。

详见 [设计文档](docs/superpowers/specs/2026-07-08-library-seat-reservation-design.md) 与 [实施计划](docs/superpowers/plans/2026-07-08-library-seat-reservation.md)。

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

### 参考时段配置（设计文档 2026-07-08-library-seat-reservation-design.md §4.4）

3 个守护账号 × 每个账号 3 个 slots，相邻账号首尾重叠 30min，可全天 08:00–22:00 无空档：

```yaml
accounts:
  - id: guard_a
    slots: ["09:00-11:00", "13:00-15:00", "17:30-19:30"]
  - id: guard_b
    slots: ["10:30-12:30", "15:00-17:00", "19:30-21:30"]
  - id: guard_c
    slots: ["08:00-10:30", "12:00-14:00", "19:00-21:30"]
```

合并后的覆盖图（每行一个账号，▓ = 该账号负责的时段）：

```
              08 09 10 11 12 13 14 15 16 17 18 19 20 21 22
guard_c ▓▓▓▓▓▓▓▓▓▓      ▓▓▓▓▓▓▓▓▓▓            ▓▓▓▓▓▓▓▓▓▓
guard_a       ▓▓▓▓▓▓▓▓     ▓▓▓▓▓▓▓▓    ▓▓▓▓▓▓
guard_b            ▓▓▓▓▓▓▓▓▓▓   ▓▓▓▓▓▓▓▓▓▓  ▓▓▓▓▓▓▓▓▓▓
```

设计要点：

- 每个账号 **3 段/天** = 配置 `one_account_max_concurrent_segments_per_day: 3`（设计文档中称为"乐观模式"，可让单账号跨多段拼接）。设为 1 即"悲观模式"——每账号每天只抢一段，更安全但需要更多账号。
- 相邻账号 slots **首尾重叠 30min**（如 guard_a `09-11` 与 guard_b `10:30-12:30`），确保前一个账号签退前下一个账号已签到，无人能抢到空档。
- 若某时段**所有**守护账号都失败 → 该时段失守，`/coverage` 红框告警，`logs` ERROR。
- `library.max_reserve_hours: 2.0` 是单次最大预约长度（超星上限），slots 中任何超过 2h 的 range 会被自动拆为 ≤2h 子段。

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