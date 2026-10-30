# cgbot 文档库

更新日期:2026-10-30。项目处于文档阶段,没有代码。

## 从哪里开始

| 需要了解的内容 | 入口 |
| --- | --- |
| 设计、已定决策、调研结论与待答问题 | [DESIGN.md](DESIGN.md) |
| 要交付什么、阶段依赖 | [Milestone](MILESTONE.md) |
| 每个阶段由哪些能力组成 | [Roadmap 索引](ROADMAP.md) |
| 当前具体任务与实施顺序 | [TODO 索引](todos/README.md) |
| 仓库规范、硬约束与原子化提交 | [AGENTS.md](../AGENTS.md) |

## 文档如何维护

工作流只有三层:**Milestone 生成 Roadmap,Roadmap 的每个实施条目生成一份 TODO**。设计事实以 `DESIGN.md` 为准;`reference/`、`testing/`、`operations/`(出现时再建)只存共用资料。
一条 Roadmap 条目对应一个 `todos/R<阶段>.<条目>-<主题>.md`,任务 `Tnn`、测试 `Unn`/`Inn`/`Cnn`,编号不重排、不复用。任务状态只在所属 TODO 维护,再回填 Roadmap 与 Milestone。
