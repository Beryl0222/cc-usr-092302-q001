# 家庭持续阅读接力 · 领域设计

句象书店作家对谈（北京、长沙、武汉……）之后，读者需要一条"活动 → 适龄书单 → 借阅 → 阅读指导 → 家庭共读反馈 → 下一场活动"的持续服务。
本文档定义角色、限界上下文、事件语义与关键业务不变量，是各门店系统、家庭端、运营端共同遵守的合同基线。

实现层面只依赖事件溯源（event sourcing）：**事实只追加、不覆盖**；任何当前状态都是事件流的折叠（fold）结果。

---

## 1. 角色与代行关系

| 角色 | 说明 |
|---|---|
| 读者（person） | 自然人。未成年人即"儿童读者"，按出生日期动态计算年龄。 |
| 监护人（guardian） | 与儿童读者建立监护关系的成年读者；关系有起止与授权范围，可撤销。 |
| 门店馆员 / 阅读指导人（librarian / guide） | 门店工作人员；指导资格来自角色授权，**不来自消费**。 |
| 区域运营者（operator） | 跨店视角：活动、书单、库存调度、追踪分析。 |
| 系统（system） | 定时任务、集成方等非人主体。 |

**代行（act-on-behalf）规则：**

1. 家长代办时，事件 `actor_id` 记家长，`on_behalf_of` 记孩子，且必须存在当时有效的监护授权，授权范围覆盖该动作。
2. **孩子的撤回优先**：儿童本人（在其与年龄相称的能力范围内，或通过指导人协助确认）做出的撤回（退出推荐、撤回授权、删除反馈等），监护人不能用新的代办动作覆盖；折叠状态时孩子的撤回标记一旦出现即为终态。
3. 代办产生的记录向双方可见，但儿童视图只暴露适龄内容（见 §5）。

## 2. 隐私授权模型

授权是**带版本、带范围、可撤回**的事件事实，而不是档案上的一个布尔位：

- `CONSENT_GRANTED`：授权人（或监护人对儿童）、被授权处理者、范围（`profile_view` / `recommendation` / `guidance` / `event_followup` / `cross_store_history` / `family_feedback`）、适用门店、有效期。
- `CONSENT_WITHDRAWN`：撤回。**儿童本人的撤回优先级高于监护人后续的授予**；撤回不删除历史事实，但此后的折叠与视图必须停止该范围的处理，新派生数据不再生成。
- 授权文本与推荐理由一样需要版本固定（见 §6 的快照原则）：`basis_version` 指向当时授权文本版本。
- 撤回/删除请求产生的是**逻辑停止使用**视图（tombstone），不是物理改写事件流。

## 3. 限界上下文与事件目录

代码与 Schema 以 `contracts/event-catalog.json` 为唯一目录；下表是概览。

### 3.1 身份与家庭（identity）
读者建档、监护关系建立/解除、隐私授权授予/撤回。
`PERSON_REGISTERED`、`GUARDIANSHIP_GRANTED`、`GUARDIANSHIP_REVOKED`、`CONSENT_GRANTED`、`CONSENT_WITHDRAWN`。

### 3.2 画像与偏好（profile）
兴趣与年龄分级。儿童偏好可由家长代录，也可由孩子本人维护；孩子撤回的偏好不再进入推荐。
`PREFERENCE_DECLARED`、`PREFERENCE_WITHDRAWN`。

### 3.3 书目（catalog）
作品（work）与具体版本（edition，ISBN/出版社/适读年龄/内容分级）分离；推荐与书评绑定在版本上。
`WORK_RECORDED`、`EDITION_RECORDED`、`EDITION_UPDATED`、`AGE_GATING_SET`。

### 3.4 书单与推荐（recommendation）
活动后或日常的书单。推荐发出时**快照**书目说明与推荐理由；书目说明日后更新，历史推荐仍能还原当时理由。
`BOOKLIST_PUBLISHED`、`RECOMMENDATION_MADE`、`RECOMMENDATION_WITHDRAWN`。

### 3.5 库存与门店（inventory）
单本可借物（item，条码）与版本区分；门店信息、营业时间、临时闭店。
`STORE_RECORDED`、`ITEM_RECORDED`、`STOCK_ADJUSTED`、`STORE_TEMPORARILY_CLOSED`、`STORE_REOPENED`。

### 3.6 借阅（loan）——含跨店
共享借阅单、取/还安排、在途、逾期。
`LOAN_RESERVED`、`LOAN_PICKED_UP`、`LOAN_IN_TRANSIT`（跨店调拨/归还在途）、`LOAN_ARRIVED`、`LOAN_RETURNED`、`LOAN_OVERDUE_MARKED`、`LOAN_RENEWED`、`LOAN_MISSING_MARKED`。

### 3.7 活动（event）
作家对谈等活动：报名、候补、签到。报名名额、候补名单、会员权益**三条账分开**。
`AUTHOR_EVENT_SCHEDULED`、`EVENT_REGISTRATION_OPENED`、`EVENT_REGISTERED`、`EVENT_WAITLISTED`、`EVENT_REGISTRATION_PROMOTED`、`EVENT_REGISTRATION_CANCELLED`、`EVENT_ATTENDED`。

### 3.8 会员权益（membership）
会员身份与权益（活动名额优先、跨店次数等）是独立事实流；**借阅次数与活动报名分别计数**。
`MEMBERSHIP_GRANTED`、`MEMBERSHIP_REVOKED`、`BENEFIT_USED`、`BENEFIT_RESTORED`。

### 3.9 阅读指导（guidance）
资格授予基于角色/培训，与消费金额无关；指导会话与建议留痕。
`GUIDE_QUALIFIED`、`GUIDANCE_SESSION_HELD`、`GUIDANCE_NOTE_RECORDED`。

### 3.10 家庭反馈（feedback）
共读反馈、孩子与家长分别的声音；孩子可撤回自己的反馈。
`FAMILY_FEEDBACK_RECORDED`、`FEEDBACK_WITHDRAWN`。

## 4. 事件信封（在基线上扩展）

基线五个必填字段保留：`event_id, kind, occurred_at, subject_id, version`。新增字段：

| 字段 | 必填 | 语义 |
|---|---|---|
| `context` | 是 | 限界上下文名（§3 的英文标识）。 |
| `actor_id` | 是 | 动作实际发起人；系统动作为 `system:*`。 |
| `on_behalf_of` | 否 | 代行时的最终归属读者（家长代办=孩子 id）。 |
| `request_id` | 条件必填 | 由外部请求（扫码、报名、API）产生的事件必填。**幂等键**（见 §8）。 |
| `correlation_id` | 否 | 一次业务接力（如"某场对谈的后续链路"）的贯穿 id，运营追踪用。 |
| `causation_id` | 否 | 直接触发本事件的前一事件 id（如 `LOAN_IN_TRANSIT` 由调拨命令导致）。 |
| `payload` | 是 | 按 `kind` 在目录中定义；只含事实，不含可派生状态。 |
| `basis_versions` | 否 | 快照依据版本：授权文本、书目说明、分级表等（见 §6）。 |

约束：

- 时间一律 RFC3339 带偏移量（门店跨时区也可排序）；`occurred_at` 是事实发生时间，事件入库时间另存不入合同。
- `event_id` 全局唯一；`(request_id, kind, subject_id)` 在事件流中至多出现一次有效申请。
- 事件不可变；纠错采用补偿事件（如 `LOAN_MISSING_MARKED` / `STOCK_ADJUSTED(reason=correction)`），不更新旧事件。

## 5. 儿童适龄与视图

- 年龄按读者出生日期在**查询时刻**计算，不存静态年龄；分级表按版本管理（`AGE_GATING_SET` 带 `basis_version`）。
- 任何面向儿童的列表（书单、可借、活动、反馈记录）输出前必须经 `visible_for_age(...))` 过滤：版本当前 `min_age <= 年龄 <= max_age` 且无未撤回的内容性撤回标记。
- 家长端可以看到更广的管理信息，但**孩子自己的撤回在家长端同样标注为不可恢复**，不得提供"替孩子重新开启"的入口。
- 适龄判断使用被观看时刻的分级版本；而"当时为什么推荐这本书"使用推荐时刻的快照版本（§6），两者不互相覆盖。

## 6. 推荐理由可还原（快照原则）

`RECOMMENDATION_MADE.payload` 必须内嵌：

- `edition_key` + `edition_description_snapshot`（当时的内容简介全文或哈希+归档地址）、
- `age_gating_snapshot`（当时适读区间与分级表版本）、
- `reason_codes` 与 `reason_text`（当时理由）、
- `basis_versions`：`{ catalog_note, age_gating, consent_text }` 各自版本号。

`EDITION_UPDATED` 只改变**今后**的展示与推荐；查看历史推荐时，系统用快照回放，输出必须等于当年发出时的文字。运营复盘"为什么当时推它"时读快照，不读当前表。

## 7. 借阅：跨店、在途、逾期、闭店影响面

### 7.1 状态机（单册 item）

`AVAILABLE → RESERVED → PICKED_UP(ON_LOAN) → RETURNED → AVAILABLE`
跨店还书/调拨：`ON_LOAN → IN_TRANSIT → ARRIVED(AT_HOLDING_STORE) → AVAILABLE`
异常分支：任一持有态 → `OVERDUE`（标记，不改变持有关系）；`→ MISSING`。

- 借出与归还以**单册 item** 为主体；库存可借数是事件折叠结果，不单独维护易失计数器。
- 逾期由系统定时任务对"已超过 due_at 且未还/未续"的借阅单发 `LOAN_OVERDUE_MARKED`；逾期标记幂等（同一 loan 单重复标记只生效一次）。
- 在途期间起止门店都能看到该册，但状态为 `IN_TRANSIT`，任一门店都不可再次借出。

### 7.2 临时闭店只影响"取/还安排"

`STORE_TEMPORARILY_CLOSED` 载荷只含门店、闭店起止、替代门店。规则：

- 受影响集合 = 取/还门店命中闭店区间、且安排时间与闭店区间重叠的**未完成**借阅单与预约；
- 对这些单生成安排改期/改店事件（改的是 `pickup_plan`/`return_plan` 派生安排）；
- **不**关闭活动、不改动其他门店的库存、不影响在途册的流向（在途册目的地若闭店则顺延并通知，册状态仍连续）；
- 家庭端看到的地点/时间/可借状态由同一事件流折叠，门店 POS 与家庭 App 不存在第二份"柜台记录"。

## 8. 幂等、断网补传与重复扫码

- 每次外部动作（扫码借出、扫码归还、报名提交）由终端生成 `request_id`（建议 UUIDv7，离线时本地预生成）。
- 追加事件前查幂等表：`(request_id)` 已存在 → 返回原结果，不新增事件、不动库存、不增借阅次数。
- 断网：终端本地排队，网络恢复后按 `request_id` 补传；服务端去重保证同一请求只折叠一次。
- 库存与借阅次数的扣减**只能**作为事件折叠的结果存在，禁止"先扣库存再写事件"；因此重复补传天然不会重复扣减。
- 同一册被两个终端同时借出：以事件流追加顺序为准，第二个 `LOAN_PICKED_UP` 因折叠时 item 非可借状态而被拒绝（冲突事件可记为拒绝回执，不改变状态）。

## 9. 活动报名 / 候补 / 会员权益三本账

- **报名账**：`EVENT_REGISTERED` / `EVENT_REGISTRATION_CANCELLED` / `EVENT_ATTENDED`，决定出席与名额占用。
- **候补账**：`EVENT_WAITLISTED` → 名额释放时按候补序发 `EVENT_REGISTRATION_PROMOTED`（需要候补人在限时内确认，超时顺延下一位；晋升幂等）。
- **权益账**：`BENEFIT_USED`（如"会员优先名额""跨店免费次数"）独立于报名事件；取消报名触发 `BENEFIT_RESTORED`。
- 计数规则：同一活动同一读者报名事件至多一条有效；取消后可重新报名但不恢复原候补序位。
- **借阅次数（loan 域）与活动报名（event 域）永不在同一计数器上扣减**；会员权益的消耗在权益账留痕，报名系统只读取"是否可用"的折叠结果。

## 10. 对谈 → 后续借阅与反馈的追踪链

- 一场对谈一个 `correlation_id`（= 活动 id）；活动当天 `EVENT_ATTENDED` 携带该 id。
- 活动现场或后面对该读者产生的 `RECOMMENDATION_MADE`、`LOAN_*`、`GUIDANCE_SESSION_HELD`、`FAMILY_FEEDBACK_RECORDED` 都沿用同一 `correlation_id`，并用 `causation_id` 串直接前因。
- 运营查询：按活动 id 折叠即可得到"出席读者 → 推荐版本 → 借出册 → 续借/逾期 → 指导 → 家庭反馈"的全链；反之从一册书可反查来源于哪场活动。
- 追踪链的可见性受授权范围 `event_followup` / `cross_store_history` 约束：授权撤回后链上历史事实保留，但运营视图对该读者停止展开个人字段。

## 11. 阅读指导资格不挂钩消费

- 指导资格只由 `GUIDE_QUALIFIED`（培训/角色授权，含有效期与复审）决定；`GUIDANCE_SESSION_HELD` 的校验只查：资格有效、监护授权 `guidance` 有效、内容适龄。
- 规则函数 `assert_guidance_allowed(...)` 的输入**不含**消费、充值、积分字段；任何携带消费条件的调用直接拒绝（合同层面的护栏，防止需求被悄悄塞回）。
- 活动后获得指导是出席/需求触发的服务，不是奖品或等级权益。

## 12. 门店一致性

家庭端展示的地点、时间、可借状态必须与门店一致：全部读同一事件流的折叠投影，不允许柜台本地表作为事实源；闭店改期（§7.2）与库存变动通过投影刷新同时生效。

## 13. 关键不变量速查（对应 `src/rules.py`）

1. 代行必须有有效监护授权；儿童本人撤回 > 监护人授予。
2. 儿童视图只含适龄且未被内容撤回的条目。
3. 推荐快照可逐字还原，不受书目更新影响。
4. 单册库存状态机闭合；重复 request_id 不产生第二次效果。
5. 闭店事件只改命中区间的未完成取/还安排。
6. 报名、候补、权益使用分别计数；权益取消可恢复。
7. 指导资格校验输入中出现消费维度即拒绝。
8. 逾期标记幂等；在途册不可被再次借出。
9. 任一事件可沿 correlation/causation 回溯到对谈活动。
10. 撤回授权后派生视图停止使用对应范围数据，历史事件不被物理删除。

## 14. 待与业务确认的开放问题

- 儿童本人"撤回"的年龄门槛与协助确认流程（多大年龄可独立撤回，指导人如何核验）。
- 候补晋升的确认限时默认值，以及未确认后权益是否立即返还。
- 跨店在途的责任分界（在途逾期算借出店还是调入店）。
- 授权文本与书目说明快照的留存年限与归档介质（哈希存证 vs 全文存档）。
- 闭店替代门店在库存全闭（如商场整体停业）时的兜底（邮寄/顺延）。
