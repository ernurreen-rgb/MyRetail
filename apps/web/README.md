# MyRetail Web

Веб-клиент MyRetail на Next.js. Требования к продукту, архитектурные решения и текущий статус ведутся в Notion проекта.

## Разработка

Из корня репозитория запустите:

```powershell
npm.cmd run dev:web
```

Откройте [http://localhost:3000](http://localhost:3000). Health endpoint веб-приложения доступен по адресу [http://localhost:3000/api/health](http://localhost:3000/api/health).

Если адрес API отличается от локального значения по умолчанию, скопируйте `.env.example` в `.env.local` и измените `MYRETAIL_API_URL`.

## Команды

```powershell
npm.cmd run lint --workspace @myretail/web
npm.cmd run typecheck --workspace @myretail/web
npm.cmd run build --workspace @myretail/web
```
