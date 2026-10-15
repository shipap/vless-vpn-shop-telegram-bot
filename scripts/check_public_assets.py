"""Проверка попадания известных локальных секретов в публичные исходники и сборки."""

import json
from pathlib import Path

from dotenv import dotenv_values


def main():
    root = Path(__file__).resolve().parents[1]
    values = dotenv_values(root / ".env")
    secrets = {
        str(value).encode()
        for key, value in values.items()
        if value
        and any(word in key for word in ("SECRET", "TOKEN", "PASSWORD", "API_KEY"))
        and len(str(value)) >= 8
    }
    for value in json.loads(values.get("SUNRISE_ADMIN_TOTP_SECRETS") or "{}").values():
        if isinstance(value, str) and len(value) >= 8:
            secrets.add(value.encode())
    for filename in ("owner-access.json", "local-resources.json"):
        file = root / ".private" / filename
        if file.exists():
            data = json.loads(file.read_text(encoding="utf-8-sig"))
            for key in ("totp_secret", "password", "redis_password"):
                if data.get(key):
                    secrets.add(str(data[key]).encode())
    examined = 0
    failures = []
    for folder in ("apps/web", "packages/web-ui", "packages/api-client", ".artifacts/web"):
        for path in (root / folder).rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".js",
                ".ts",
                ".tsx",
                ".json",
                ".html",
                ".css",
                ".svg",
                ".map",
            }:
                continue
            examined += 1
            content = path.read_bytes()
            if any(value in content for value in secrets):
                failures.append(path.relative_to(root).as_posix())
    if failures:
        raise SystemExit("FAIL: известный локальный секрет обнаружен в: " + ", ".join(failures))
    print(
        f"PASS: {examined} публичных файлов проверены на известные локальные секреты; значения не выводились."
    )


if __name__ == "__main__":
    main()
