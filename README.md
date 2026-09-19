# 颅内决策实验编排

服务用于协调临床优先条件下的颅内电极研究，保存刺激、行为与脑信号的时间关系和安全决定。

研究对象是因临床原因已植入深部电极的住院患者，住院窗口稀缺。系统在**临床优先**的前提下编排实验游戏、临床监测与休息安排，管理同意范围、电极位置版本、刺激材料（宝石/炸弹概率）以及毫秒级行为与脑信号时间轴。

## 设计原则

1. **临床优先**：临床占时（监测/休息/处置/影像）不可移动，研究环节只能排进空档；新的临床安排出现时，与之冲突的未开始环节立即失效。
2. **原始流不可变**：行为事件与脑信号按设备时钟原样摄取，内容哈希固化；设备时钟校正与数据分段都是引用原始流的派生层，绝不改动原始流。
3. **预注册治理**：验证性分析必须引用预注册假设与注册时固定的数据范围，且假设注册先于数据采集；临时试出的结果只能留在探索区，不能作为发布依据。
4. **安全联动**：不良反应、医嘱变化或参与者暂停被记录的同一时刻，未开始环节立即失效、进行中环节立即中止；已完成数据按同意决定处理（保留或隔离销毁）。
5. **可复现发布**：发布“做/不做”区域结论前，发布包须能复现排除记录、事件对齐与多重分析版本，并核对原始流哈希未变。
6. **最小知情**：分析人员看不到患者直接身份；临床人员可查看安全信息，但接触不到未授权的研究推断。

## 角色

| 角色 | 能做什么 | 看不到什么 |
| --- | --- | --- |
| `coordinator` 协调员 | 登记参与者/同意/电极/刺激，排程与执行环节，摄取原始流 | 分析结果与发布结论 |
| `clinician` 临床人员 | 登记临床占时、不良反应、医嘱变化，查看安全视图（含身份） | 研究推断（假设、分析、结论） |
| `analyst` 分析人员 | 时钟校正、分段、事件对齐、预注册、分析、发布复现 | 患者直接身份、安全视图 |

角色通过请求头声明（原型约定）：`X-Actor-Role: coordinator|clinician|analyst`，`X-Actor-Id: <操作者>`。

## 结构

- `study/model.py` — 领域模型：同意版本、电极版本、刺激材料、环节状态机、原始流、时钟校正、分段、假设、分析、安全事件、发布包。
- `study/store.py` — 规范化序列化、内容哈希、只增日志与 `verify_journal` 可复现校验。
- `study/system.py` — 领域核心 `StudySystem`：排程、数据流、治理、安全联动。
- `study/access.py` — 按角色的视图投影（去标识、安全视图、研究推断隔离）。
- `study/api.py` — HTTP JSON 路由与错误映射。
- `service.py` — 服务入口，保留 `/health` 契约。

## 接口摘要

- 登记：`POST /participants`、`/participants/{pid}/consents`、`/electrodes`、`POST /stimulus-sets`
- 排程：`POST /participants/{pid}/clinical-blocks`（临床）、`/sessions`（协调）、`POST /sessions/{sid}/start|complete`
- 数据：`POST /sessions/{sid}/streams`、`POST /streams/{id}/clock-corrections`、`/segments`、`GET /segments/{id}/extract`、`POST /alignments`、`GET /alignments/{id}/run`
- 治理：`POST /hypotheses`、`/analyses`、`/exclusions`、`/publications`、`GET /publications/{id}/verify`
- 安全：`POST /participants/{pid}/adverse-events`、`/order-changes`、`/pause`、`/resume`
- 视图：`GET /participants/{pid}`（按角色投影）、`GET /participants/{pid}/safety`（临床）、`GET /analyses/{id}`（分析）
- 审计：`GET /journal/verify`

错误映射：`400` 输入非法，`403` 越权，`404` 不存在，`409` 领域冲突（临床冲突/同意/治理/状态/不可变）。

## 运行与测试

```bash
python3 service.py --check        # 基础检查
python3 service.py --port 8000    # 启动服务，GET /health 确认身份
npm test                          # 运行全部测试（契约 + 领域 + 接口）
```
