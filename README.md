# 05-proofpack（核证匣）

Proofpack — 举证放行：分层配额采样 → 证据打包 → 校验清单 → 放行/驳回闭环。

与 Feedstitch（多源归并）、Batchgate（作业编排）、Syncledger（双端对账）、Changecast（CDC 投影）主链路不同。夹具全本地，不连活库。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5500 |
| API | http://localhost:10500 |

## 主链

选举证对象 → 按层配额确定性采样 → 生成证据包（含指纹）→ 跑校验清单（含分层配额互证）→ 全过且层配额门禁通过才可放行，否则驳回并留痕

## 分层配额采样

- 夹具约定分层字段（`{"strata_field", "rows"}`）：门店日结 `region`（城区4/近郊4/远郊2）、渠道结算 `channel_group`（A3/B3/C2）、价目变更 `price_band`（低6/中5/高1）。
- 配额按对象版本化（`quota_configs`）：每层目标条数（`count`）或层内采样率（`rate`），配额层集合须与夹具层集合一一对应。
- 总样本为各层结果合并；批次写明每层源条数、目标条数、样本数、是否触达配额（`sample_batch_strata`）。
- 同一种子 + 同一配额版本重跑，各层样本集合可复现。

### 源不足配额（二选一，单值策略 `shortfall_policy`）

- `strict` 严格：任一层不足配额，整次采样失败，不落批次、不产生可打包样本。
- `best_effort` 尽力：能抽多少算多少，批次标记未满层（`ready_shortfall`）；未满批次允许保留样本与摸底清单、可打包，但包一律 `releasable=0`，放行通过硬拒绝。

### 配额变更（二选一，单值策略 `stale_policy`）

改配额产生新版本，并在同一事务内处置仍引用旧配额的未放行包：

- `invalidate`：作废受影响未放行包（`voided`），不得再放行决策。
- `freeze`：保留只读对照但一律禁止放行，直至按新配额重新采样打包。

旧配额批次禁止再打包；改配额与正在进行的分层采样互斥同成同败——采样落库完成前配额被改，本次采样失败（409），不留半层样本批次或可放行包。

### 互证与放行门禁

- 清单含「分层配额互证」：层配额 ↔ 批次分层明细 ↔ 样本行 ↔ 包条目逐层核对。
- 放行通过前重新推导：无分层明细、尽力未满、配额已变更、已作废的包一律硬拒绝（400），拒绝原因引用分层明细。

## 种子举证对象

- 门店日结抽检
- 渠道结算抽检
- 价目变更核证

底座内置基础校验：条数一致、键唯一、必填非空、分层配额互证。影子试跑 / 规则版本表空挂，深逻辑留给后题。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 本地后端

```bash
cd backend
pip install -r requirements.txt
set DATA_DIR=%TEMP%\proofpack-data
uvicorn app.main:app --host 127.0.0.1 --port 10500
```

## 测试

```bash
cd backend
python -m pytest tests/
```
