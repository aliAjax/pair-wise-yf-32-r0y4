# 器官分配与转运协调系统

Python 标准库独立项目。系统按器官类型、血型、地域、医疗匹配、紧急程度和等待时间排序候选患者，并管理提出、接受、转运、交接、植入或撤回流程。器官过期后所有继续流转操作都会被阻止，全部状态变化写入审计记录。

## 运行

```bash
python3 app.py --db organ_allocation.db
```

默认监听 `127.0.0.1:8203`，首页 `/`，健康检查 `/health`。

身份头：`X-User-Id`、`X-Role`。角色为 `viewer`、`hospital`、`coordinator`、`allocation_officer`、`auditor`；医院角色还需 `X-Hospital`。

## 主要接口

- `POST /api/donors`、`POST /api/candidates`：登记器官与候选患者。
- `GET /api/donors/{id}/ranking`：查看兼容候选排序。
- `POST /api/allocations`：提出唯一分配。
- `POST /api/allocations/{id}/accept`、`withdraw`：医院确认或撤回。
- `POST /api/allocations/{id}/transit`、`delay`：冷链转运和延误上报。
- `POST /api/allocations/{id}/handoff`、`handoff-accept`：来源医院发起、接收医院确认。
- `POST /api/allocations/{id}/implant`：确认植入。
- `POST /api/allocations/{id}/events`：三方（来源医院、接收医院、调配员）提交转运事件，可带 `submitted_at` 表示提交时刻，系统接成单条分配时间线做对账。
- `GET /api/allocations/{id}/timeline`：查看对账时间线，医院只能看本机构相关的分配。
- `GET /api/allocations/{id}/audit`、`GET /api/state`：完整审计和权限视图。

## 转运时间线对账规则

- 每条事件记录提交人、角色和机构（`actor`/`role`/`hospital`），以及提交时刻 `submitted_at` 和入库时刻 `recorded_at`。
- 同一动作晚到或重复提交只保留首次记录（返回 `duplicate`），晚到的中间步骤只补录一次，不把已完成步骤退回。
- 完成类记录（转运、交接、植入）与撤回矛盾时，按 `submitted_at` 取最后生效的一步，被压掉的记录标记 `conflict`；提交时刻相同则先入库的保持生效。
- 已过期分配不再接受任何事件生效，新事件一律记为 `conflict`。
- 历史分配没有时间线时，首次读取或提交事件会按现有状态补一条 `system` 初始记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

血型兼容与评分是演示规则，不包含 HLA 分型、器官大小、病程、儿科差异和真实移植网络规则。医院身份使用请求头模拟，SQLite 环境适合原型，不处理跨机构身份信任、远程患者隐私协议和真实冷链设备接入。
