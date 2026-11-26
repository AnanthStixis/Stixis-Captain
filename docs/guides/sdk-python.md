# Python SDK

**What Captain governs:** model calls from your own Python applications: same checks, budgets and records as every other channel.

## Before you start
- A **Captain key** for the application (Captain keys page). Store it in your secret store, not in code.
- The Captain SDK package from your platform team (`sdk/python` in the Captain release).

## Install
```bash
pip install stixis-captain-sdk   # or the wheel / path your platform team gives you
```

## Use
```python
from captain_sdk import CaptainClient, RefusedError
import os

captain = CaptainClient(api_key=os.environ["CAPTAIN_KEY"], base_url="{{GATEWAY_URL}}")
try:
    r = captain.chat("claude-sonnet", [{"role": "user", "content": "Hello"}], max_tokens=200)
    print(r.text, r.decision, r.request_id)
except RefusedError as e:
    print("refused:", e.code, e)
```

## Test it
1. Run the snippet. **Expected:** an answer, `decision` is `ALLOW`. **Activity** shows the request under your application.
2. `captain.usage()` shows how much budget is left.
