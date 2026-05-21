import contextvars
import json
import re
import time
import traceback as _tb
from datetime import datetime
from pathlib import Path
from typing import Optional


class SiteLogger:

    def __init__(
        self, domain: str, url: str, log_dir: Path
    ) -> None:
        safe = re.sub(r'[^a-zA-Z0-9._-]', '_', domain)
        self.domain = domain
        self.url = url
        self.site_dir = log_dir / safe
        self.site_dir.mkdir(parents=True, exist_ok=True)
        self._t0 = time.monotonic()
        self._runlog = self.site_dir / "run.log"
        self._actlog = self.site_dir / "actions.log"
        self._events: list = []
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._w(
            self._runlog,
            f"{'=' * 72}\n{ts}  {url}\n{'=' * 72}\n",
            mode="w",
        )

    def _t(self) -> float:
        return round(time.monotonic() - self._t0, 2)

    def _w(
        self, path: Path, text: str, mode: str = "a"
    ) -> None:
        try:
            with open(
                path, mode, encoding="utf-8",
                errors="replace",
            ) as f:
                f.write(text)
        except Exception:
            pass

    def _jdump(self, name: str, data: object) -> None:
        try:
            (self.site_dir / name).write_text(
                json.dumps(
                    data, ensure_ascii=False,
                    indent=2, default=str,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _ev(self, kind: str, **kw) -> None:
        self._events.append(
            {"t": self._t(), "kind": kind, **kw}
        )

    def step(self, name: str, msg: str = "", **kw):
        t = self._t()
        extra = (
            "  " + "  ".join(
                f"{k}={v}" for k, v in kw.items()
            )
        ) if kw else ""
        self._w(
            self._runlog,
            f"[{t:7.2f}s] ▶ {name}"
            f"{': ' + msg if msg else ''}{extra}\n",
        )
        self._ev("step", name=name, msg=msg, **kw)

    def ok(self, msg: str, **kw):
        t = self._t()
        extra = (
            "  " + "  ".join(
                f"{k}={v}" for k, v in kw.items()
            )
        ) if kw else ""
        self._w(
            self._runlog,
            f"[{t:7.2f}s] ✓ {msg}{extra}\n",
        )
        self._ev("ok", msg=msg, **kw)

    def warn(self, msg: str, **kw):
        t = self._t()
        extra = (
            "  " + "  ".join(
                f"{k}={v}" for k, v in kw.items()
            )
        ) if kw else ""
        self._w(
            self._runlog,
            f"[{t:7.2f}s] ⚠ {msg}{extra}\n",
        )
        self._ev("warn", msg=msg, **kw)

    def err(
        self, step: str, msg: str = "",
        exc: Exception = None,
    ):
        t = self._t()
        tb_str = ""
        if exc is not None:
            tb_str = "\n" + "".join(
                _tb.format_exception(
                    type(exc), exc, exc.__traceback__
                )
            )
        line = f"[{t:7.2f}s] ✗ {step}"
        if msg:
            line += f": {msg}"
        if exc:
            line += (
                f"  ({type(exc).__name__}: "
                f"{str(exc)[:120]})"
            )
        self._w(self._runlog, line + tb_str + "\n")
        self._ev(
            "error", step=step, msg=msg,
            exc=str(exc) if exc else None,
        )

    def log_ai(
        self, form_data: str, instructions: dict,
        tokens: int, provider: str = "",
        error: str = None,
    ):
        self._jdump("ai_response.json", {
            "timestamp": datetime.now().isoformat(),
            "provider": provider,
            "tokens_used": tokens,
            "form_data_len": len(form_data),
            "form_data_head": form_data[:600],
            "response": instructions,
            "error": error,
        })
        t = self._t()
        if error:
            self._w(
                self._runlog,
                f"[{t:7.2f}s] ✗ AI[{provider}] "
                f"ERROR: {error}\n",
            )
        else:
            r = instructions or {}
            actions = r.get("actions") or []
            self._w(self._runlog, (
                f"[{t:7.2f}s] ✓ AI[{provider}]: "
                f"tokens={tokens}  "
                f"form_found={r.get('form_found')}  "
                f"actions={len(actions)}\n"
            ))
        self._ev(
            "ai", tokens=tokens, provider=provider,
            form_found=(instructions or {}).get(
                "form_found"
            ),
            error=error,
        )

    def log_vision(
        self, stage: str, response: dict,
        screenshot_bytes: int = 0,
        error: str = None,
    ):
        self._jdump(f"vision_{stage}.json", {
            "timestamp": datetime.now().isoformat(),
            "stage": stage,
            "screenshot_bytes": screenshot_bytes,
            "response": response,
            "error": error,
        })
        t = self._t()
        if error:
            self._w(
                self._runlog,
                f"[{t:7.2f}s] ✗ Vision[{stage}] "
                f"ERROR: {error}\n",
            )
        else:
            r = response or {}
            self._w(self._runlog, (
                f"[{t:7.2f}s] ✓ Vision[{stage}]: "
                f"state={r.get('state')!r}  "
                f"notes={r.get('notes', '')!r}\n"
            ))
        self._ev(
            "vision", stage=stage, error=error,
        )

    def log_captcha(self, action: str, **kw):
        t = self._t()
        kv = "  ".join(
            f"{k}={str(v)[:80]}" for k, v in kw.items()
        )
        self._w(
            self._runlog,
            f"[{t:7.2f}s] 🔑 captcha[{action}] "
            f"{kv}\n",
        )
        self._ev(
            "captcha", action=action,
            **{k: str(v)[:80] for k, v in kw.items()},
        )

    def log_action(
        self, action: str, selector: str = "",
        value: str = "", success: bool = True,
        error: str = "",
    ):
        t = self._t()
        icon = "✓" if success else "✗"
        parts = [f"[{t:7.2f}s] {icon} [{action}]"]
        if selector:
            parts.append(f"sel={selector!r}")
        if value:
            parts.append(f"val={value[:60]!r}")
        if error:
            parts.append(f"err={error[:120]!r}")
        line = "  ".join(parts)
        self._w(self._actlog, line + "\n")
        if not success:
            self._w(self._runlog, line + "\n")
        self._ev(
            "action", action=action,
            selector=selector,
            success=success,
            error=error[:80] if error else "",
        )

    def log_shot(self, name: str, path_str: str):
        t = self._t()
        self._w(
            self._runlog,
            f"[{t:7.2f}s] 📷 {name} → {path_str}\n",
        )

    def finish(self, result: dict):
        t = self._t()
        status = result.get("status", "?")
        icon = (
            "✅" if status == "success"
            else ("⚠️" if status == "uncertain"
                  else "❌")
        )
        self._w(self._runlog, (
            f"\n{'─' * 72}\n"
            f"[{t:7.2f}s] {icon} ИТОГ: "
            f"status={status}  "
            f"method={result.get('method', '?')}\n"
            f"           "
            f"{result.get('message', '')[:150]}\n"
            f"{'─' * 72}\n"
        ))
        self._jdump("summary.json", {
            "domain": self.domain, "url": self.url,
            "duration_s": t, "result": result,
            "events": self._events,
        })


_site_logger_var: contextvars.ContextVar = (
    contextvars.ContextVar("_site_logger", default=None)
)


def get_logger() -> Optional[SiteLogger]:
    return _site_logger_var.get(None)
