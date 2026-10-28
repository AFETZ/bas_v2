# Независимый replay осевого пилота

28.10.2026. Raw: `/home/bas/bas_v2-paper-live/runs/native-radio-realtime/urban-axes-pilot01`.
Runtime `829e4d1751eb3ba4a2773747eea017d67a398094`, dirty paths0 по сохранённому environment.
Проверены первичные JSONL/CSV собственным stdlib-скриптом без импорта продуктового анализатора.
Это квалификация пяти горизонтальных полётов в radio `los_only`, не городской NLOS-эксперимент.

| Проверка | Результат |
|---|---|
| Полный physical lifecycle | Пять ARM-HEARTBEAT, реальный подъём, LAND-mode, реальные наземные позы и disarmed LAND-HEARTBEAT |
| Оси | У всех пяти выполнены +X, −Y, +Z, −X; положительный Y этим маршрутом не проверялся |
| Перемещение после первых UART каждой цели | +X9,824…9,836м; −Y9,830…9,849м; +Z3,967…3,991м; −X9,846…9,877м |
| Уникальные waypoint completions | 25/25 независимо удовлетворяют XYZ/speed/dwell; стартовая точка0 — удержание, не новое горизонтальное движение |
| Реальное удержание | По deduplicated Gazebo poses: ≥2source s, XY≤3м, |Zerror|≤1,5м, speed≤1м/с; max gap40мс |
| UART | 2139 уникальных допусков за полный runtime;0 повторов CID и0 нарушений записанной issued-grant/age/write-lineage |
| Полномочия | 477 выданных grant, один epoch/relay;0 конфликтующих интервалов полномочий |
| Измерительные обновления | 450 offered,450 created,446 admitted и timely:99,1111%; четыре не получили UART-допуск |
| Latency created→write-end | mean7,049мс,p95=14,266мс,max32,214мс; это не чистая RF-задержка |
| Координатная реконструкция | 2251 LOCAL_POSITION_NED sample;0 ошибок пересчёта объявленного N=y/E=x/D=−z и anchors |
| Telemetry estimate против ближайшей actual pose | Средняя Euclidean ошибка по UAV0,035…0,038м; максимальная0,1513м |
| Сопоставление по времени | Ближайшая actual pose к UART producer host-time: p95=16,974мс,max20,384мс |
| Live | Наблюдаемый source-span90,014s/wall90,06883046s:RTF0,999391; p5≈1s окон0,997647,min0,995269 |
| Native lag | 1801 samples в окне;p95=2,019619мс,max5,849444мс |
| Геометрические строки | 36090 записей, все geometric LOS и radio LOS; влияние здания в этом пилоте не испытывалось |

Сопоставление телеметрии с actual включает ошибку оценивания SITL и временного выравнивания;
это не измерение одной лишь ошибки математического преобразования координат.
Online флаг `maneuver_observed` не использовался как доказательство; завершения повторно проверены по actual позам.
Измерительные окно и цели взяты из raw событий, а не готовых summary-чисел.

В конце у всех пяти есть `service_failed: native radio watchdog expired or disappeared after readiness`.
Их timestamps514807323619957…514807327387734ns расположены после закрытия измерительного окна
514773464403640ns и после последнего disarmed LAND-HEARTBEAT514807254447464ns.
Эти события сохранены; наблюдаемое окончание watchdog не является падением внутри измерительного окна.

Проверка UART сверяет все записанные допуски с реальными GCS-issued grants и gcs_send,
возраст на финальной проверке≤250мс и действительность authority от начала записи до final check.
Полное независимое декодирование PCAP/tlog и каждого UART payload в эту узкую проверку не входит.
Недоставленные четыре refresh не объявляются четырьмя потерями RF-пакетов без разбора других стадий.

Воспроизведение из интеграции, без симуляций:

```bash
python3 reports/ispras/urban_ns3/tools/review_axes_raw.py \
  runs/native-radio-realtime/urban-axes-pilot01 \
  --output reports/ispras/urban_ns3/axes_pilot_independent_review.json
```

JSON хранит индивидуальные цели/перемещения, lifecycle, время final-write, сводные метрики,
SHA256 первичных входов и самого скрипта. Полные raw сохраняются в исходном каталоге.
