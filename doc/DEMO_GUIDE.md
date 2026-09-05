# Видеодемонстрация BAS v2 RC1

Рабочая ветка: `demo/bas-v2-rc1`; базовый RC1 не изменяется.
Локальный внутренний демопакет: `/home/bas/bas_v2-demo/rc1-2026-09-05`.
Физический FC не подключён: **software demonstration + hardware blocked**.

## Команды

Из корня checkout:

```bash
make demo-record SCENARIO=01   # 01, 02, 03, 04 или 05
make demo-record-all           # последовательно; продолжает незавершённый набор
make demo-video                # сборка из сохранённых кадров и журналов
make stop
```

Новый независимый набор: добавьте `DEMO_OUTPUT=/absolute/new/directory`.
Завершённый сценарий не перезаписывается. После исправления проблемы повторяются
только незавершённые cases; исходные неуспешные runs сохраняются.
Для перемонтажа одного ролика без исполнения стенда:

```bash
python3 scripts/product/demo_video.py --output /absolute/demo_output --scenario 02
python3 scripts/product/demo_finish_video.py --output /absolute/demo_output
```

Нужны существующий pinned Docker runtime, подготовленные assets, NVIDIA,
X11/GNOME Terminal для реального окна MAVProxy и host FFmpeg с libx264,
Python с Pillow/NumPy/PyYAML. ROS/Gazebo/OpenCV берутся из runtime image.
Новые медиасервисы, генеративные изображения, TTS или radio bypass не используются.

## Пять entry configs

| ID | Entry | Исполнение |
|---|---|---|
| 01 | `network/config/demo/01_fleet_and_customer_scene.json` | customer: операторский case, затем отдельный штатный полёт пяти SITL |
| 02 | `network/config/demo/02_propagation_and_obstruction.json` | customer: штатная автономная миссия UAV1, четыре БАС удерживают позиции |
| 03 | `network/config/demo/03_communications_and_medium_access.json` | существующие dual-UART, 10×5 parallel one-shot, P2P, multicast, shared uplink |
| 04 | `network/config/demo/04_interference_and_heatmaps.json` | восемь конфигураций native source campaign на текущем пяти-SITL стенде; native-maps после записи |
| 05 | `network/config/demo/05_external_interface_and_operating_envelope.json` | внешний serial/PTy, TCP, UDP на настоящем UART SITL; TCP disconnect/reconnect; native stop probe |

Смена GCS-клиента между оператором и flight harness выполняется через новый run:
BSF1 нумерация существующего UART receiver не должна сбрасываться новым sender
в середине потока. Case/run_id явно показаны в монтаже.
Сценарии являются фазами существующего `run_native_radio_five_uav.sh`.
MAC/PHY, solver, мощность, параметры кэша и маршруты не меняются.
Дополнительные обзорные камеры имеют только visual sensors, без collision.

## Время и просмотр

`raw/<run_id>/video/*.avi` — непрерывная последовательность реальных кадров.
AVI использует индексную шкалу 25 FPS. Фактическое wall/model время каждого кадра
записано в `frames.jsonl`; AVI FPS сам по себе не является измерением capture FPS.
Монтаж выбирает исходные кадры по host monotonic, с сохранением задержек и пропусков.
`operator.mkv` содержит захват настоящего окна MAVProxy; `operator_io.jsonl` —
ввод/вывод приложения. `operator_clock.json` задаёт mapping recorder clock.

Основные файлы: `INDEX.md`, пять MP4 и `BAS_v2_RC1_demo_ru.mp4`, `subtitles/*.srt`,
`edit_timeline.csv`, `requirements_video_matrix.csv`, `reports/video_quality.json`,
`metrics/recording_performance.json`. SRT и русские титры объясняют действия;
музыки и озвучки нет. В общем MP4 имеются главы и русская subtitle track.

Пятикамерная запись 01 снизила измеренный RTF примерно до 0,776 и фактический
capture до 19,4 FPS. Штатный performance gate не пройден; успешный полёт не
превращает его в PASS. Полёт 02 также не прошёл performance gate: RTF 0,988,
steady ns-3 lag p95 71,92 мс. Остальные значения находятся в INDEX и исходных reports.

UART payload — MAVLink, не видеопоток. Autonomy не доказывает доставку команд
во время outage. LOS classification не выдумывается. Energy SINR отделён от
native decoder measurements; BLER для Wi-Fi — not_applicable. Кэш 20 с / 10 м
может пропустить короткое восстановление. Карты 8×8 — native prediction,
не измеренная PDR-карта; baseline/jammer имеют одинаковые цветовые пределы.
RC1 radio-only benchmarks показаны отдельно с исходными run_id и настоящими
outages при 10 dBm; 16 STA не означают 16 SITL. Это не hard real-time и не
аппаратный HitL. Условия передачи CAVISE assets третьим лицам остаются открытыми.

В Git входят только код, конфигурации и короткие инструкции. MP4, traces,
PCAP и assets остаются локально вне Git. Демопакет предназначен для внутреннего просмотра.
