# Инструкция оператора RC1

## Подготовка

Рабочая платформа: Linux x86_64, Docker с NVIDIA Container Toolkit, рабочий
NVIDIA driver и доступ к privileged containers/netns/TAP. Проверенный стенд и
точные версии — в `ENVIRONMENT_AND_ASSETS.md`. На другой GPU/CPU нужно заново
измерить operating envelope; эти результаты не доказывают аппаратный real-time.

```bash
git clone https://github.com/AFETZ/bas_v2.git
cd bas_v2
git checkout release/bas-v2-rc1
make demo-preflight DEMO_GUI=0 DEMO_BOOTSTRAP=1
```

Если Town01 отсутствует, положите предоставленный CAVISE bundle локально,
укажите `CAVISE_MAPS_DIR` и повторите preflight. Используется существующий
`prepare_cavise_map.sh`; не нужен новый экспорт CARLA. Уже готовые assets находятся
в `.external/cavise_maps/Town01`. Для customer:

```bash
make prepare-customer
```

Это создаёт `.external/customer_10km/{scene.xml,customer.sdf,field.ply,field.obj,
reference_tower.ply,reference_tower.obj,geometry_summary.json}`. Shapely устанавливается
в отдельный pinned target. Геометрия не меняет каноническую Town01.

Для offline-восстановления из локального delivery package загрузите
исходники через `git clone --branch release/bas-v2-rc1 source.bundle bas_v2`, затем
`runtime-image.tar` командой `docker load -i runtime-image.tar`, распакуйте
`native-dependencies.tar.gz` и `scene-assets.tar.gz` в корень исходников, затем
выполните preflight и `make prepare-customer`. Gazebo derivatives/customer XML
содержат абсолютные runtime paths: после переноса их следует подготовить снова.
Архив dependencies сохраняет относительную ссылку `.python-deps-py310` вместе
с её target. Runtime image по-прежнему требует NVIDIA driver на host.

## Автоматический показ

Запуски имеют уникальный RUN_ID; существующая папка не перезаписывается.
Без GUI сохраняются реальные кадры тех же камер Gazebo.

```bash
BAS_NATIVE_FIVE_RUN_ID=demo-town01 BAS_NATIVE_SOURCES=network/config/native_jammers_town01.yaml make demo-town01 DEMO_GUI=0
BAS_NATIVE_FIVE_RUN_ID=demo-customer BAS_NATIVE_SOURCES=network/config/native_jammers_town01.yaml make demo-customer DEMO_GUI=0
```

GUI включается `DEMO_GUI=1` при доступном X11 DISPLAY. Сценарий проверяет control и
payload UART, выполняет P2P/P2MP, взлёт пяти БАС, автономный маршрут БАС1 возле
препятствия, shared uplink, помеху, восстановление, LAND и auto-disarm. Остальные
четыре БАС удерживают позиции. Маршрут автономный; его исполнение во время outage
не выдаётся за доставленную по радио команду. Помеха работает в модельные секунды
100–110. Результат воздействия оценивается по фактическим native traces.

Код возврата и `functional_checks` сохраняют инженерные real-time gates.
Успешная посадка не превращает провал real-time порога в PASS. Cold start,
steady p95 и редкий максимум указаны отдельно. Настройки порогов — инженерные,
а не добавленные требования заказчика.

## Ручное управление через MAVProxy

```bash
BAS_NATIVE_FIVE_RUN_ID=operator-demo make operator DEMO_GUI=0 OPERATOR_DURATION=900
# В другом терминале после сообщения “MAVProxy bridge ready”:
make gcs
```

MAVProxy обнаруживает пять SYSID. Примеры команд в его консоли:

```text
vehicle 2
long REQUEST_MESSAGE 148
status
```

Это безопасный запрос AUTOPILOT_VERSION. В чистом SITL разрешён полёт:
`mode GUIDED`, `arm throttle`, `takeoff 10`, затем `mode LAND`. Выбор vehicle
и подтверждения видны в MAVProxy. Для нормального завершения дождитесь
`Disarming motors`, затем выйдите из НПУ и выполните `make stop`.
Существующие симуляционные параметры отключают часть arming checks — не переносите
эти параметры на аппаратный контроллер. Автоматические flight tests используют SITL.

## Внешний контроллер

Проверен software gateway с настоящими PTY, UDP/TCP sockets, reconnect, unplug,
heartbeat/deadline и native-Sionna интеграцией на UART SITL. Физического FC нет.
Это эмуляция канала/приёмопередатчика, не flight-HIL с передачей датчиков.

Скопируйте `network/config/native_external_serial.yaml` или
`native_external_ethernet.yaml` в свой ignored `runs/endpoint.yaml` и задайте
интерфейс, baud/framing или IP/port. Radio bind/peer остаются адресами native
сети. Root-side endpoint socket имеет доступ к внешнему Ethernet, radio socket
изолирован в ams-uav1. Для UDP peer задаётся явно; для TCP выбран client с reconnect.
Пример serial: `/dev/serial/by-id/...`, 115200, 8N1. На Windows COM подключается
через готовый MAVProxy serial→UDP bridge до Ethernet endpoint на Linux; такой
Windows/COM стенд аппаратно не испытан.

```bash
BAS_NATIVE_FIVE_RUN_ID=external-bench BAS_NATIVE_EXTERNAL_CONFIG=runs/endpoint.yaml BAS_NATIVE_LATENCY_MODE=1 make operator DEMO_GUI=0
make gcs
```

Режим заменяет control UART БАС1; остальные SITL — контекст радиостенда, а не
реальная динамика внешнего FC. Используйте только безопасный REQUEST_MESSAGE на
обесточенном приводном стенде. Arm, моторы, firmware и запись опасных параметров
на физическом FC требуют отдельного разрешения. Аппаратная проверка:
`blocked_external`, нужны FC, доступный serial/Ethernet интерфейс и безопасный стенд.

`external_endpoint/metrics.json` содержит соединение, трафик, queue peaks/drops,
максимальное наблюдённое ожидание и stale drops. Пределы по умолчанию: 64 records,
65 536 bytes, deadline 1 s, watchdog 2 s, reconnect .5 s. Native heartbeat
обновляется раз в .5 модельной секунды; старые BSF1 записи отбрасываются и после
восстановления. Monotonic timestamps принадлежат одному Linux simulation host.

## Метрики, карты и отдельные опыты

```bash
BAS_NATIVE_FIVE_RUN_ID=stationary BAS_NATIVE_LATENCY_MODE=1 make demo-town01 DEMO_GUI=0
make native-sources
make native-maps
make native-cache-study
make native-matrix
BAS_NATIVE_MAP_SOURCES=network/config/native_jammers_town01.yaml BAS_NATIVE_MAP_TIME_S=104 make native-maps
```

Stationary: 10 раундов × 5 concurrent one-shot REQUEST_MESSAGE, плюс отдельные
sequential/retry diagnostics. UART write, ACK UART и GCS ACK сопоставляются по
точным MAVLink bytes. ACK не содержит transaction ID исходной попытки; при
повторах причинный attempt RTT остаётся null, общий operation RTT сохраняется.
Штатный Wi-Fi ARQ не выключается ради one-shot MAVLink измерений.

Native source campaign проверяет continuous/pulsed/sweep, orientation, multiple и
non-overlap. Heatmap grid: x=0..140, y=-20..120 m, z=2 m, 8×8, reference source
active на t=4 s. Это мгновенная received-PSD prediction с теми же сценой, частотой,
антеннами/материалами и мощностью; conditional SINR≥10 dB не означает измеренный PDR.
Белые/no-path ячейки и недоступные значения не превращаются в хорошие измерения.

`native-matrix` — radio-only 1/5 moving/stationary и 16 STA, затем Sionna/Friis
на 500/1000/2000 m. Zero delivery при слабом сигнале сохраняется. Явный
`BAS_NATIVE_PROPAGATION_PROFILE=friis|hybrid` в основном runner является отдельным
инженерным профилем и не проходит gate «весь тракт Sionna». Правило hybrid и
ограничения описаны в архитектуре. Silent fallback отсутствует.

Основные файлы каждого полного run:

- `report.md`, `metrics/five_uav_native_summary.json`, `operating_envelope.json`;
- `wifi_monitor_rx.csv`, `radio_link_metrics.csv`, `radio_link_summary.json`;
- `native_queue_events.csv`, `native_queue_summary.json`, `native_source_summary.json`;
- `received_energy.csv`, `receiver_power_timeline.csv`, `received_energy_summary.json`;
- P2P/P2MP/shared summaries с уникальными application deliveries;
- `screenshots/*.raw.png` (исходные кадры), `*.png` (подписи), `*.json` (время/позиции);
- `pcap/*radiotap*.pcap` (native 802.11 DLT127) и отдельно Ethernet/TAP captures.

SignalNoiseDbm содержит полезный S и combined noise/interference декодированных
MPDU. Это не total RSSI при outage. PER decoder attempts имеет явный знаменатель;
потери до декодера, копии и native retries считаются отдельно. Application PDR
берётся из endpoint root/delivery sets, не из отношения встречных UART counters.
Пустая CSV ячейка/null означает отсутствие численного измерения; источник и причина
сохраняются в availability/summary. BLER для этого PHY: not_applicable.

Отдельный `received_energy.csv` интегрирует фактические received powers/durations
из SignalArrival до решения декодера: RSSI, J/S и энергетический SINR. Thermal floor
вычислен из штатных 7 dB noise figure, 290 K и ширины канала; это configured/derived,
а не измерение шума. В no-path нет строки полезного сигнала; timeline продолжает
показывать пришедшую помеху. Собственная TX leakage не моделируется. Wi-Fi airtime —
объединение настоящих TX intervals с retries; это не CCA busy fraction.
P2P/P2MP/shared JSON содержат goodput полезных байтов за явно заданное окно и
absolute IPDV jitter последовательных уникальных доставок по host monotonic.

## Остановка и ошибки

`make stop` завершает только помеченные BAS containers/process groups и удаляет
их netns через cleanup. При `container already running` сначала остановите
предыдущий run. Не удаляйте произвольные namespaces или пользовательские процессы.
`OptiX unavailable` обычно означает отсутствие GPU/graphics capability; runner
передаёт `NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics`.
При missing Python target проверьте `.python-deps-py310` и его относительный target.
При несовпадении исходников с бинарником запустите без `BAS_NATIVE_FIVE_SKIP_BUILD=1`.
Не повышайте cache TTL или мощность, чтобы скрыть ошибку. Сохраняйте failed run.
Отчёт можно открыть `xdg-open runs/native-radio-realtime/<RUN_ID>/report.md`.

Локальная упаковка: `./scripts/product/package_rc1.sh /absolute/new/package-dir`.
Скрипт сохраняет текущий committed HEAD, pinned image/dependencies, scene assets и
все RC1 runs. Каталог должен быть новым и вне checkout. Исходники следует сначала
закоммитить; uncommitted code не попадает в source bundle. Большие файлы не отправляются
в GitHub. Failed packaging log сохраняйте вместе с диагностикой.
