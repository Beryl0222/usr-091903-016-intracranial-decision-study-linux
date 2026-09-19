# 颅内决策实验编排

服务用于协调**临床优先**条件下的颅内电极风险决策研究：管理同意范围、电极位置版本、
刺激材料、宝石/炸弹概率、毫秒级行为与脑信号时间轴，并保证原始流不可变、
分析可复现、角色信息隔离。

## 运行

```bash
python3 service.py --check                      # 核对服务配置
python3 service.py --port 8000                  # 仅健康检查（基础契约）
python3 service.py --port 8000 --data-dir ./data  # 挂载研究协调领域接口
curl http://localhost:8000/health
```

无第三方依赖，标准库即可运行。测试：`npm test`（同时执行 `service_contract`
与 `study_contract`，共 36 条契约）。

## 角色与令牌

| 令牌 | 角色 | 能做什么 | 不能做什么 |
|---|---|---|---|
| `coord-token` | coordinator | 登记参与者/同意、排程、环节流转、原始流、派生版本 | —— |
| `clinical-token` | clinical | 上报/解除安全事件、查看安全视图 | 接触研究推断、假设与结果 |
| `analyst-token` | analyst | 预注册、排除记录、分析、发布；查看去标识研究视图 | 查看直接身份、安全事件 |

令牌经 `Authorization: Bearer <token>` 或 `X-Auth-Token` 传递。生产部署应替换种子令牌。

## 核心不变量

1. **临床优先**：存在未解除的安全事件（不良反应 / 医嘱变化 / 参与者暂停）时，
   禁止排程与开始环节；事件一上报，未开始环节立即 `invalidated`（永不复活），
   进行中环节 `interrupted`，须临床 `clear` 后才能 `resume`。
2. **同意驱动**：同意范围按版本留存；范围收窄只作废依赖该范围的未开始环节；
   撤回时未开始环节立即失效，已完成数据按预设策略 `retain` / `deidentify` /
   `destroy` 处置（`destroy` 只撤销研究索引可见性，原始字节遵循只写约束不删除）。
3. **原始流不可变**：行为/脑信号字节登记后不可覆盖，仅允许在环节进行中追加；
   设备时钟校正与数据分段生成独立的、版本化的**派生对象**，回链原始流的
   SHA-256 与字节数，绝不回写原始流。
4. **分析可复现**：每个分析必须引用预注册假设与固定数据范围（环节集合 + 数据类型）；
   确认性分析必须挂排除记录（无排除也显式登记空集），并冻结对齐/分段版本；
   研究者临时试出的结果只能留在 `exploratory` 区，不能发布。
5. **发布闸门**：发布“做/不做”结论前自动重放可复现性检查——固定范围状态、
   排除记录、事件对齐哈希、多重分析版本枚举、审计哈希链，任一不过即拒绝发布。
6. **审计链**：所有写操作进入哈希链追加账（每条含前一条哈希），可随时独立校验，
   服务重启后仍可验证。

## HTTP 接口

```
POST   /api/participants                         登记参与者（identity 与研究 code 分离）
POST   /api/participants/{code}/consent           更新同意范围（新版本）
POST   /api/participants/{code}/withdraw          撤回同意
POST   /api/layouts                               登记电极布局版本
POST   /api/layouts/{id}/supersede                标记布局被新版本取代
POST   /api/stimulus-sets                         刺激材料集（版本化）
POST   /api/game-configs                          宝石/炸弹概率（和必须为 1）
POST   /api/sessions                              排程环节
GET    /api/sessions                              协调员状态看板（coordinator）
POST   /api/sessions/{id}/start|complete|resume
POST   /api/safety-events                         上报安全事件（coordinator/clinical）
POST   /api/safety-events/{id}/clear              解除安全事件
GET    /api/safety                                临床安全视图
POST   /api/streams                               登记原始流（payload_base64）
POST   /api/streams/{id}/append                   追加原始字节
POST   /api/alignments                            时钟校正（派生版本）
POST   /api/segmentations                         数据分段（派生版本）
POST   /api/hypotheses                            预注册假设 + 固定数据范围
POST   /api/exclusion-logs                        排除记录（规则版本 + 逐条原因）
POST   /api/analyses                              运行分析（confirmatory/exploratory）
GET    /api/analyses/{id}/reproducibility         可复现性报告
POST   /api/publications                          发布 do / don't 结论（过闸门）
GET    /api/clinical-view                         临床视图（仅安全信息）
GET    /api/analyst-view                          分析视图（去标识）
```

## 持久化布局

```
<data-dir>/
  state.json     # 参与者/同意/配置/排程/假设/分析等全部索引（原子写入）
  raw/<id>.bin   # 原始设备流（只写追加）
  audit.log      # 哈希链审计账（每行一条 JSON，追加写）
```
