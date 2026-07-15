# MyRetail Web

Веб-клиент MyRetail на Next.js. Требования к продукту, архитектурные решения и текущий статус ведутся в Notion проекта.

## Разработка

Из корня репозитория запустите:

```powershell
npm.cmd run dev:web
```

Откройте [http://localhost:3000](http://localhost:3000). Health endpoint веб-приложения доступен по адресу [http://localhost:3000/api/health](http://localhost:3000/api/health).

Если адрес API отличается от локального значения по умолчанию, скопируйте `.env.example` в `.env.local` и измените `MYRETAIL_API_URL`.

`MYRETAIL_API_URL` должен быть абсолютным HTTP(S)-адресом без credentials, query string или fragment. Допустим безопасный base path, например `https://internal.example/myretail-api`; frontend добавляет только проверенные относительные API endpoints и завершает запрос fail closed при небезопасной конфигурации.

## Команды

```powershell
npm.cmd run lint --workspace @myretail/web
npm.cmd run typecheck --workspace @myretail/web
npm.cmd run build --workspace @myretail/web
```
