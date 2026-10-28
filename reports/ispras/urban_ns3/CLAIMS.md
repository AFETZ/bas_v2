# Прослеживаемость новых утверждений

Новая серия имеет собственный freeze `4d96d19`; прежние main30/sensitivity18
не используются как доказательство пространственного затенения.

| Утверждение | Код/конфигурация | Первичные данные и расчёт | Граница вывода |
|---|---|---|---|
| Здания действительно влияют на радиоканал | `network/ns3/scratch/native-geometry-loss.h`, `urban_north_inspection.json` | native component probes; actual `geometry_links.csv`, native packet traces | Эмпирические средние потери, не калиброванная радиокарта |
| Радиозатухание не суммируется с полным Friis повторно | `upstream-sionna-tap-spike.cc`, AddPropagationLossModel | static native probe loss, frozen binary identity | Только измеренный geometry-профиль |
| Все пять имеют физические горизонтальные сегменты | `urban_mission.py`, shared scenario routes | axes pilot actual poses; per-run `urban_trajectory.csv`, independently replayed physical completions | Waypoint progress, не сенсорный результат осмотра |
| Маршрут не продвигается по скрытому ground truth | `relay_experiment.py`, `urban_mission.py` | telemetry source/reception ages, waypoint marks, barrier release/goal ordering | Ground truth используется для radio mobility и независимой оценки |
| Новый relay выбирается без подсказки идеального кандидата | прежний probe/lease policy | coordination JSONL с reachability/RTT, выборами и grants | Кандидат должен достичь всех четырёх, включая прежний relay |
| Команда прошла оба радио-перехода и UART | native PCAP + relay command events | `pilot_pair/native_epoch2.json`; обе стороны каждого follower hop, совпавший MAVLink payload | Подробный PCAP proof — отдельная пилотная пара; не все кадры56опытов |
| UART и физическое выполнение различаются | existing relay audit + urban analysis | target CID stages, strict pose dwell, goal/CID/epoch lineage | Receipt/ACK не доказывает достижение цели |
| Смена не перекрывает старое полномочие | unchanged grant fencing/final UART checks | issued-grant intervals, admission audit каждого сохранённого запуска | Наблюдённые аномалии/их отсутствие, не доказательство для всех отказов |
| Сравнение политик использует независимые повторы | frozen40main four-case blocks | `analyze_urban_study.py`, pairs/interactions, missingness bounds | Малое n, один хост, условная радиомодель |
| Live обеспечен на измеренном профиле | pinned five-SITL/native runtime | actual clock/RTF/native lag/resources | Headless; не hard real time и не GPU-ускорение |

Статические3m зазоры плановых сегментов не доказывают отсутствие столкновений
при произвольном торможении. 27 waypoint включают начальные/подготовительные точки;
частые refresh удержания не считаются новыми осмотренными объектами. Во время
критического события follower преимущественно удерживают позиции. RF-isolation HOLD
предполагает работающие sidecar/runtime; при полном RF-разделении новый relay может
не существовать. Геометрический NLOS не равен потере команды.
