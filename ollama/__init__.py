import json
from typing import Any, Dict, List, Optional

import requests


class Client:
    def __init__(self, host: str = "http://localhost:11434"):
        self.host = host.rstrip("/")

    def chat(
        self,
        *,
        model: str,
        messages: List[Dict[str, str]],
        stream: bool = False,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        if options:
            payload["options"] = options

        response = requests.post(f"{self.host}/api/chat", json=payload, timeout=90)
        response.raise_for_status()
        if hasattr(response, "json"):
            return response.json()
        # Fallback for unusual responses
        return json.loads(response.text)
