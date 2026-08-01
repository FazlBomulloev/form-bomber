import re
from dataclasses import dataclass
from typing import Optional

@dataclass
class FormContext:
    html: str
    source: str
    trigger_href: Optional[str] = None
    trigger_text: Optional[str] = None
    frame: object = None

def domain_from_url(url: str) -> str:
    m = re.match(r'https?://([^/]+)', url)
    if not m:
        return url
    host = m.group(1).split(':')[0]
    try:
        return host.encode('idna').decode('ascii').lower()
    except (UnicodeError, UnicodeDecodeError):
        return host.lower()
