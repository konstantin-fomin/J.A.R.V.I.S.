#!/usr/bin/env python3
"""Однократная генерация token.json для Google Calendar.

ВАЖНО: запускать НЕ на VPS, а на машине с браузером (ноутбук/десктоп). Скрипт
не часть бота — это разовая авторизация. Поток InstalledAppFlow.run_local_server()
поднимает локальный сервер, открывает браузер для согласия и сохраняет
рефреш-токен в token.json. Полученный token.json (и credentials.json) переносятся
на VPS рядом с кодом. См. JARVIS_SPEC.md §9.

Подготовка (один раз):
  1. Google Cloud Console → включить Google Calendar API.
  2. Создать OAuth client типа «Desktop app», скачать как credentials.json.
  3. Положить credentials.json рядом с этим скриптом.

Запуск:
  pip install google-auth-oauthlib
  python generate_calendar_token.py
  (по желанию: python generate_calendar_token.py --credentials path --token path)
"""
import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

# Тот же scope, что использует бот (calendar_client.SCOPES): чтение и запись.
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Сгенерировать token.json для Google Calendar")
    parser.add_argument("--credentials", default=str(here / "credentials.json"),
                        help="путь к credentials.json (OAuth client, Desktop app)")
    parser.add_argument("--token", default=str(here / "token.json"),
                        help="куда сохранить token.json")
    args = parser.parse_args()

    creds_path = Path(args.credentials)
    if not creds_path.exists():
        raise SystemExit(
            f"Не найден {creds_path}.\n"
            "Скачай OAuth client (Desktop app) из Google Cloud Console и положи его сюда."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    # Откроет браузер; после согласия вернёт credentials с refresh-токеном.
    creds = flow.run_local_server(port=0)

    token_path = Path(args.token)
    token_path.write_text(creds.to_json())
    print(f"✅ Готово: {token_path}")
    print("Перенеси token.json на VPS рядом с кодом бота и перезапусти его.")


if __name__ == "__main__":
    main()
