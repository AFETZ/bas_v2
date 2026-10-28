# Независимая проверка фактических frozen inputs

28.10.2026. Проверка выполнена во время основной серии, без изменения интеграции.
Observed integration HEAD: `4d96d196e29792e8fd508f97994b78251efaee12`.
Freeze: `runs/urban-ns3-20261028/freeze.json`; исходные freeze/validation/raw сохранены.
Независимый stdlib-скрипт не импортирует проектный planner/validator/анализатор.

| Фактическая проверка | Результат |
|---|---|
| Матрица | 56 уникальных planned run IDs, порядки1…56:40 main+12 sensitivity+4 engineering |
| Main | 10 полных блоков, в каждом ровно `(fixed/failover)×(geometry/LOS-only)` |
| Независимые seeds | 10 различных пар(seed1,RngRun1…10); все четыре случая блока имеют одну пару |
| Парность | Внутри блока одинаковые horizon/scenario/parameter variant; configSHA пары каждой радиомодели одинаков |
| Порядок | Полностью воспроизведён собственным кодом через Random20261028/20261029 |
| Радио-абляция | Все значения двух настоящих JSON совпадают после удаления только `radio.mode`, включая физику сцены, маршруты и коэффициенты |
| Источники | SHA256/размеры всех приложенных scenario/review/pilot-analysis/base-environment файлов совпадают с freeze |
| Native executable | Фактический9 058 304-byte бинарник совпадает с frozen SHA256 `3f2341c4a368553f88d3b13da94e87f968069b824f6fece7fe9d4aa0170281ea` |
| Первый main runtime | environment содержит тот же post-build binarySHA, frozen HEAD и dirty0 |
| До выполнения | Host timestamp freeze предшествует started timestamp первого controller |
| Определения | Общий горизонт240model s; дополнительный restricted endpoint60host s; physical hold2source s, route hold3autopilot s |
| Ресурсы после окна | Frozen LAND deadline60s и полные pre/post parameter reads с5workers объявлены заранее |

## Пилоты и native доказательство

Первичные phase/offer/action/config записи двух настоящих пилотов подтверждают
полные240model-s окна и1200 offered каждого. Физический hold2s и route hold3s
проверены по настоящим сохранённым args/JSON, а не только подписи в тексте.
У failover raw27 `action_completed`, у fixed15; это здесь счёт первичных событий,
полный независимый replay всех их физических dwell в эту узкую проверку не входит.
Failed LAND фиксированного пилота после окна сохранён вместе с ошибкой;
эти пилоты не являются основной серией и не заменяют её будущих пропусков.

Проверен `pilot_pair/native_epoch2.json` против настоящих gcs_send, uart_admitted,
uart_observed и gcs_receive. Для follower1/2/3/5 указаны реальные native radiotap
файлы обоих endpoint каждого перехода, совпадающие payload bytes/hash и UART.
Возвращённая телеметрия сопоставлена с первичными UART/GCS hashes; за ACK её не выдаём.
В native observations присутствует `pcap_sim_s`, а не ложная Unix-шкала.

| CID | UAV | created после CP1 NLOS, s | UART write-end после CP1 NLOS, s |
|---|---:|---:|---:|
|2:1:2190|1, прежний relay|4,220377784|4,232879016|
|2:2:2191|2|4,220589658|4,233805569|
|2:3:2192|3|4,220847537|4,235325154|
|2:5:2194|5|4,221029656|4,236257156|

Начало NLOS заново найдено по raw переходу CP↔UAV1 после начала окна.
Scope всех этих команд — новый relay4/epoch2; их timestamps/bytes подтверждены raw.
Значение follower5 совпадает с4,236257156s в пилотном отчёте; превышение4s сохранено.
PCAP повторно целиком не сканировались: проверена согласованность уже полученного
native-derived proof с первичными журналами и настоящими capture filenames.

## Воспроизведение и ограничения

```bash
python3 reports/ispras/urban_ns3/tools/review_frozen_inputs.py \
 --root /home/bas/bas_v2-paper-live \
 --freeze /home/bas/bas_v2-paper-live/runs/urban-ns3-20261028/freeze.json \
 --output NEW_REVIEW.json
```

JSON содержит индивидуальные проверки, hashes входов и собственного скрипта.
HEAD-проверка описывает момент активной серии; после разрешённых последующих commits
текущий HEAD закономерно может измениться — прежний snapshot/runtime metadata остаётся источником версии.
Docker/image-inspect, builds и полные PCAP scans не выполнялись; image проверен как frozen metadata.
Содержательных блокеров замороженного дизайна не обнаружено. Новых main-результатов и
общей гарантии физической точности эмпирической радиомодели эта проверка не устанавливает.
