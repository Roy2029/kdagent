"""DeepSeek 直连连通性验证（I5 / M1 前置）。

用法：uv run python scripts/probe_deepseek.py

读取 .env 的 DEEPSEEK_API_KEY / DEEPSEEK_BASEURL，发送一次最小对话请求，
打印响应状态、模型、首条回复与 usage。key 不落盘、不打印。
"""

from __future__ import annotations

from pathlib import Path

import httpx


def load_env(path: Path = Path(".env")) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def main() -> None:
    env = load_env()
    api_key = env.get("DEEPSEEK_API_KEY")
    base_url = env.get("DEEPSEEK_BASEURL", "https://api.deepseek.com/v1")
    if not api_key:
        raise SystemExit(".env 缺少 DEEPSEEK_API_KEY")

    url = f"{base_url}/chat/completions"
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "请只回复三个字：连接正常"}],
        "max_tokens": 32,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    print(
        f"HTTP {resp.status_code} | model={data.get('model')} "
        f"| prompt_tokens={usage.get('prompt_tokens')} "
        f"| completion_tokens={usage.get('completion_tokens')}"
    )
    print(f"回复：{content.strip()}")


if __name__ == "__main__":
    main()
