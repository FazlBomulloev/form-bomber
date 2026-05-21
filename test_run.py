"""Тестовый запуск на подмножестве проблемных сайтов."""
import asyncio
import json
from pathlib import Path
from db import db_init
from runner import check_site_v2

TEST_SITES = [
    "https://stomplaza.ru",
    "https://www.z-32.ru",
    "https://kremldenta.ru",
    "https://artlion.su",
    "https://lite-clinic.ru",
    "https://novikovski-stomatology.ru",
    "https://vindent.ru",
    "https://s2clinic.ru",
    "https://lets-smile.ru",
    "https://kdi-samara.ru",
]

PHONE = "+79991234567"
FIRSTNAME = "Тест"
LASTNAME = "Тестов"
PATRONYMIC = "Тестович"
EMAIL = "test@test.ru"
COMMENT = "Тестовая заявка"
CLAUDE_KEY = ""


async def main():
    await db_init()

    results = {}
    for url in TEST_SITES:
        print(f"\n{'='*60}")
        print(f"Тестируем: {url}")
        print(f"{'='*60}")
        try:
            r = await check_site_v2(
                url, PHONE,
                FIRSTNAME, LASTNAME, PATRONYMIC,
                EMAIL, COMMENT,
                claude_key=CLAUDE_KEY,
            )
            results[url] = {
                "status": r["status"],
                "method": r["method"],
                "message": r.get("message", ""),
                "reason_code": r.get("reason_code", ""),
            }
            icon = (
                "OK" if r["status"] == "success"
                else ("??" if r["status"] == "uncertain"
                      else "FAIL")
            )
            print(f"  [{icon}] {r['status']} | "
                  f"{r['method']} | {r.get('message','')[:80]}")
        except Exception as e:
            results[url] = {
                "status": "crash",
                "error": str(e)[:200],
            }
            print(f"  [CRASH] {e}")

    print(f"\n\n{'='*60}")
    print("ИТОГО:")
    print(f"{'='*60}")
    stats = {}
    for url, r in results.items():
        s = r["status"]
        stats[s] = stats.get(s, 0) + 1
        icon = {"success": "OK", "uncertain": "??",
                "failed": "FAIL", "captcha": "CAP",
                "crash": "ERR"}.get(s, s)
        print(f"  [{icon:4s}] {url:40s} "
              f"{r.get('method','')} "
              f"{r.get('message','')[:50]}")
    print(f"\nСтатистика: {json.dumps(stats)}")

    Path("data").mkdir(exist_ok=True)
    Path("data/test_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

if __name__ == "__main__":
    asyncio.run(main())
