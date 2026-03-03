# RequestProcessor Architecture

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                      RequestProcessor                            │
│                   (Main Orchestrator)                            │
│                                                                   │
│  • High-level request orchestration                             │
│  • Langfuse tracing integration                                 │
│  • Command execution coordination                               │
└───────────────────────┬─────────────────────────────────────────┘
                        │
                        │ delegates to
                        │
        ┌───────────────┼───────────────┬───────────────┐
        │               │               │               │
        ▼               ▼               ▼               ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ Repository   │ │     MCP      │ │Observability │ │  Claude SDK  │
│   Context    │ │Configuration │ │   Manager    │ │   Executor   │
│   Loader     │ │   Builder    │ │              │ │              │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
│              │ │              │ │              │ │              │
│ • Fetch      │ │ • Create MCP │ │ • Setup      │ │ • Execute    │
│   CLAUDE.md  │ │   config     │ │   Langfuse   │ │   SDK        │
│ • Rate       │ │ • Build      │ │   hooks      │ │ • Process    │
│   limiting   │ │   agent      │ │ • Error      │ │   messages   │
│ • Retry      │ │   options    │ │   reporting  │ │ • Vertex AI  │
│   logic      │ │              │ │              │ │ • Cleanup    │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
```

## Data Flow

```
1. Request arrives at RequestProcessor.process()
   ↓
2. Command execution via CommandRegistry
   ↓
3. RepositoryContextLoader.fetch_claude_md()
   ↓
4. MCPConfigurationBuilder.create_mcp_config()
   ↓
5. ObservabilityManager.setup_langfuse_hooks()
   ↓
6. MCPConfigurationBuilder.create_agent_options()
   ↓
7. ClaudeSDKExecutor.execute_sdk()
   ↓
8. Response returned to caller
```

## Dependency Graph

```
RequestProcessor
├── GitHubTokenManager (injected)
├── httpx.AsyncClient (injected)
├── Langfuse (injected, optional)
├── MultiRateLimiter (injected, optional)
├── HealthChecker (injected, optional)
│
├── RepositoryContextLoader (composed)
│   ├── GitHubTokenManager
│   ├── httpx.AsyncClient
│   └── MultiRateLimiter
│
├── MCPConfigurationBuilder (composed)
│   └── GitHubTokenManager
│
├── ObservabilityManager (composed)
│   └── (no dependencies)
│
└── ClaudeSDKExecutor (composed)
    ├── asyncio.Event (shutdown_event)
    └── MultiRateLimiter
```

## Responsibility Matrix

| Component               | Responsibilities                                   | External Dependencies     |
| ----------------------- | -------------------------------------------------- | ------------------------- |
| RequestProcessor        | Orchestration, Langfuse tracing, command execution | Langfuse, CommandRegistry |
| RepositoryContextLoader | Fetch CLAUDE.md, rate limiting, retry logic        | GitHub API, httpx         |
| MCPConfigurationBuilder | MCP config, agent options                          | Claude SDK types          |
| ObservabilityManager    | Langfuse hooks, error reporting                    | subprocess, asyncio       |
| ClaudeSDKExecutor       | SDK execution, message processing, Vertex AI setup | Claude SDK, Google Cloud  |

## Key Design Principles

### 1. Dependency Injection

All components receive their dependencies through constructors, enabling:

- Easy testing with mocks
- Flexible configuration
- Clear dependency visibility

### 2. Composition over Inheritance

RequestProcessor composes focused components rather than inheriting behavior:

- More flexible
- Easier to test
- Clearer responsibilities

### 3. Single Responsibility

Each component has one reason to change:

- RepositoryContextLoader: Repository context fetching logic
- MCPConfigurationBuilder: MCP configuration logic
- ObservabilityManager: Observability/hooks logic
- ClaudeSDKExecutor: SDK execution logic

### 4. Interface Segregation

Components expose minimal, focused interfaces:

- No unnecessary methods
- Clear contracts
- Easy to understand

### 5. Separation of Concerns

Clear boundaries between:

- Orchestration (RequestProcessor)
- Data fetching (RepositoryContextLoader)
- Configuration (MCPConfigurationBuilder)
- Observability (ObservabilityManager)
- Execution (ClaudeSDKExecutor)

## Testing Strategy

### Unit Tests

Each component can be tested independently:

```python
# Test RepositoryContextLoader
async def test_fetch_claude_md():
    mock_token_manager = Mock()
    mock_http_client = Mock()
    loader = RepositoryContextLoader(mock_token_manager, mock_http_client)
    result = await loader.fetch_claude_md("owner/repo")
    assert result == expected_content

# Test MCPConfigurationBuilder
async def test_create_mcp_config():
    mock_token_manager = Mock()
    builder = MCPConfigurationBuilder(mock_token_manager)
    config = await builder.create_mcp_config()
    assert "github" in config

# Test ObservabilityManager
def test_setup_langfuse_hooks():
    manager = ObservabilityManager()
    hooks = manager.setup_langfuse_hooks()
    assert "Stop" in hooks

# Test ClaudeSDKExecutor
async def test_execute_sdk():
    mock_event = asyncio.Event()
    executor = ClaudeSDKExecutor(mock_event)
    # Test execution logic
```

### Integration Tests

Test component interactions:

```python
async def test_request_processor_integration():
    processor = RequestProcessor(
        token_manager=real_token_manager,
        http_client=real_http_client,
        # ... other dependencies
    )
    response = await processor.process(
        repo="owner/repo",
        issue_number=1,
        command="test command",
        user="testuser"
    )
    assert response is not None
```

## Performance Considerations

1. **Async Operations**: All I/O operations are async to prevent blocking
2. **Rate Limiting**: Applied at appropriate boundaries (GitHub API, Anthropic API)
3. **Resource Cleanup**: Proper cleanup in finally blocks and cleanup methods
4. **Timeout Handling**: Timeouts at SDK execution level (30 minutes)

## Error Handling

Each component handles errors at its level:

- RepositoryContextLoader: Retries with exponential backoff
- MCPConfigurationBuilder: Configuration validation
- ObservabilityManager: Hook failures don't block execution
- ClaudeSDKExecutor: Timeout and initialization errors

## Future Enhancements

1. **Metrics Collection**: Add metrics for each component
2. **Circuit Breaker**: Add circuit breaker for external API calls
3. **Caching**: Cache CLAUDE.md and MCP configs
4. **Health Checks**: Add health check endpoints for each component
5. **Configuration Validation**: Add schema validation for configurations
