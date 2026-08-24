"""Network-response capture — the block-resistant core.

Instead of hitting BookMyShow's internal endpoints ourselves (which get
fingerprinted and blocked, the AWS-WAF lesson from Zepto), we let the BMS page
make its OWN authenticated XHR/fetch calls and we snoop the JSON responses off
the wire. If BMS changes an endpoint path, this keeps working because we match
on payload shape, not URL.
"""
from __future__ import annotations

import json
from typing import Callable

from playwright.sync_api import Page, Response


class ResponseCollector:
    """Attach to a Page; collects JSON responses whose body matches a predicate."""

    def __init__(self, page: Page, keep: Callable[[str, dict], bool]):
        self.page = page
        self.keep = keep
        self.hits: list[dict] = []
        page.on("response", self._on_response)

    def _on_response(self, resp: Response) -> None:
        try:
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return
            body = resp.json()
        except Exception:
            return
        if not isinstance(body, (dict, list)):
            return
        try:
            if self.keep(resp.url, body):
                self.hits.append({"url": resp.url, "body": body})
        except Exception:
            return

    def clear(self) -> None:
        self.hits.clear()

    def detach(self) -> None:
        try:
            self.page.remove_listener("response", self._on_response)
        except Exception:
            pass


def deep_find(obj, key_names: set[str]):
    """Yield every value under any of key_names, anywhere in a nested json blob."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in key_names:
                yield v
            yield from deep_find(v, key_names)
    elif isinstance(obj, list):
        for item in obj:
            yield from deep_find(item, key_names)


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)[:2000]
