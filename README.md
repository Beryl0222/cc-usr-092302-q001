# 家庭持续阅读接力

句象书店（北京 / 长沙 / 武汉）家庭持续阅读服务：把读者与监护关系、年龄兴趣偏好、
书目版本、门店库存、共享借阅、归还、阅读指导、作家活动、家庭共读反馈与隐私授权
连成一条可追溯的事件流。

## 架构

- **事件溯源**：所有状态变化都是追加事件，事件信封沿用基线合同
  `event_id / kind / occurred_at / subject_id / version`，事件目录见
  [`src/events.py`](src/events.py) 的 `PAYLOAD_REQUIRED`，JSON 合同见
  [`contracts/domain-events.schema.json`](contracts/domain-events.schema.json)
  （两者由测试强制同步）。
- **纯归约内核** [`src/service.py`](src/service.py)：命令 API 在事件落库前校验全部
  业务不变量；状态由事件流 fold 得到，可随时整体重放。
- **只读投影** [`src/readmodels.py`](src/readmodels.py)：家庭端与门店端从同一份归约
  状态计算，因此地点、时间、可借状态天然一致。

## 需求与不变量的落点

| 业务要求 | 实现位置 |
| --- | --- |
| 儿童只能看到适龄内容 | 借阅/推荐命令做年龄区间守卫；`visible_catalog` 对儿童过滤超龄书目 |
| 家长可代办，但不能越过孩子本人撤回 | 监护声明校验代办资格；孩子本人撤回写入 `blocked_by_child`，家长重新授权被拒，仅孩子本人可恢复 |
| 书目说明更新后仍能还原推荐理由 | 推荐事件冻结 `title_description_revision`；`recommendation_as_made` 按修订版回放当时说明 |
| 跨店借阅的在途 / 逾期状态 | `LOAN_IN_TRANSIT → READY → PICKED_UP → RETURNED`；`loan_view` 给出跨店标记、应还日与逾期判定 |
| 临时闭店只改受影响的取还安排 | `schedule_closure` 仅改约“闭店窗口内、未完成、同店”的预约，并同步借阅取书店；其他门店与窗口外安排不动 |
| 重复扫码 / 断网补传不重复扣库存和次数 | 命令以 `request_id` 幂等：同一次请求产生的全部事件带同一关联戳，重放实例重建索引后补传只回放首次结果 |
| 活动报名、候补、会员权益分开计算 | 报名/候补各自独立事件与计数；取消按顺位自动递补；借阅名额走 `MEMBERSHIP_GRANTED`，与座位互不影响 |
| 从一次对谈追到借阅和反馈 | 反馈携带 `author_event_id`；`trace_event_followup` 串到场、后续借阅、活动依据的指导、家庭反馈 |
| 家庭与门店看到的地点/时间/可借一致 | 两端都从同一事件流投影；闭店改约同时更新预约与借阅；在途入库对两端同时可见 |
| 不能用消费金额决定阅读指导 | `assign_reading_guide` 的 `basis` 是白名单（兴趣/年龄/活动/反馈/家长请求），任何消费依据在命令层即被拒 |

## 目录

- `src/events.py` — 事件目录与事件构造
- `src/service.py` — 归约器 + 命令 API（不变量守卫、幂等）
- `src/readmodels.py` — 家庭端 / 门店端 / 运营追溯投影
- `contracts/domain-events.schema.json` — 领域事件 JSON 合同
- `fixtures/family-reading-flow.json` — 端到端 28 事件样例（对谈→跨店借阅→闭店改约→反馈）
- `tests/test_service.py` — 24 个测试逐条锁定上述不变量

## 本地检查

```bash
python -m unittest discover -s tests   # 合同 + 全部不变量测试
python -m scripts.export_fixture       # 重新生成端到端样例
```
