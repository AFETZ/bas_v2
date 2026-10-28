# Поэтапная миссия: диагностическая пара до freeze

28.10.2026. **Пилоты, n=1 на политику; не основная серия и без CI.**
`urban-staged-pilot05-failover` / `urban-staged-pilot06-fixed`, seed1/RngRun12,
240model s, одинаковые JSON/сеть/протокол, WP_SPD3m/s, физический dwell2s и
маршрутное ожидание3s. Различие source revisions включает только измерительные
изменения/запись executable hash; физика, radio и mission между парой не менялись.
Последующий общий post-window timeout60s выбран до основной серии;
эта пара ещё использовала180s. Настройки внутри измерительного окна не меняются.

| Показатель | Fixed | Failover |
|---|---:|---:|
| Offered | 1200 | 1200 |
| Созданные target CID | 444 | 1177 |
| Master application receive | 427 | 1167 |
| Follower application receive | 424 | 1163 |
| Target UART write-end | 424 | 1161 |
| Timely/offered | 424/1200 | 1161/1200 |
| Подтверждённые физические waypoint | 15/27 | 27/27 |
| Новых назначений | 0 | 1, UAV4/epoch2 |
| Первый post-NLOS UART всех пяти | не наблюдался | 4,236257156s |
| Новая post-NLOS физическая цель всех пяти | не наблюдалась | 53,944859817s |
| Попытка | failed: LAND после окна | completed |

У fixed окно полностью записано; отсутствие восстановления цензурировано через
152,272821271host s после NLOS. Политика не запрещает восстановление прежнего
relay, если его канал вернётся; здесь BRAKE удержал его в затенённой геометрии.
Завершение окна не превращает failed LAND/пропуски after-параметров в успех.

У failover4253 всех UART admissions проверены по выданным полномочиям и возрасту;
нарушений/непроверенных допусков/перекрытий не обнаружено. Полные pre/post таблицы
параметров пяти аппаратов выгружены вне окна; WP_SPD3 подтверждён MAVLink.
Превышение4s сохранено; физический endpoint не имеет4s SLA.

53,944859817s =18,487834235s до создания **выбранной первой успешной** новой цели
критического UAV1 +0,731353926s до первого успешного refresh +0,005228292s
создание→UART +34,720443364s движение/наблюдение/удержание.
Это одна проверенная по порядку lineage, не сумма максимумов разных UAV.
Задержка создания зависит от сценарных барьеров; это не отсутствие других целей
и не чистое время алгоритма handover. Первый и completion-refresh CID различаются.

Сетевая доставка не приравнивается к UART: здесь1163 validated application
receipts и1161 UART. Это не счёт PHY Rx/ACK. Физические27 — уникальные waypoint,
включая стартовые удержания/этапы поддержки, а не27 независимых полётов или
заявление о качестве фактического визуального осмотра.

[Native PHY proof](pilot_pair/native_epoch2.md) прослеживает обе передачи
CP→UAV4→followers1/2/3/5 по captures отправителя и получателя, совпадающие bytes
на реальном UART и обратную подлинную телеметрию. Wi-Fi PCAP time — ns-3 model s;
UART/lease time — host monotonic ns. Последующая телеметрия не выдаётся за ACK.

Численные CSV/происхождение: `pilot_pair/pilot_runs.csv`, `pilot_provenance.json`.
Полные raw: `runs/native-radio-realtime/urban-staged-pilot0{5-failover,6-fixed}`.

```bash
python3 scripts/product/analyze_urban_experiment.py run RAW --output OUT
python3 reports/ispras/urban_ns3/tools/export_pilot_pair.py \
 --analysis runs/urban-ns3-dev/staged-pilot05-final-analysis/metrics/urban_analysis.json \
 --analysis runs/urban-ns3-dev/staged-pilot06-final-analysis/metrics/urban_analysis.json \
 --output reports/ispras/urban_ns3/pilot_pair
flock -n -E75 /tmp/bas-paper-runtime.lock python3 scripts/product/inspect_relay_path.py \
 --run-dir runs/native-radio-realtime/urban-staged-pilot05-failover \
 --capture-kind native --epoch 2 --telemetry-wait-s 3 --json NEW_REPORT.json
```

Ограничения: идеализированные корпуса/антенны, эмпирические некалиброванные
потери, одна машина, исправные sidecar/runtime. При критическом событии follower
преимущественно удерживали точки; нельзя заявлять одновременный манёвр всей группы.
Отрицательные return-route/deadlock пилоты сохранены отдельно и не заменены этой парой.
