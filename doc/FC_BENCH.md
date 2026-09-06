# Физический FC: безопасный стендовый шаг RC1

`hardware_validation_status=blocked_external`: физический полётный контроллер
не подключён и не проверен. Нужны FC с MAVLink, доступ к его интерфейсу и принимающий
инженер с безопасным стендом. PTY, второй процесс и UART SITL физическим FC не являются.
Это проверка эмуляции приёмопередатчика/канала; flight-HIL с датчиками не заявляется.

1. **Подключение.** Подходит serial/COM (Linux USB serial или UART через подходящий
   адаптер) либо Ethernet UDP/TCP. Нужны кабель FC, при необходимости USB–UART
   адаптер, либо Ethernet-кабель и уже настроенный MAVLink endpoint. До подключения
   сверить по документации конкретных FC и адаптера распиновку, уровни напряжения,
   питание и общую землю; TTL UART, RS-232 и RS-485 не взаимозаменяемы. Для UART
   соединяются TX→RX, RX→TX и GND при подтверждённой совместимости. Не подавать
   питание от адаптера без проверки схемы. Приводы обесточены, винты сняты.
2. **Конфигурация.** Из восстановленного `source/`: `mkdir -p runs`, затем
   `cp -n network/config/native_external_serial.yaml runs/endpoint.yaml`
   (либо `native_external_ethernet.yaml`). Отредактировать только копию.
   Serial: фактический `/dev/serial/by-id/...`, `115200`, `bytesize: 8`, `parity: N`,
   `stopbits: 1`, если эти значения уже поддерживает FC. Ethernet: `kind: udp`
   с явными `bind/peer` или `kind: tcp_client` с `peer: [IP, PORT]`; loopback из
   примера заменить фактическим интерфейсом. SYSID физического FC должен соответствовать
   выбранному UAV1/SYSID1; при несовпадении остановиться для согласования конфигурации.
   Радио оставить: `uav_id: 1`, `channel: control`, namespace `ams-uav1`,
   bind `10.71.1.10:14601`, peer `10.71.0.10:14600`; queues `64/65536`,
   deadline `1 s`, watchdog `2 s`, reconnect `.5 s`. Runner добавляет heartbeat path
   и создаёт radio socket в ams-uav1, внешний Ethernet socket — в host namespace.
3. **Запуск.** Сначала preflight по [USER_GUIDE](USER_GUIDE.md). Затем:

   ```bash
   BAS_NATIVE_FIVE_RUN_ID=fc-bench-$(date -u +%Y%m%dT%H%M%SZ) BAS_NATIVE_EXTERNAL_CONFIG=runs/endpoint.yaml BAS_NATIVE_LATENCY_MODE=1 make operator DEMO_GUI=0
   # В другом терминале после “MAVProxy bridge ready”:
   make gcs
   ```

   В MAVProxy: `vehicle 1`, `long REQUEST_MESSAGE 148`, `status`.
   Проверить реальный COMMAND_ACK и AUTOPILOT_VERSION, если FC поддерживает запрос.
   Исходные MAVLink bytes должны пройти GCS→BSF1→native Wi-Fi/ns-3/Sionna→gateway→FC
   и обратно; прямое второе соединение GCS–FC не допускается. Остальные SITL дают
   контекст радиостенда, но не динамику внешнего FC. Неподдержанный запрос фиксируется
   фактическим ACK, а не заменяется фиктивным успехом.
4. **Reconnect.** Разъединить только кабель данных, подключить обратно и повторить
   тот же запрос. Смотреть `runs/native-radio-realtime/<ID>/external_endpoint/`:
   `events.jsonl` connected/disconnected, `metrics.json` reconnects/io_errors,
   connected/radio_live, queues/deadline/stale drops. Для UDP reconnect счётчик
   не доказывает физический линк: нужны фактические прекращение/возврат байтов и ACK.
5. **Watchdog.** При работающем gateway найти единственный native executable:
   `docker exec bas-v2-native-radio-five-uav pgrep -af '^/workspace/multiagent_simulation/.external/ns-3-sionna-native/build/scratch/ns3.48-upstream-sionna-tap-spike-default'`.
   Сверить PID/команду с `logs/process_snapshot.txt`, затем
   `docker exec bas-v2-native-radio-five-uav kill -STOP <проверенный_PID>`.
   Через >2 s `radio_live=false`, очереди очищены, новый REQUEST_MESSAGE не доставляется.
   Вернуть процесс командой `kill -CONT <проверенный_PID>` через тот же docker exec;
   повторить только свежий запрос. Старые команды не должны воспроизводиться.
   Если native завершился/heartbeat не восстановился, сохранить диагностику и остановить run.
6. **Остановка.** После `CONT` выйти из MAVProxy и выполнить `make stop`; проверить
   отсутствие BAS container, сохранить логи и обеспечить безопасное отключение FC.

Arm, моторы, прошивка и изменение опасных параметров на физическом FC требуют
отдельного разрешения. Этот документ не разрешает эти действия и не меняет hardware
или full-TZ статус. Условия передачи CAVISE третьей стороне остаются неподтверждёнными.
