# Authentication

EchoPath keeps authentication outside the public folder.

For bridge routing, use the selected backend's existing local auth or environment variables. For direct API routing, store credentials in the environment:

```json
{
  "model_routing": {
    "mode": "api"
  },
  "openai": {
    "auth_source": "environment"
  }
}
```

Do not store real credentials in this public folder.
