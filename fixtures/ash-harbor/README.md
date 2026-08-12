# 《灰港同盟》原创回测包

这是 SagaSmith Narrative MCP 的机器可读长线回测 fixture。它用 10 个“等价
session”的压缩路线验证以下系统中立能力：

- Level 1 骰池、随机表、时钟和资源变更；
- Lobby 中 profile 与 Pack 的 draft、finalize、import 和 activate；
- Play 中的场景、阵营、关系、时钟、资源、传闻和角色知识；
- downtime 与 world-turn；
- profile 声明后才存在的可选 conflict；
- table、facilitator 和 actor 三种受众隔离；
- 幂等重试、revision 冲突、snapshot/branch 恢复；
- 两个合法结局，主路线到达 `ending.free_compact`。

## 文件

- `profile.json`：原创 Level 1 mechanics profile。
- `campaign-seed.json`：初始角色、世界状态、权限与私有知识。
- `module.json`：10 个压缩场景、两个合法结局和场景证据。
- `route.json`：顺序操作、随机结果归一化方式和机器断言。
- `provenance.json`：来源、许可证据和公开分发决定。

## 原创性与许可

世界、角色、文字、场景、机制表达和回测数据均为本 fixture 原创，不包含任何
商业规则书、模组或世界观的文本。全部内容以 Apache License 2.0 分发。

