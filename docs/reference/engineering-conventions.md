# 工程约定(R0.2)

更新日期:2026-10-30。TODO:[R0.2](../todos/R0.2-engineering-conventions.md)。`[已定]` 为用户决定,`[推论]` 待用户确认。

## 已定

- **Python**:3.12+。`pyproject.toml` 声明依赖与 `requires-python = ">=3.12"`,`uv` 管理环境,`uv.lock` 提交入库。环境创建/更新:`uv sync`;新增依赖:`uv add`。`[已定]`
- **配置**:TOML,使用标准库 `tomllib` 读取。密钥不入主配置,走独立 secrets 文件或环境变量,二者均不入库(`.gitignore`)。`[已定]`
- **布局**:`src/cgbot/` 包布局;单进程 asyncio;运行时数据在仓库外置的 `data/`(可配置路径,不入库)。`[已定]`
- **质量工具**:`pytest` + `pytest-asyncio`,`ruff`(lint + format),类型检查 pyright 或 mypy 二选一(见下)。`[已定]`
- 测试分层与标记:离线单元(默认运行,不联网)、真实边界集成(marker `integration`,默认跳过,须授权)、平台检查(marker `platform`)。对应 TODO 的 `-Unn/-Inn/-Cnn`。`[已定,标记名为推论]`

## 推论(待确认,不阻塞 M1 起步)

- 类型检查:选 **pyright**(strict 渐进)。理由:速度与 asyncio 推断较好;若用户偏好 mypy 再换。
- 目录:`src/cgbot/{config,napcat,model,identity,session,provider,agent,tools,scheduler,memory,group,voice,coding}/`,与 DESIGN.md §10 模块图对应;适配器(napcat)与领域(agent/group/session)单向依赖,领域不得 import 适配器。
- 配置:启动时加载并校验,错误即启动失败并指明键;热重载不做(DESIGN.md §3 的重载需求由重启代替,若要热重载另议)。键名由 R1.1 定义。
- 日志:标准库 `logging`,结构化键值输出到 stderr 与按日轮转文件;级别 INFO 默认;**不记录私聊消息正文**(只记长度、ID、类型),群聊同样不记正文;密钥、令牌、Cookie 一律不出现。
- 密钥:secrets 文件权限 0600;配置里只引用密钥名;`omp_add` 私聊录入的主机/密码是已定流程,存入 data/ 下受限权限文件,不进日志与记忆本。
- 提交前检查:`uv run ruff check`、`uv run ruff format --check`、`uv run pyright`、`uv run pytest`。
- 仓库忽略:`data/`、`secrets*`、`.venv/`、`__pycache__/`、`*.sqlite*`。
