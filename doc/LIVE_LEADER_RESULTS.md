# BAS live: измеренные результаты и запуск

27.10.2026. Рабочая копия `/home/bas/bas_v2-paper-live`, ветка `paper/live-leader-20261026`.
Измеренная версия — `8e16ec223e895c11891e6e7d7c39dff8cee39cac`; последующая документация не меняет реализацию.
Подтверждённый профиль: Town01, stock DART/Gazebo, пять настоящих ArduPilot SITL, native ns-3.48 Wi-Fi ad-hoc/Friis, headless, control UART.
Sionna RT не инициализируется; GPU/OptiX preflight для этого профиля не требуется. Агрегация/Block Ack отключены штатными атрибутами ns-3; физика, MAC, PER и автопилот — upstream.

## Эксперимент

Матрица fixed/failover × none/selective/total_GCS × пять парных RngRun: **30/30 completed**, 150 физических циклов ARM→подъём→изменения высоты/удержание→LAND→disarm. Исключённых запусков нет; одна версия исходников, image и настроек.
RngSeed=1, RngRun=1…5; порядок 15 пар и политик заранее рандомизирован seed=20261026. Единица повторения — групповой запуск.
После готовности всех пяти измеряется 180 model s: 0–45 normal, 45–105 fault, 105–180 recovery; фактические границы RF берутся из native events.
Отказ — явно заданные дополнительные 200 dB через native MatrixPropagationLossModel: CP↔UAV1 либо CP↔все; peer-связи не отключаются. Friis не моделирует затенение зданиями.
НПУ — единственный issuer; heartbeat 0,2 s, detection 1 s, lease 2 s, renewal 0,5 s, fence 10 ms после максимального ранее выданного полномочия, command age ≤250 ms на UART.
НПУ передаёт исполняемые команды выбранному координатору; тот обслуживает себя и четырёх follower. После selective loss бывший UAV1 остаётся достижим по peer path и включён в четвёрку follower.
Миссия: 1 Hz, ровно 900 offered обновлений/запуск; новая высотная цель каждые 20 model s, шаг 4 m, базовые высоты 8/12/16/20/24 m относительно home. XY удерживается.
Физическая цель требует изменения ≥1 m, нахождения в области z±1,5 m/XY±3 m при скорости ≤1 m/s в течение 2 source s. Графики actual/target используют world ENU z; target преобразован с записанным calibration offset, это не AGL.

| Условие | Политика | Timely / все offered, среднее | Приём новой команды всеми 5, s | Новая физическая цель всеми 5, s |
|---|---|---:|---:|---:|
| Без отказа | fixed | 99,511% | N/A | N/A |
| Без отказа | failover | 99,511% | N/A | N/A |
| Selective | fixed | 65,711% | 62,356 | 67,715 |
| Selective | failover | 98,222% | 3,963 | 21,302 |
| Total-GCS | fixed | 65,711% | 61,971 | 67,448 |
| Total-GCS | failover | 65,867% | 62,178 | 67,543 |

Времена — средние пяти запусков от actual fault_start; UART-допуск и физическое исполнение — разные исходы. Потерянные и неотправленные запросы входят в знаменатель. Старое завершившееся задание не считается восстановлением.
**Текст для статьи:** «В пяти парных повторениях selective loss назначение нового координатора увеличило долю своевременных UART-допусков с 65,711% до 98,222%: средний парный эффект +32,511 п.п., исследовательский 95% bootstrap-интервал [32,267; 32,756] п.п. Среднее восстановление приёма команды всеми пятью аппаратами составило 3,963 s против 62,356 s; новой физической цели — 21,302 s против 67,715 s».
Парные эффекты actual времени: −58,393 s [−58,799; −57,994] для UART и −46,413 s [−46,810; −46,016] для новой цели; все selective восстановления наблюдались, без цензурирования.
Заранее заданная 30 s bounded-loss метрика даёт −26,037 s [−26,058; −26,020] и −8,698 s [−8,713; −8,682]. Это ограниченная потеря, не выдуманное восстановление на 30 s.
Интервалы по пяти целым парам, exact 3125 resamples, разведочные. No-fault эффект timely=0 п.п. с интервалом [−0,511; +0,489]; эквивалентность не доказана. Total-GCS bounded losses насыщены на 30 s; [0;0] не доказывает эквивалентность.
Во всех 10 total-GCS опытах: настоящий BRAKE/удержание пяти, 0 новых назначений и target UART-допусков внутри RF-изоляции; 15 потерянных renewal issues показаны отдельно. Возобновление произошло после возврата связи.
В серии 27 000 offered и **75 058 UART-допусков**; аудит не обнаружил duplicate/stale/lease/epoch/lineage нарушений. Это наблюдение этих запусков, не универсальная гарантия.

## Live, управление, данные

Отдельный `paper-live600-02`: 600,001 model / 600,123 wall s, сценарный RTF=0,999796; по крайним tracker samples 599,978/600,110 s tracker RTF=0,999779, p5≈1 s окон=0,998672, минимум=0,970634; lag p95/max=1,499/28,952 ms. Все пять приземлились; 150 физических целей, 2996/3000 timely.
Внешний оператор: 225/225 TIMESYNC ответов во время полёта; UAV3 реально переключён GUIDED→BRAKE→GUIDED через UDP НПУ, координатора и UART, подтверждён исходными HEARTBEAT.
Основная серия: RTF 0,999564…0,999883, худший run p5=0,997355, max measurement lag=7,847 ms; пределы 250/500/100 ms не ослаблялись.
[Native proof первой плановой пары](../runs/paper-live-20261026/main-selective-s1-r04-native-path-review.md): оба радио-перехода и возврат для follower 1/2/3/5 под master4, новое задание выполнено внутри 60 s отказа. Отдельный grounded SIGSTOP master-service дал 223/225→0/225→220/225 настоящих ответов.
[Raw](../runs/native-radio-realtime/), [план/freeze/source/точные команды](../runs/paper-live-20261026/campaign/), [30 отчётов, CSV и графики](../runs/paper-live-20261026/campaign-analysis/relay_campaign_analysis.md).
[Рисунок первой плановой пары](../runs/paper-live-20261026/first-pair-figure/first_planned_selective_pair.png) доступен также в SVG/PDF; это иллюстрация n_pair=1, не выбор лучшего результата. [Числа шести ячеек](../runs/paper-live-20261026/paper-numbers.csv).
[Независимое ревью](../runs/paper-live-20261026/campaign-final-review/review.md), [целостность raw/config](../runs/paper-live-20261026/campaign-metadata-integrity-review.md), [отдельный live proof](../runs/paper-live-20261026/live600-02-analysis/live/live_metrics.md).
В каждом run: `configuration/{initial,final}`, effective ns-3 ConfigStore, SDF/launch/radio/routes/QoS, DataFlash BIN + observed PARM CSV (1388 уникальных имён на SITL), app/UART JSONL, native events, PCAP, poses/resources. 240 native файлов, 10 184 179 226 bytes, скопированы без ошибок.

## Команды в подготовленной рабочей копии

При повторе выберите новый RUN_ID: существующие raw не перезаписываются. Профиль ниже дословно сохраняет общие переменные frozen environment.

```bash
cd /home/bas/bas_v2-paper-live
source runs/paper-live-20261026/live-profile.env
# Автоматический selective-эксперимент; посадка включена в сценарий.
BAS_NATIVE_FIVE_RUN_ID=user-selective-01 BAS_NATIVE_FAULT_PROFILE=selective \
  flock -n -E 75 /tmp/bas-paper-runtime.lock ./network/ns3/run_native_radio_five_uav.sh
# Либо интерактивный стенд без отказа, 900 s; этот режим сам не выполняет посадку.
BAS_NATIVE_FIVE_RUN_ID=user-operator-01 BAS_NATIVE_FAULT_PROFILE=none \
  flock -n -E 75 /tmp/bas-paper-runtime.lock make operator DEMO_SCENARIO=town01 DEMO_GUI=0 OPERATOR_DURATION=900
```

Во втором терминале: `cd /home/bas/bas_v2-paper-live && make gcs`. В MAVProxy после готовности GPS/position estimate:

```text
vehicle 3
set link 3
mode GUIDED
arm throttle
takeoff 16
```

После наблюдаемого подъёма отправьте `mode LAND`. Дождитесь посадки и disarm, затем остановите принадлежащий вам native-контейнер:

```bash
docker stop --timeout 120 bas-v2-native-radio-five-uav
```

`make stop` также получил native grace 120 s, но останавливает и другие именованные BAS-стенды; для этого опыта выше указан конкретный контейнер. `BAS_NATIVE_FIVE_SKIP_BUILD=0` включает пересборку после изменений. Точный argv анализа — `runs/paper-live-20261026/campaign/analysis-command.json` (пиннед Docker/Python 3.10; host Python не содержит Matplotlib).

## Ограничения и сохранённые неудачи

Область результата — одна карта, вертикальная миссия, один хост с общим monotonic clock, native Friis и симметричные заданные обрывы. Нет вывода о физическом HitL, распределённой синхронизации часов или hard real time. Rock/Bullet не прошёл подтверждение взлёта; GUI, RT-геометрическое радио и faster-than-realtime не измерены в этой серии.
Town01 использует канонические визуальные meshes и приближённые collision boxes; растительность не имеет collision. Ресурсы покрывают три категории симуляторов, не полный расход всех helpers. В live proof ns-3 в среднем ≈30,07% одного ядра/RSS max58,93 MiB; tmpfs-файлы дополнительно732,18 MiB (ёмкость4 GiB не равна расходу).
DataFlash — observed-only экспорт, полнота по live PARAM_COUNT не доказана; generic native_sources имеет not_recorded при явном sources=none. В deduplicated trajectory CSV есть один момент с четырьмя уникальными позами; синтетическая пятая не добавлялась. Все N/A/пропуски остаются в CSV; 10 pair_exclusions относятся только к no-fault recovery N/A, исключённых runs0.
Неудачные пилоты сохранены: отсутствие подъёма rock, ранний false handover, перегрузка batched logging, native BAR storm, `paper-live600-01` с единичным jitter261,329 ms и прерванными посадками. Точная причина последней паузы не измерена; tmpfs выбран как проверенный профиль, причинная оценка HDD/overhead не заявляется. `paper-forward-stop-01` — инфраструктурная ошибка маркера до проб.
Контрольные тесты, raw и review находятся в `runs/paper-live-20261026/`; 37 focused mission/analysis tests прошли, после storage-изменения прошли22 analysis tests; native binary проверен сборкой и реальными запусками. Полная регрессия других профилей не заявляется.
Исходный `/home/bas/bas_v2-main` и его локальные изменения сохранены побайтово относительно подготовки. Симуляторы этой работы остановлены. Статья не отправлялась, репозиторий не публиковался.
