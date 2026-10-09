# articles-report

Помесячная выгрузка данных по разделу `/articles/` сайта psbank.ru из Яндекс Метрики.

## Метрики

- Визиты из органического поиска
- Визиты из ИИ-источников (28 доменов + 21 utm_source)
- Переходы в продуктовый контур (CTA)
- Заявки (отправка формы)

## Запуск

```bash
# Первый раз — получить OAuth-токен
python3 ym_articles_report.py --auth

# Выгрузка данных
python3 ym_articles_report.py

# Диагностика ИИ-источников
python3 ym_articles_report.py --diag-ai
```

Результат — xlsx-файл в той же папке.
