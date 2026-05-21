import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class FormProfile:
    domain: str
    actions: list = field(default_factory=list)
    phone_selector: Optional[str] = None
    name_selector: Optional[str] = None
    email_selector: Optional[str] = None
    comment_selector: Optional[str] = None
    checkboxes: list = field(default_factory=list)
    dropdowns: list = field(default_factory=list)
    radio_groups: list = field(default_factory=list)
    submit_selector: Optional[str] = None
    form_selector: Optional[str] = None
    has_captcha: bool = False
    captcha_type: Optional[str] = None
    trigger_type: Optional[str] = None
    trigger_href: Optional[str] = None
    trigger_text: Optional[str] = None
    cookie_selector: Optional[str] = None
    success_texts: list = field(default_factory=list)
    error_texts: list = field(default_factory=list)
    notes: Optional[str] = None
    success_count: int = 0
    fail_count: int = 0
    tokens_used: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat()
    )


@dataclass
class FormContext:
    html: str
    source: str
    trigger_href: Optional[str] = None
    trigger_text: Optional[str] = None
    frame: object = None


_profiles_lock = asyncio.Lock()


def profiles_load() -> dict:
    from config import PROFILES_PATH
    Path("data").mkdir(exist_ok=True)
    p = Path(PROFILES_PATH)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {}


def profiles_save(profiles: dict):
    from config import PROFILES_PATH
    Path(PROFILES_PATH).write_text(
        json.dumps(
            profiles, ensure_ascii=False, indent=2
        ),
        "utf-8",
    )


async def profile_save_one(
    domain: str, profile_dict: dict
):
    async with _profiles_lock:
        profiles = profiles_load()
        profiles[domain] = profile_dict
        profiles_save(profiles)


def domain_from_url(url: str) -> str:
    m = re.match(r'https?://([^/]+)', url)
    return m.group(1) if m else url


def migrate_v1_to_actions(profile: FormProfile) -> list:
    actions = []
    step = 1
    if profile.phone_selector:
        actions.append({
            "step": step, "action": "fill",
            "field": "phone",
            "selector": profile.phone_selector,
            "value": "{phone}",
        })
        step += 1
    if profile.name_selector:
        actions.append({
            "step": step, "action": "fill",
            "field": "name",
            "selector": profile.name_selector,
            "value": "{name}",
        })
        step += 1
    if profile.email_selector:
        actions.append({
            "step": step, "action": "fill",
            "field": "email",
            "selector": profile.email_selector,
            "value": "{email}",
        })
        step += 1
    if profile.comment_selector:
        actions.append({
            "step": step, "action": "fill",
            "field": "comment",
            "selector": profile.comment_selector,
            "value": "{comment}",
        })
        step += 1
    for cb in (profile.checkboxes or []):
        if cb:
            actions.append({
                "step": step, "action": "click",
                "field": "checkbox", "selector": cb,
            })
            step += 1
    for dd in (profile.dropdowns or []):
        if dd.get("selector"):
            actions.append({
                "step": step, "action": "select_first",
                "field": "dropdown",
                "selector": dd["selector"],
                "type": dd.get("type", "native"),
                "option_selector": dd.get(
                    "option_selector"
                ),
            })
            step += 1
    for rg in (profile.radio_groups or []):
        sel = (
            rg.get("selector")
            if isinstance(rg, dict) else str(rg)
        )
        if sel:
            actions.append({
                "step": step, "action": "click",
                "field": "radio", "selector": sel,
            })
            step += 1
    if profile.submit_selector:
        actions.append({
            "step": step, "action": "submit",
            "field": "submit",
            "selector": profile.submit_selector,
        })
    return actions
