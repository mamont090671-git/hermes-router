# Hermes Router

Умный прокси-роутер для LLM-бэкендов с автоматическим переключением между удалённым и локальным серверами.

## Назначение

Роутер направляет запросы к LLM-бэкенду (например, удалённый сервер с GPU) с автоматическим фоллбэком на локальный сервер при недоступности. Используется для обеспечения отказоустойчивости — если удалённый сервер недоступен, клиент продолжает работать через локальную модель без ручной перенастройки.

## Архитектура

```
Клиент (Hermes AI) → :9999 → hermes_router.py
                            ├── PRIMARY  :8585 (удалённый GPU-сервер) — основной
                            └── FALLBACK :8080 (локальная модель)     — резервный
```

## Возможности

- **Auto-failover** — мгновенное переключение на резервный бэкенд при сбое
- **Circuit Breaker** — экспоненциальный backoff health-check'ей (5с → 60с) для экономии ресурсов
- **Dual Health-Check** — проверка доступности обоих бэкендов каждые N секунд
- **Stream Proxy** — поддержка потоковой отдачи ответов (OpenAI-совместимые API)
- **Graceful Shutdown** — корректная остановка по сигналам

## Установка

### Требования

- Python 3.10+
- Flask 3.x
- requests 2.x

### Быстрый старт

```bash
cd hermes_router

# Создайте .env файл
cp .env.example .env

# Заполните .env вашими данными (см. ниже)

# Запуск
python3 hermes_router.py
```

### systemd сервис

```bash
# Скопируйте service-файл
sudo cp hermes-router.service /etc/systemd/system/

# Перезагрузите конфигурацию
sudo systemctl daemon-reload

# Включите и запустите
sudo systemctl enable hermes-router
sudo systemctl start hermes-router

# Проверка
sudo systemctl status hermes-router
```

## Настройка

### Environment Variables

| Переменная | По умолчанию | Обязательное | Описание |
|-----------|-------------|:----------:|----------|
| `HOME_SERVER` | — | **да** | URL основного бэкенда (например, `http://YOUR_SERVER:8585`) |
| `LOCAL_LAPTOP` | — | **да** | URL резервного бэкенда (например, `http://127.0.0.1:8080`) |
| `BASE_INTERVAL` | `5.0` | нет | Интервал health-check при онлайн, сек |
| `MAX_INTERVAL` | `60.0` | нет | Максимальный интервал при оффлайн, сек |
| `CONNECT_TIMEOUT` | `0.6` | нет | Таймаут установки соединения, сек |
| `READ_TIMEOUT` | `300` | нет | Таймаут чтения тела ответа, сек |
| `MAX_REQUEST_SIZE` | `10485760` | нет | Лимит тела запроса, байт (10 MB) |

### Настройка Hermes Agent

Чтобы Hermes использовал роутер как бэкенд, укажите его в конфиге модели:

```yaml
model:
  default: /llama/Qwen3.6.gguf
  provider: custom
  base_url: http://localhost:9999/v1
```

Порт **9999** — адрес роутера. Все запросы Hermes будут проходить через него и автоматически маршрутизироваться на primary или fallback бэкенд.

### .env.example

```
HOME_SERVER=http://YOUR_PRIMARY_SERVER:8585
LOCAL_LAPTOP=http://YOUR_FALLBACK_SERVER:8080
BASE_INTERVAL=5.0
MAX_INTERVAL=60.0
CONNECT_TIMEOUT=0.6
READ_TIMEOUT=300
MAX_REQUEST_SIZE=10485760
```

## API

Роутер слушает порт **9999** и пробрасывает все HTTP-методы на выбранный бэкенд.

```
GET/POST /v1/chat/completions  → primary или fallback
GET/POST /v1/models            → primary или fallback
GET/POST /api/*                → primary или fallback
```

Заголовки проксируются, кроме `host`.

## Поведение роутера

1. При старте запускается фоновый worker, проверяющий оба бэкенда
2. Если `HOME_SERVER` недоступен — запросы маршрутизируются на `LOCAL_LAPTOP`
3. При восстановлении `HOME_SERVER` — запросы возвращаются автоматически
4. При ответе 5xx/429 от `HOME_SERVER` — мгновенный fallback на локал
5. Если оба бэкенда мертвы — клиент получает `503` с заголовком `Retry-After: 5`
6. Circuit Breaker: при падении сервера health-check удваивает интервал (5 → 10 → 20 → 40 → 60 сек)

## Мониторинг и отладка

```bash
# Статус сервиса
sudo systemctl status hermes-router

# Логи health-check
sudo journalctl -u hermes-router | grep -i health

# Логи переключений
sudo journalctl -u hermes-router | grep -iE "online|offline|fallback"

# Все логи за последний час
sudo journalctl -u hermes-router --since "1 hour ago"

# Логи последней перезагрузки
sudo journalctl -u hermes-router -b
```

## Архитектурные ограничения

- `threaded=True` — подходит для <100 concurrent запросов. Для большей нагрузки используйте gunicorn.
- `CONNECT_TIMEOUT=0.6` — может давать ложные срабатывания при медленном VPN/tunnel соединении.
- Нет rate limiting — не предназначен для защиты от DDoS.
- Нет per-path health logic — `/v1/models` может отвечать, а `/v1/completions` — нет.

## Структура проекта

```
hermes_router/
├── README.md              # Этот файл
├── hermes_router.py       # Основной скрипт
├── requirements.txt       # Зависимости Python
├── hermes-router.service  # systemd unit file
├── .env.example           # Шаблон .env
└── .gitignore
```

## История версий

| Версия | Дата | Описание |
|--------|------|----------|
| 1.0 | — | Базовая версия с health-check per request |
| 2.0 | 2026-08-22 | Circuit breaker, graceful shutdown, env vars, dual backend check |
| 3.0 | 2026-08-22 | Flat error handling, OOM protection, URL parsing fix, Content-Type fallback |

## Лицензия

MIT
