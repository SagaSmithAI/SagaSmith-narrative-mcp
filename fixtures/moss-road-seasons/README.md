# 《苔路四季》原创回测包

这是 SagaSmith Narrative MCP 的机器可读无骰长线回测 fixture。它用 10 个“等价
session”的压缩路线验证与《灰港同盟》明显不同的玩法边界：

- Level 0，无骰、无随机表、无 `mechanic_resolve`；
- 没有固定 GM，Owner 只管理 campaign，地点、社区和 NPC 由玩家轮换 stewardship；
- profile 不声明 `conflict`，Play 中不得暴露任何 Conflict 工具；
- 春、夏、秋、冬、解冻五段旅行与持续世界演化；
- table、group 和 actor 私有受众及 ActorKnowledge 隔离；
- 关系、承诺、社区物资、季节时钟、路线和旅行段；
- downtime、world-turn、NPC 隔离对话、snapshot/branch 恢复；
- 两个合法结局，主路线到达 `ending.shared_morning`；
- 与《灰港同盟》使用不同 campaign 并行运行时，状态、知识、幂等和 exposure 不串线。

## 文件

- `manifest.json`：fixture 身份、能力边界和覆盖矩阵。
- `profile.json`：原创 Level 0、分布式 stewardship profile。
- `campaign-seed.json`：角色、社区、路线、权限、element grants 和私有知识。
- `module.json`：10 个压缩场景、两个合法结局和内容断言。
- `route.json`：公开 MCP facade 操作、并行隔离要求和机器断言。
- `provenance.json`：原创性、许可证据和公开分发决定。

## 原创性与许可

世界、角色、文字、场景、程序表达和回测数据均为本 fixture 原创，不包含任何商业
规则书、模组或世界观文本。全部内容以 Apache License 2.0 分发。
