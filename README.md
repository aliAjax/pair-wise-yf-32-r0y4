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
- `POST /api/allocations/{id}/events`：三方（来源医院、接收医院、调配员）提交转运事件，接入分配时间线对账。
- `GET /api/allocations/{id}/timeline`：分配时间线对账；医院只看到本机构的分配。
- `GET /api/allocations/{id}/audit`、`GET /api/state`：完整审计和权限视图。

## 时间线对账

- 同一个动作晚到或重复提交只补一次记录（幂等），不新增条目；已完成步骤不退回。
- 两个机构同时提交矛盾的完成（交接/植入）与撤回时，按提交时刻取最后生效的一步，被压掉的记录标为 `conflict`。
- 已完成交接/植入后才提交的撤回属于晚到，标 `conflict`，不退回已完成步骤。
- 每条时间线记录都包含动作、提交人和所属机构；历史分配没有时间线时，按现有状态补一条初始记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

血型兼容与评分是演示规则，不包含 HLA 分型、器官大小、病程、儿科差异和真实移植网络规则。医院身份使用请求头模拟，SQLite 环境适合原型，不处理跨机构身份信任、远程患者隐私协议和真实冷链设备接入。
