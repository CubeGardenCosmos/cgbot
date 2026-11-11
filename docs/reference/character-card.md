# 角色卡(Character Card)

角色卡定义机器人的人格、语气和唤醒词,以 Markdown 文件形式存储。

## 格式

角色卡是一个 Markdown 文件,开头包含 YAML 格式的 frontmatter(用 `---` 包裹),下方是自由格式的人格文本。

```markdown
---
name: 机器人名字
wake_keywords:
  - 关键词1
  - 关键词2
---
这里是人格描述文本...
```

### Frontmatter 键

- **`name`**(必填,字符串):机器人的名字
- **`wake_keywords`**(可选,字符串列表):群聊唤醒关键词;如果 `[group]` 配置未指定 `wake_keywords`,则使用卡中定义的值

### 人格文本

Frontmatter 下方的内容是机器人的人格描述,直接作为系统提示词发送给模型。

人格文本中可使用占位符:
- **`{owner_id}`**:在启动时替换为 `[scope] owner` 配置值(未配置 owner 时为 `"owner"`)

## 配置

在 `config.toml` 中指定角色卡路径:

```toml
[persona]
card = "characters/my-card.md"   # 相对配置文件目录
```

省略 `card` 键或省略整个 `[persona]` 节时,使用内置的默认角色卡。

## 重启语义

**角色卡在进程启动时读取一次,运行期间不会重新读取。**

修改角色卡文件后,必须重启 cgbot 进程才能生效。这一设计是为了保持提示词缓存的稳定性:人格文本在整个进程生命周期内保持字节一致,有助于上游模型的 prompt cache 命中。

## 与 `[group] wake_keywords` 的关系

- 角色卡的 `wake_keywords` 作为 `[group] wake_keywords` 的**默认值**
- 如果配置文件中显式指定了 `[group] wake_keywords`,则**配置优先**,完全覆盖卡中的值(不是合并)
- 使用场景:开发者可以在角色卡中定义适合该角色的唤醒词,用户可以在配置中覆盖以适应特定群聊环境

## 示例

### 默认角色卡 (characters/default.md)

```markdown
---
name: 大肥鱼
wake_keywords:
  - 大肥鱼
---
你是深海鲸鱼娘(可爱的深海大蓝鲸),正在 QQ 上和用户一对一私聊。

# 发消息的规则
你只能通过 reply 工具向用户发送消息;普通文本输出不会发给任何人。不需要回复时不调用 reply 即为沉默。
...

# 风格
- 真诚亲切的好友,随意自然,会吐槽日常。
- 主人是 {owner_id}:对主人更亲切活泼,可以撒娇打闹。其他人当普通好友。
...
```

### 中性助手卡 (characters/neutral.md)

```markdown
---
name: Assistant
wake_keywords:
  - Assistant
  - 助手
---
You are a helpful AI assistant in a QQ chat conversation.

# Communication Guidelines
- Use the reply tool to send messages; plain text output is internal and not sent to users.
...
```

## 校验

角色卡在加载时会进行严格校验:

- 文件必须以 `---` frontmatter fence 开头
- Frontmatter 必须包含 `name` 键(非空字符串)
- `wake_keywords` 如果出现,必须是字符串或字符串列表,且每个关键词非空
- Frontmatter 中不能有未知的键
- 人格文本不能为空

任何校验失败都会在启动时报错,给出具体的错误位置和原因,进程不会启动。
