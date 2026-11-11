# Contributing to cgbot

Thank you for your interest in contributing to cgbot!

## Development Setup

1. Fork and clone the repository
2. Install Python 3.12+ and [uv](https://github.com/astral-sh/uv)
3. Run `uv sync` to install dependencies
4. Copy `config.example.toml` to `config.toml` and configure for testing

## Code Style

- Follow PEP 8 conventions
- Use `ruff` for linting: `uv run ruff check`
- Type hints required for public APIs
- Run `pyright` for type checking if available

## Testing

- Write unit tests for new features in `tests/`
- Unit tests should not require external services
- Mark integration tests with `@pytest.mark.integration`
- Run tests: `uv run pytest`
- Ensure tests pass before submitting PR

## Commit Guidelines

**Format**: `Prefix: 中文说明` (English prefix + Chinese description)

Common prefixes:
- `Feat`: New features
- `Fix`: Bug fixes
- `Refactor`: Code refactoring
- `Test`: Test additions/changes
- `Docs`: Documentation changes
- `Build`: Build system changes
- `Ci`: CI configuration
- `Chore`: Maintenance tasks

**Atomic Commits**: Each commit should address one clear problem or complete one logical change. Include implementation, tests, docs, and config together.

## Pull Request Process

1. Create a feature branch from `main`
2. Make atomic commits following guidelines
3. Ensure all tests pass
4. Update relevant documentation
5. Push to your fork and submit a PR
6. Describe changes, what was tested, and any limitations

## Documentation

- Update `docs/DESIGN.md` for design decisions (Chinese)
- Update README if adding user-facing features
- Keep docs in sync with code changes
- Validate internal links after changes

## Privacy and Security

- Never commit secrets, API keys, or credentials
- Use placeholders in examples (e.g., `your-api-key-here`)
- Review diffs before committing to avoid accidentally staging secrets
- Report security issues privately (see [SECURITY.md](SECURITY.md))

## Architecture Constraints

When contributing, please respect these core design principles:

- **Private and group chat are separate logic paths** - Do not mix concerns
- **Privacy isolation** - Private data must never flow to group contexts
- **Reply-as-tool** - Models output via explicit reply tools, not directly
- **Provider abstraction** - LLM provider must remain a swappable layer
- **No modifications to voice services** - SoVITS/ASR are external dependencies

## Questions?

- Check existing [issues](https://github.com/CubeGardenCosmos/cgbot/issues)
- Review [docs/DESIGN.md](docs/DESIGN.md) for design rationale
- Open a new issue for clarification

## License

By contributing, you agree that your contributions will be licensed under the GPL-3.0-or-later License.
