# Происхождение native-фикстуры

`repository-report-v1-v4.mxl` — **настоящий** файл, созданный конфигуратором 1С:Предприятие
`8.3.27.2130` на стенде Cerebro, не синтетический текст и не upstream-fixture.

| Поле | Значение |
|---|---|
| Команда | `1cv8.exe DESIGNER /F <verify-ib> /DisableStartupDialogs /DisableStartupMessages /ConfigurationRepositoryF <repository> /ConfigurationRepositoryN acceptance /ConfigurationRepositoryReport <файл> -NBegin 1 -NEnd 4` |
| Хранилище | `C:\gitsync-python-acceptance\20260915-122644-d223395b\repository` (синтетическое, создано для приёмки) |
| Пользователь хранилища | `acceptance`, пароль не задан (`/ConfigurationRepositoryP` отсутствует) |
| Исходное имя на стенде | `history-unbound.mxl` (ИБ **не** привязана к хранилищу) |
| Размер | 6787 байт |
| SHA256 | `5f941c82d7e8bee7178a69e58a5c54e647c685ed5a7e9cbb7bb06a8e89f055e7` |
| Формат | MOXCEL (табличный документ 1С в текстовой сериализации), UTF-8 с BOM после 12-байтового заголовка |

Содержимое — 4 версии: создание хранилища, добавление `ОбщийМодуль.AcceptanceProbe`,
изменение его модуля, удаление модуля. Подробности стенда: `native-stand.md` в каталоге
доказательств (вне продукта).

Расширение `.txt` формат отчёта **не** меняет: конфигуратор всё равно пишет MOXCEL.
Поэтому парсер продукта разбирает контейнер MOXCEL, а не выдуманную построчную разметку.
