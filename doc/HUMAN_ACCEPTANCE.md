# Короткая человеческая приёмка BAS v2 RC1

`independent_acceptance=pending`. Процедуру выполняет другой принимающий инженер;
имя и дату заполняет сам проверяющий. Повтор агента на том же стенде этого статуса
не меняет. Назначений людей и встреч нет. Полный исследовательский benchmark не нужен.

Начать с [HANDOVER](HANDOVER.md), установить **release** по
[USER_GUIDE](USER_GUIDE.md). Команды ниже выполняются из восстановленного `source/`.
Видеофайлы открываются из отдельного `viewing/`; для просмотра исходники не нужны.

| Действие / команда | Ожидаемое наблюдение | Куда смотреть при ошибке |
| --- | --- | --- |
| `make demo-preflight DEMO_GUI=0 DEMO_BOOTSTRAP=1`, затем `make prepare-customer` | 0 failures/warnings; сцены подготовлены в этой установке | Вывод команды; image ID, Python pins и пути из USER_GUIDE |
| Из viewing: `xdg-open videos/BAS_v2_RC1_demo_ru.mp4` | Пять глав, русские титры; 01 — пять БАС/НПУ и оператор; 02 — распространение; 03 — UART/общий эфир; 04 — помехи; 05 — software gateway и границы | `INDEX.md`, `requirements_video_matrix.csv`, `metrics/recording_performance.json`; 01/02 имеют failed real-time gate |
| `BAS_NATIVE_FIVE_RUN_ID=accept-operator-$(date -u +%Y%m%dT%H%M%SZ) make operator DEMO_SCENARIO=customer DEMO_GUI=1 OPERATOR_DURATION=900` | Gazebo показывает пять БАС и НПУ; после загрузки — `MAVProxy bridge ready` | Текущий `runs/native-radio-realtime/<ID>/logs/stack_health.log`, `logs/gazebo_sitl.log`, `logs/operator_bridge.log`; GUI требует X11, допустим `DEMO_GUI=0` с журналами и роликом 01 |
| В другом терминале `make gcs`; в MAVProxy: `vehicle 2`, `long REQUEST_MESSAGE 148`, `status` | Выбран SYSID2; настоящий `COMMAND_ACK` для REQUEST_MESSAGE и AUTOPILOT_VERSION при поддержке FC; не синтетический ответ | `runs/operator/`, `logs/operator_bridge.log`, `metrics/operator_bridge.json`, журналы control UART; наличие heartbeat само по себе не подтверждает запрос |
| Выйти из MAVProxy (`Ctrl-D`), затем `make stop` | Операторский run завершён; контейнер исчез из `docker ps --filter label=bas.product=native-radio-five-uav` | Вывод stop; не удалять чужие процессы/netns. Оператор и автоматический harness используют разные RUN_ID |
| `BAS_NATIVE_FIVE_RUN_ID=accept-customer-$(date -u +%Y%m%dT%H%M%SZ) BAS_NATIVE_SOURCES=network/config/native_jammers_town01.yaml make demo-customer DEMO_GUI=0` | Один штатный run: 5 SITL, control/payload UART каждого, P2P/P2MP, shared uplink, взлёт/маршрут/LAND/auto-disarm, no-bypass; без непрерывной записи | `logs/flight_scenario.log`, `metrics/scenario_summary.json`, `metrics/control_uart_*`, `metrics/payload_uart_*`, `metrics/p2p_summary.json`, `metrics/p2mp_summary.json`, `metrics/shared_medium_summary.json` |
| `cat runs/native-radio-realtime/<ID>/report.md`; открыть `screenshots/` | Десять UART, реальные ACK, multicast roots и доставки каждому; все штатные functional checks перечислены явно | `metrics/five_uav_native_summary.json`, `logs/summary.log`, `pcap/`; код возврата ненулевой сохраняется как failed |
| Просмотреть ролик 04 и `metrics/native_source_summary.json`, `metrics/receiver_power_timeline.csv` этого run | Излучение предусмотренной помехи в native t=100–110 s; изменение полученной мощности/packet outcomes и последующее восстановление | `logs/native_radio_events.csv`, `metrics/received_energy.csv`; автономное движение не доказывает приём команд во время outage |
| После завершения `make stop`; `docker ps --filter label=bas.product=native-radio-five-uav` | Пять БАС сели и auto-disarm подтверждён, no-bypass выполнен, BAS container отсутствует, отчёты доступны | `metrics/no_bypass_summary.json`, `logs/flight_scenario.log`, вывод stop; сохранить незавершённый run при ошибке |

Не запускать `demo-record-all`, новые видеосценарии, ALOHA, custom PER/provider,
overload, matrix/cache benchmark. Видео 05 показывает PTY/UDP/TCP на UART SITL;
аппаратная часть выполняется отдельно по [FC_BENCH](FC_BENCH.md).

После проверки инженер записывает в своём протоколе: имя, дату, source commit,
RUN_ID обоих запусков, команды, фактические наблюдения, gates и ссылки на отчёты.
Только после этого можно решить вопрос изменения `independent_acceptance`.
Аппаратный и full-TZ статусы автоматически от человеческого просмотра не меняются.
