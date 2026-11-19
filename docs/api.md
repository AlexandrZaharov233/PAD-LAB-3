# API ParkSlot

Адрес через Gateway: `http://127.0.0.1:8081` в Docker Compose или `http://127.0.0.1:8082` в Minikube после запуска `open-k8s.bat`. Формат JSON, кодировка UTF-8. Подробные схемы и ограничения полей доступны в Swagger каждого сервиса.

Для защищённых запросов передавайте `Authorization: Bearer TOKEN`. Дополнительно можно передать `X-Request-ID` длиной 8–80 латинских букв, цифр, дефисов и подчёркиваний. Сервер возвращает его в заголовке; некорректное значение заменяется новым UUID.

## Маршруты

| Метод и путь Gateway | Назначение | Доступ |
| --- | --- | --- |
| POST `/api/auth/register` | Регистрация, получение токена | Все |
| POST `/api/auth/login` | Вход, получение токена | Все |
| GET `/api/auth/me` | Данные учётной записи | Вошедший пользователь |
| GET/PATCH `/api/profiles/me` | Просмотр/изменение профиля | Владелец |
| GET/POST `/api/profiles/me/vehicles` | Список/добавление авто | Владелец |
| GET/DELETE `/api/profiles/me/vehicles/{id}` | Просмотр/удаление авто | Владелец |
| GET `/api/parking/lots` | Каталог парковок | Все |
| GET `/api/parking/lots/{id}` | Парковка | Все |
| GET `/api/parking/lots/{id}/spaces` | Список мест | Все |
| GET `/api/parking/spaces/{id}` | Место и тариф | Все |
| POST `/api/parking/lots` | Создание парковки с местами | Администратор |
| PATCH `/api/parking/lots/{id}` | Тариф и активность парковки | Администратор |
| PATCH `/api/parking/spaces/{id}` | Доступность места | Администратор |
| GET `/api/bookings/availability` | Доступность за интервал | Все |
| POST `/api/bookings` | Создание брони | Вошедший пользователь |
| GET `/api/bookings` | Свои бронирования, до 200 | Владелец |
| GET `/api/bookings/{id}` | Подробности брони | Владелец / администратор |
| POST `/api/bookings/{id}/cancel` | Отмена | Владелец / администратор |
| GET `/api/bookings/admin/all` | Все брони, до 500 | Администратор |
| POST `/api/payments` | Учебная оплата | Владелец брони |
| GET `/api/payments` | Свои платежи, до 200 | Владелец |
| GET `/api/payments/admin/all` | Все платежи, до 500 | Администратор |
| GET `/api/notifications` | Свои уведомления, до 200 | Владелец |
| POST `/api/notifications/{id}/read` | Прочтение | Владелец |
| GET `/api/promotions` | Промокоды и счётчики использований | Администратор |
| POST `/api/promotions` | Создать промокод с условиями | Администратор |
| PATCH `/api/promotions/{code}` | Включить или выключить код | Администратор |
| POST `/api/promotions/quote` | Предварительно рассчитать скидку | Вошедший пользователь |
| GET/POST `/api/waitlist` | Список своих заявок / записаться в очередь | Владелец |
| POST `/api/waitlist/{id}/cancel` | Отказаться от заявки или предложения | Владелец |
| GET/POST `/api/maintenance` | Список и создание интервалов работ | Администратор |
| POST `/api/maintenance/{id}/cancel` | Отменить работы и открыть место | Администратор |
| GET `/api/reports/summary` | Сводка за `start_at` — `end_at` | Администратор |
| GET `/api/status` | Готовность десяти сервисов | Все |

Прямой API сервиса использует тот же путь без `/api`, например `http://127.0.0.1:18014/bookings`. У каждого сервиса свои `/health`, `/ready`, `/docs`, `/openapi.json`. Gateway: `/health` и `/ready` на выбранном порту. В Minikube внутренние API доступны по имени Service из кластера.

## Примеры тел запросов

Регистрация:

```json
{"email":"student@example.test","password":"My-parking-password-123","name":"Александр"}
```

Добавление автомобиля:

```json
{"plate":"ABC 123","label":"Мой автомобиль"}
```

Бронь: идентификаторы берутся из API автомобилей и парковок. Время замените на будущее.

```json
{
  "space_id":"UUID места",
  "vehicle_id":"UUID автомобиля",
  "start_at":"2030-06-10T10:00:00+03:00",
  "end_at":"2030-06-10T12:00:00+03:00",
  "idempotency_key":"уникальный-ключ-запроса-001",
  "promo_code":"PARK10"
}
```

`promo_code` — необязательное поле. Итоговую стоимость рассчитывает сервер.

Проверка промокода: `POST /api/promotions/quote` с `{"code":"PARK10","lot_id":"UUID парковки","base_amount_cents":6000}`. Ответ содержит `discount_cents` и `amount_cents`. В бронировании скидка рассчитывается заново по тарифу места.

Лист ожидания принимает `lot_id`, `start_at`, `end_at`. Для планирования работ передайте `space_id`, `start_at`, `end_at`, `reason` и уникальный `idempotency_key`. Для дат в API указывайте часовой пояс.

Оплата:

```json
{"booking_id":"UUID брони","idempotency_key":"уникальный-ключ-платежа-001","outcome":"success"}
```

`outcome` принимает `success` или `decline`. Сумма определяется по брони. Клиент не может передать собственную сумму. Повтор того же запроса с тем же ключом возвращает ту же сущность и код 201. При изменении тела с прежним ключом - 409.

Создание парковки администратором:

```json
{"name":"Учебная парковка","address":"Учебная улица, 12","hourly_rate_cents":3000,"capacity":12}
```

## Проверка в PowerShell

```powershell
$baseUrl = 'http://127.0.0.1:8082' # либо 8081 для Compose
Invoke-RestMethod "$baseUrl/api/status"
$loginBody = @{email='student@example.test'; password='My-parking-password-123'} | ConvertTo-Json
$login = Invoke-RestMethod "$baseUrl/api/auth/login" -Method Post -ContentType 'application/json' -Body $loginBody
$headers = @{Authorization="Bearer $($login.access_token)"}
Invoke-RestMethod "$baseUrl/api/bookings" -Headers $headers
Invoke-RestMethod "$baseUrl/api/payments" -Headers $headers
Invoke-RestMethod "$baseUrl/api/notifications" -Headers $headers
```

## Ошибки

```json
{
  "error": {
    "code":"SPACE_OCCUPIED",
    "message":"Это место уже забронировано на выбранное время",
    "request_id":"c97fe30c-596a-42bf-b587-ad4774dc49d9"
  }
}
```

| HTTP | Примеры |
| --- | --- |
| 401 | Нет токена, неверный пароль, просроченный токен |
| 403 | Пользователь пытается изменить тариф |
| 404 | Ресурс отсутствует или принадлежит другому пользователю |
| 409 | Пересечение броней, повторная оплата, конфликт ключа, истёкшая бронь |
| 413 | Запрос к Gateway больше 64 KiB |
| 422 | Неверные поля, время без пояса, недопустимая длительность |
| 429 | Слишком много попыток входа |
| 503 | Недоступны сервис, БД или брокер при проверке готовности |
| 504 | Истекло ожидание ответа зависимого сервиса |

Для технической диагностики используйте Request ID в `docker compose logs`. Не отправляйте пароль, токен или содержимое `.env` вместе с логами.
