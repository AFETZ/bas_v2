# BAS v2 RC1 — контрольное восстановление и внутренняя передача

Дата: 2026-09-06. Продукт: пять БАС ArduPilot SITL/Gazebo и НПУ, native ns-3.48 Wi-Fi PHY/MAC с in-process Sionna RT. Функциональность RC1 заморожена. Release:
`release/bas-v2-rc1`, **263dfd5b494a3471038af7e79687fd20ae482cfb**. Demo: `demo/bas-v2-rc1`, **70c51575ca45bddbfb0a94d5ec391af1f5aa462b** (предшествующий recording commit
`b88818e`). Handover: `feature/bas-v2-rc1-handover`. Дополнение [RC1.1](RC1_1_STABILIZATION.md): reporting исправлен; real-time limited сохранён.

## Что открыть и откуда получить

Начать с `viewing/INDEX.md`, затем `viewing/videos/BAS_v2_RC1_demo_ru.mp4` (13:37.120). Передаваемая структура разделена; названия ниже — роли каталогов, их можно перенести:

```text
software/   source.bundle, source.tar.gz, runtime-image.tar,
            native-dependencies.tar.gz, scene-assets.tar.gz, INDEX.md, doc/, artifacts/
viewing/    INDEX.md, HANDOVER.md, videos/ (общий + пять MP4), subtitles/ (русские SRT),
            doc/, requirements_video_matrix.csv, edit_timeline.csv, metrics/, reports/
source/     отдельная восстановленная установка и её runs/; создаётся при установке
research/   полные исходные записи, frames.jsonl, PCAP/CSV/логи; выдаются отдельно
```

На **имеющемся стенде** доступен вход под разрешённой учётной записью Linux (локальный сеанс или уже предоставленный SSH/SFTP). Пути ниже — адреса на стенде, а не доступные
получателю веб-ссылки. Внешняя публикация и рассылка не выполнялись.

| Роль | Фактическое расположение на стенде |
| --- | --- |
| software | `/home/bas/bas_v2-delivery/rc1-2026-09-05-final` |
| viewing | `/home/bas/bas_v2-handover/rc1-2026-09-06/viewing` |
| source | `/home/bas/bas_v2-restore-check` |
| research/demo | `/home/bas/bas_v2-demo/rc1-2026-09-05/raw` и остальные исходники демопакета |
| research/испытания | `software/artifacts/runs/` (A), `source/runs/` (B) |
| Проверки восстановления | `/home/bas/bas_v2-handover/rc1-2026-09-06/checks` |

В viewing нет image, сцен, непрерывных исходных AVI/MKV и полного PCAP. Есть компактные подтверждающие выборки; `DATA_LOCATIONS.md` объясняет границы и перенос путей. Исходные
пакеты и шесть готовых MP4 сохранены, полное декодирование не повторялось: размеры/mtime совпадают с состоянием до сохранённого `reports/video_quality.json`.

## Что демонстрируют пять роликов

| MP4 в `videos/` | Требования и наблюдение |
| --- | --- |
| `01_fleet_and_customer_scene.mp4` | R1/R8/R9: пять настоящих SITL, НПУ, customer 10×10 км, выбор SYSID2/ACK, полёт/LAND/auto-disarm |
| `02_propagation_and_obstruction.mp4` | R3/R5: native Wi-Fi/Sionna, движение БАС1 у препятствия, двунаправленные power/energy measurements и возраст |
| `03_communications_and_medium_access.mp4` | R2: десять UART, 50 one-shot ACK, P2P, 20 P2MP roots/100 доставок, общий uplink |
| `04_interference_and_heatmaps.mp4` | R4/R5: восемь source cases и отдельные native prediction maps; видимое воздействие помех |
| `05_external_interface_and_operating_envelope.mp4` | R6/R7/R10: PTY/UDP/TCP на UART SITL, reconnect/no-bypass, границы масштаба и дальности; физического FC нет |

Точные timecodes и подтверждающие файлы — `requirements_video_matrix.csv`.

## Установка, запуск, остановка

В [USER_GUIDE](USER_GUIDE.md) дан последовательный offline-блок: клонировать **source.bundle**, проверить release SHA, `docker load`, распаковать dependencies/assets в новый
`source/`, `make demo-preflight DEMO_GUI=0 DEMO_BOOTSTRAP=1`, затем `make prepare-customer`. Нужны описанные Linux/Docker/NVIDIA host prerequisites; image сам не поставляет host
driver. Из `source/` выполнить один run:

```bash
BAS_NATIVE_FIVE_RUN_ID=rc1-customer-$(date -u +%Y%m%dT%H%M%SZ) BAS_NATIVE_SOURCES=network/config/native_jammers_town01.yaml make demo-customer DEMO_GUI=0
make stop  # после штатного завершения; также команда аварийной остановки стенда
```

## Три независимых результата

| Результат | Штатные gates | Gazebo RTF mean | steady lag p95 / max, ms | radio-state age p95 / max, s |
| --- | --- | --- | --- | --- |
| A: исходный `rc1-customer-final-01` | 20/20, passed | .996689 | .336842 / 58.010540 | 13.928 / 19.880 |
| A: исходный `rc1-customer-final-02` | 20/20, passed | .996043 | 16.076663 / 108.432918 | 13.870 / 19.880 |
| B: `rc1-restored-customer-20260906T100415Z` | **19/20, failed**, make rc=2 | .993056 | **57.760693** / 129.202154 | 13.880 / 19.880 |

B восстановлен без скрытых зависимостей старого checkout: preflight 0/0, 752 ссылки сцен разрешаются внутри восстановленного mount; image/pins совпали, source чистый. B завершил
полёт/LAND/auto-disarm пяти БАС, 26 реальных ACK result=0, десять UART, P2P 100/100, P2MP 20 roots/100 deliveries, shared uplink 100/100, fairness=1, no-bypass 10.5 s. Помеха
t=100–110 s: decoder errors 0→6→0, SINR mean 32.06→27.34→30.92 dB; application PDR в этих окнах не измерен. Cleanup полный. Провален только
`realtime_scheduler_gazebo_and_pose_gates`: lag p95 >50 ms. Остальные RT границы пройдены: RTF mean≥.95, p5=.949876≥.8, pose age p95=30.339≤500 ms. Cold lag max=9197.073 ms указан
отдельно; steady — вне preflight/unclassified. 22 из 395 steady samples >50 ms; причина вариации не установлена, дефект установки не подтверждён. Повтор полного run не выполнялся.
**software_release_status=limited** для текущей передачи; A остаётся исторически verified, результат B не переименован.

C — независимые видеопрогоны: 01/02 **real-time gate failed**, capture ~19.37/~24.68 FPS, RTF ~.776/~.988, lag p95 76.77/71.92 ms. Для 03–05 измерения берутся из
`metrics/recording_performance.json`; сохранены rc=2 и failed full gates C/03, rc=2 при отчёте C/05, rc=0 восьми source cases C/04. Полный PASS им не присваивается. Метрики A/B не
относятся к кадрам C; 25 FPS MP4 — формат файла, без ускорения времени. Сравнение опирается на сохранённые `metrics/five_uav_native_summary.json`, а не старые округления STATUS;
runtime HEAD A — 547a536/2fb157b, B — точный release SHA выше. Отчёты A/B и диагностика включены выборочно в `viewing/reports/`; исходные raw сохранены.

## Исправления и оставшиеся ограничения

Исправлены инструкции offline-восстановления/проверки точных SHA и переносимые ссылки просмотрового комплекта. Исправлен ошибочный комментарий Ethernet YAML о namespace: внешний
socket — host, radio socket — ams-uav1. Значения YAML и runtime не менялись. В титрах различаются capture FPS и 25 FPS файла; документы теперь раскрывают также ненулевые коды C/03
и C/05. Монтаж и старые результаты сохранены. Дефект C/05 зарегистрирован: `write_latency_diagnostic_report` обращается к неопределённому `native_sources` (`logs/summary.log`); в
замороженном RC1 не исправлен.

- Кэш 20 s/10 m: задержка исчезновения пути около 1 s и пропуск восстановления 1 s;
  четыре no-path mismatch в исходном исследовании. Низкий lag не доказывает точность канала.
- Hard real-time гарантии нет. Radio-only 16 STA не являются 16 полноценными SITL.
  При reference 10 dBm на 500/1000/2000 m зафиксированы 0/100 доставок.
- MAVLink payload второго UART не видеопоток; reference Wi-Fi не калибровка модема.
  Software gateway не проверенный аппаратный HitL; полноценный flight-HIL не заявлен.
- Поле/холмы/башня синтетические, Town01 collision proxies приближённые.
  Передача CAVISE третьим лицам не разрешена автоматически: условия не подтверждены.

## Что требуется от принимающей стороны

Другой принимающий инженер выполняет [короткую человеческую приёмку](HUMAN_ACCEPTANCE.md) и записывает своё имя/дату/наблюдения/RUN_ID. Назначений нет;
`independent_acceptance=pending`. Для аппаратного шага нужны физический FC, совместимый serial/COM или Ethernet, кабели и безопасный стенд по [FC_BENCH](FC_BENCH.md).
Arm/моторы/прошивка/опасные параметры физического FC требуют отдельного разрешения. `hardware_validation_status=blocked_external`; `full_tz_status=blocked_external` до аппаратного
испытания **и** принятия заявленного диапазона применимости. Отсутствие FC не блокирует эту внутреннюю передачу программного RC.
