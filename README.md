# cgbot

[中文](./README_zh.md) | English

An LLM-native QQ bot framework with conversation context management, multi-agent architecture, and per-user sandboxed coding environments powered by Codex.

## Overview

**cgbot** is a Python-based chat bot framework designed around large language models (LLMs) as the core control loop. Unlike traditional command-based bots, cgbot treats LLM interaction as first-class, with proper context budgeting, compression, and persistence inspired by systems like Codex and Oh My Pi.

### Key Features

- **LLM-Native Architecture**: Conversations driven by LLM reasoning, not commands
- **Unified User Identity**: Cross-platform identity system (QQ now, WeChat planned)
- **Context Management**: Automatic compression at 50% window capacity or after 8 hours idle
- **Reply-as-Tool**: Model outputs through explicit reply tools with channel selection
- **Private & Group Chat**: Separate logic paths with strict privacy isolation
- **Codex Integration**: Per-user Docker sandboxes for multi-agent coding tasks
- **Configuration-Driven**: TOML-based config, no WebUI
- **Security Model**: Zero-trust sandboxes with secret injection via gateway, controlled egress

### Architecture

```mermaid
flowchart TB
    subgraph External
        NapCat[NapCat<br/>QQ Gateway]
        User[QQ Users]
    end
    
    subgraph cgbot
        WS[WebSocket<br/>Adapter]
        Identity[Identity &<br/>Session]
        Agent[Agent Loop]
        Provider[LLM Provider<br/>Layer]
        Storage[Context<br/>Storage]
        Memory[Memory<br/>Notebook]
    end
    
    subgraph Caged Stack
        Proxy[Docker Proxy<br/>HAProxy]
        Egress[Egress Proxy<br/>Mihomo]
        Sandbox[User Sandboxes<br/>Codex Runtime]
    end
    
    User <-->|OneBot v11| NapCat
    NapCat <-->|WebSocket| WS
    WS --> Identity
    Identity --> Agent
    Agent <--> Provider
    Agent <--> Storage
    Agent <--> Memory
    Agent -->|codex_*| Proxy
    Proxy --> Sandbox
    Sandbox -->|HTTP| Egress

### Connection Types

- **QQ** (via [NapCat](https://github.com/NapNeko/NapCatQQ)): Primary interface using OneBot v11 reverse WebSocket
- **WeChat** (via OpenClaw): Planned for future releases

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- Running [caged stack](caged/README.md) for sandbox features
- LLM API access (see [Sponsor](#sponsor) for a free option)

### Installation

```bash
# Clone repository
git clone https://github.com/CubeGardenCosmos/cgbot.git
cd cgbot

# Install dependencies
uv sync

# Copy and edit configuration
cp config.example.toml config.toml
# Edit config.toml with your settings
```

### Configuration

See `config.example.toml` for detailed configuration options. Key sections:

- `[napcat]`: NapCat WebSocket connection
- `[provider]`: LLM provider settings (API keys in secrets.toml)
  - The template uses Cicori AI's OpenAI/Responses-compatible endpoint (`api.cicori.com`)
  - You can use any OpenAI-compatible provider by changing `base_url`
- `[persona]`: Character card configuration (see `docs/reference/character-card.md`)
- `[agent]`: Agent behavior and reasoning levels
- `[storage]`: Session persistence paths
- `[coding]`: Codex sandbox integration

### Running

```bash
# Development
uv run python -m cgbot

# With secrets file
uv run python -m cgbot --secrets secrets.toml
```

### Testing

```bash
# Run unit tests
uv run pytest

# Include integration tests (requires real services)
uv run pytest -m integration
```

## Deployment

For production deployment with Docker:

1. Review [deployment guide](docs/reference/deploy.md) (single source of truth)
2. Set up [caged container stack](caged/README.md)
3. Configure networks and secrets per deployment guide
4. Use `deploy/docker-compose.yml` and `deploy/deploy.sh`

**Important**: See [AGENTS.md deployment rules](AGENTS.md#部署) for authorization requirements and secrets handling.

See [`caged/README.md`](caged/README.md) for details on the container stack (Docker API allowlist proxy, egress proxy, sandbox image).

## Security Model

- **Identity Isolation**: Private chats and memory isolated from group chats
- **Reply-as-Tool**: Model cannot output directly; must call reply tools
- **Secrets by Reference**: Credentials injected at gateway, never visible to sandboxes
- **Sandbox Zero-Trust**: No docker.sock access, controlled egress, per-user volumes
- **Privacy Rules**: User private data never flows to group contexts

## Sponsor

cgbot is sponsored by [Cicori AI](https://api.cicori.com/).

Sign up via GitHub to receive a free weekly US$5 DeepSeek API credit.

## Project Status

**Alpha**: Core framework implemented (M1), private chat features in progress (M2). Group chat (M3), voice (M4), and full Codex integration (M5) planned.

See [MILESTONE.md](docs/MILESTONE.md) and [roadmaps](docs/roadmaps/) for detailed status.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.

## License

GPL-3.0-or-later License - see [LICENSE](LICENSE) for details.

## Documentation

- [Design Document](docs/DESIGN.md) (Chinese)
- [Architecture & Decisions](docs/README.md) (Chinese)
- [Agent Collaboration](AGENTS.md) (Chinese)
- [Deployment Reference](docs/reference/deploy.md) (Chinese)
- [Sandbox Image](docs/reference/sandbox-image.md) (Chinese)
