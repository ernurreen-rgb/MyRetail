# Локальное окружение ERPNext

ERPNext является внутренним ядром MVP MyRetail. Окружение основано на официальном Docker-образе `frappe/erpnext:v16.23.1` и запускает отдельный локальный сайт `myretail.localhost`.

## Требования

- Docker Desktop 4.78 или новее;
- WSL 2;
- свободный порт `8080`;
- локальный файл `.env`, созданный из `.env.example`.

На текущем рабочем компьютере Docker Desktop установлен в `E:\Docker\app`, а WSL-диски Docker находятся в `E:\Docker\wsl`.

## Запуск

```powershell
$env:Path = "E:\Docker\app\resources\bin;$env:Path"
docker compose --env-file infra/erpnext/.env -f infra/erpnext/compose.yaml up -d
```

После создания сайта откройте [http://myretail.localhost:8080](http://myretail.localhost:8080).

Логин администратора: `Administrator`. Пароль находится только в локальном файле `infra/erpnext/.env` и не должен попадать в Git или Notion.

## Сервисный доступ MyRetail API

После первого запуска создайте отдельного пользователя интеграции:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\infra\erpnext\scripts\bootstrap-api-user.ps1
```

Сценарий создаёт роль `MyRetail API Reader` с минимальными правами для товарного
каталога и складских операций: чтение складов и остатков, работа с товарами и ценами,
а также создание и проведение складских документов. Затем он создаёт пользователя
`myretail-api@local.test`, выпускает API-ключи и записывает их в игнорируемый файл
`services/api/.env`. Повторный запуск обновляет ключи. Секреты не выводятся в терминал.

Для локальной разработки можно создать тестовую компанию `MyRetail Demo` и товар `DEMO-001`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\infra\erpnext\scripts\setup-local-demo.ps1
```

Сценарий идемпотентен и не создаёт повторные записи. Тестовые данные не используются в production.

Для сквозной проверки Sprint 3 подготовьте отдельный набор складских QA-данных:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\infra\erpnext\scripts\setup-stock-qa-data.ps1
```

Сценарий создаёт два склада (`Основной склад QA` и `Резервный склад QA`), четыре товара
`QA-*`, начальные остатки, дробное количество весового товара и резерв `2.000` единицы
молока через отдельный тестовый заказ. Повторный запуск восстанавливает целевые остатки
и не дублирует справочники или активный заказ резервирования.

Сценарий также выставляет русский язык для системных настроек ERPNext и пользователя `Administrator`.
Если интерфейс уже был открыт в браузере, после запуска сценария выйдите из ERPNext и выполните жёсткое обновление страницы.

## Проверка

```powershell
docker compose --env-file infra/erpnext/.env -f infra/erpnext/compose.yaml ps
docker compose --env-file infra/erpnext/.env -f infra/erpnext/compose.yaml logs create-site
```

## Остановка

```powershell
docker compose --env-file infra/erpnext/.env -f infra/erpnext/compose.yaml down
```

Команда `down` сохраняет данные в именованных томах. Не используйте `down --volumes`, если не требуется полностью удалить локальный сайт и базу данных.

## Требования безопасности

- Для MVP используется отдельный ERPNext site и база данных на каждого тенанта.
- MariaDB не публикуется на хост-машину.
- ERPNext доступен только через localhost в локальном окружении.
- Пароли, API-ключи, ключи шифрования и конфигурация сайтов не коммитятся.
- Клиентский трафик продукта должен проходить через MyRetail API.

## Следующие задачи

1. Проверить резервное копирование и восстановление локального сайта.
2. Добавить сценарии создания и сброса тестового тенанта.
3. Завершить первоначальную настройку тестовой компании ERPNext.
4. Добавить тестовые товары и проверить их отображение в веб-приложении.
