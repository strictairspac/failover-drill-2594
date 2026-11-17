# 数据结构
本小型项目首次启动使用 SQLAlchemy metadata 建立 users/nodes/scenarios/drills/drill_applies/audits 六表。自然键 users.email、nodes.name、scenarios.name、drill_applies.drill_id 与 drill_applies.credential 唯一。drills.scenario_id、drill_applies.drill_id 为外键，演练快照在入队时固定，落实凭据在落定时固定。

drill_applies 记录每次结论落实：status（pending/applying/applied/rejected）、租约 owner/lease_until、尝试次数、冻结快照前后的节点视图、拒绝当时的节点、字段变化与拒绝原因。升级既有部署时需为 drill_applies 建表（新表，不影响旧表数据）：

```sql
CREATE TABLE drill_applies (
  id INT PRIMARY KEY AUTO_INCREMENT,
  drill_id INT NOT NULL UNIQUE,
  status VARCHAR(20) NOT NULL,
  credential VARCHAR(64) NOT NULL UNIQUE,
  evidence_sha256 VARCHAR(64) NOT NULL,
  actor VARCHAR(120) NOT NULL,
  attempts INT NOT NULL DEFAULT 0,
  owner VARCHAR(120) NULL,
  lease_until DATETIME NULL,
  before_nodes JSON NULL,
  after_nodes JSON NULL,
  observed_nodes JSON NULL,
  changes JSON NULL,
  problems JSON NULL,
  applied_at DATETIME NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT fk_drill_applies_drill FOREIGN KEY (drill_id) REFERENCES drills(id)
);
```

若需让旧演练出现在落实候选列表，必须重新提交演练；不伪造历史凭据。上线结构演进须补充版本化迁移，本项目不支持对既有不同结构数据库自动升级。
