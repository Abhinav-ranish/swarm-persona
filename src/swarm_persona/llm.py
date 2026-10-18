"""Model providers. One chat model drives the personas (browser-use), and the same provider answers
the planner and the merge pass as plain text."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .config import LLM


def chat_model(cfg: LLM, max_tokens: int = 8000):
    if cfg.provider == 'bedrock':
        return bedrock_model(cfg, max_tokens)
    key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
    if cfg.api_key_env and not key:
        raise SystemExit(f'{cfg.api_key_env} is not set')
    if cfg.provider == 'anthropic':
        from browser_use.llm.anthropic.chat import ChatAnthropic

        return ChatAnthropic(model=cfg.model, api_key=key, max_tokens=max_tokens,
                             **({'base_url': cfg.base_url} if cfg.base_url else {}))
    from browser_use.llm.openai.chat import ChatOpenAI

    return ChatOpenAI(model=cfg.model, api_key=key, base_url=cfg.base_url, max_completion_tokens=max_tokens)


def bedrock_model(cfg: LLM, max_tokens: int):
    """browser-use asks for its next action through a tool call but never sets toolChoice, and some
    Bedrock models (Kimi K3 among them) often answer in plain text instead, which browser-use
    rejects with "Expected structured output but no tool use found". This model forces the tool,
    and if a reply still arrives as text it retries once unforced and salvages the JSON object."""
    try:
        import boto3
    except ImportError as error:
        raise SystemExit('provider bedrock needs boto3: pip install "swarm-persona[bedrock]"') from error
    from browser_use.llm.aws.chat_bedrock import ChatAWSBedrock
    from browser_use.llm.exceptions import ModelProviderError
    from browser_use.llm.views import ChatInvokeCompletion

    class ChatBedrockForcedTool(ChatAWSBedrock):
        async def ainvoke(self, messages, output_format=None, **kwargs: Any):
            if output_format is None:
                return await super().ainvoke(messages, None, **kwargs)
            tools = self._format_tools_for_request(output_format)
            name = tools[0]['toolSpec']['name']
            base = {k: v for k, v in (self.request_params or {}).items() if k != 'toolConfig'}
            self.request_params = {**base, 'toolConfig': {'tools': tools, 'toolChoice': {'tool': {'name': name}}}}
            try:
                return await super().ainvoke(messages, output_format, **kwargs)
            except ModelProviderError as e:
                if 'no tool use' not in str(e):
                    raise
                self.request_params = base
                text = (await super().ainvoke(messages, None, **kwargs)).completion
                match = re.search(r'\{.*\}', text, re.S)
                if not match:
                    raise
                return ChatInvokeCompletion(completion=output_format.model_validate(json.loads(match.group(0))),
                                            usage=None)
            finally:
                self.request_params = base

    region = cfg.region or os.environ.get('AWS_REGION') or 'us-east-2'
    return ChatBedrockForcedTool(model=cfg.model, aws_region=region, max_tokens=max_tokens,
                                 session=boto3.Session(region_name=region))


async def complete(cfg: LLM, prompt: str, max_tokens: int = 12000) -> tuple[str, int]:
    """Plain text from the configured model, and the tokens it cost (0 when not reported)."""
    from browser_use.llm.messages import UserMessage

    reply = await chat_model(cfg, max_tokens).ainvoke([UserMessage(content=prompt)])
    usage = reply.usage
    return str(reply.completion), (usage.prompt_tokens + usage.completion_tokens) if usage else 0


def extract_json(text: str):
    """The first JSON value in a model reply, fenced or not."""
    match = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    body = match.group(1) if match else text
    starts = [i for i in (body.find('['), body.find('{')) if i >= 0]
    if not starts:
        raise ValueError('no JSON in the reply')
    value, _ = json.JSONDecoder().raw_decode(body[min(starts):])
    return value
