# .NET SDK

**What Captain governs:** model calls from your own .NET 8 applications.

## Before you start
- A **Captain key** for the application, kept in your secret store.
- The package from your platform team (`sdk/dotnet`, `Stixis.Captain.Sdk`).

## Use
```csharp
using Stixis.Captain.Sdk;

var captain = new CaptainClient(Environment.GetEnvironmentVariable("CAPTAIN_KEY")!, "{{GATEWAY_URL}}");
var r = await captain.ChatAsync("claude-sonnet", new[] { new ChatMessage("user", "Hello") });
Console.WriteLine($"{r.Text} {r.Decision}");
```

## Test it
Run it. **Expected:** an answer; **Activity** shows the request under your application.
