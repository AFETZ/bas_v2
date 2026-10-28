# Дополнительные эксперименты и границы полезности

28.10.2026. Основная30 и sensitivity18 не повторялись. Новая серия — инженерные вертикальные пробы с неизменёнными policy/service/autopilot/PHY/MAC; runtime source первых6 `bb026a052a2474df29bee3603cc98af27592e207`; дополнительной2с пары `ffb720c` (изменились только reports, runtime diff пуст; tracked dirty касается только аудита). Коммиты интеграции аудита после запусков меняют только reports. Это не obstacle-aware/NLOS-опыт.

## Методика и исходы

НПУ не получает прямые UDP-ответы UAV4: stock iptables INPUT DROP source10.71.4.10 в изолированном ams-gcs namespace. Все исполняемые команды продолжают идти через native ns-3/выбранный relay; фильтр не создаёт обхода сети. UAV4 жив, peer-доступен и выполняет задания. Это недоступность кандидата для прямого НПУ, не полная изоляция/остановка UAV4. Probes/grants остаются служебным прямым трафиком согласно исходному алгоритму.

По одной fixed/failover паре для nominal2/5/10/30с, ns-3 seed1/run27; окно90 model с, fault30…30+D, task20 model с, refresh1Hz wall, Friis+native Matrix200dB на CP↔initialmaster1. Наземные pre/post-параметры здесь не выгружались (snapshot flag0); измеренные параметры не приписываются этим новым raw. Арминг и сроки не ослаблялись. CLI --pilot позволяет новые длительности; это не новая подтверждающая main matrix. Доверительных интервалов при n=1 нет.

Сначала выполнена10с пара. Дополнительные5/30с с теми же условиями и порядком fixed→failover зафиксированы в additional_candidate_plan.json до их запуска; исходники контроллера и команды/события — controllers/. Предыдущий10с пилот не объявляется заранее запланированной частью последующего freeze. После первых6 проб отдельно заморожена2с пара: candidate_two_second_plan.json. Она выполнялась после просмотра предыдущих исходов, без изменения алгоритма/сроков; весь набор не объявляется единой preregistered серией. Старая2с sensitivity18 (3пары, другая фаза/180с, без фильтраUAV4) остаётся отдельно.

| Nominal, с | Политика | Timely/offered, % | T_UART, с | T_goal, с | Новый relay/epoch |
|---:|:---|---:|---:|---:|:---|
| 2 | fixed | 96.889 | 3.926561755 | 16.102801393 | нет |
| 2 | failover | 96.222 | 3.930176449 | 16.125041515 | 2/2 |
| 5 | fixed | 92.000 | 6.951121511 | 16.146704749 | нет |
| 5 | failover | 96.222 | 5.932035904 | 16.146334411 | 2/2 |
| 10 | fixed | 87.778 | 11.939128149 | 17.109611242 | нет |
| 10 | failover | 96.222 | 3.926807204 | 16.108075025 | 5/2 |
| 30 | fixed | 64.889 | 31.934998632 | 56.108839458 | нет |
| 30 | failover | 95.333 | 3.932207218 | 16.118397609 | 2/2 |

**Восемь completed**, все пять аппаратов: takeoff/два уровня целей/LAND-disarm. 450 offered/run,3600 всего. Сохранены реальные RF start/end, все CID и роли. [Полная таблица](data/candidate8_runs.csv), [таймлайн](data/candidate8_timeline.svg), [независимый pose/CID review](data/candidate8_independent_review.json).

Проверены 14670 UART-допусков:0 просроченных/вне записанного lease,0duplicateCID; в выданных grants нет пересечения сроков разных лидеров. Product analyzer дополнительно проверил lineage/issued-authority; все endpoint-числа совпали с независимым replay. Прямых echoUAV4 во время отказа0 во всех8; в10с пробах один echo мог пройти до установки фильтра — нельзя писать 'ни одного за весь startup'.

Независимо по Gazebo poses проверены40 выбранных новых физических целей пяти аппаратов: минимальное вертикальное перемещение 3.991354м, наблюдаемый dwell≥2.000source с, maxgap≤0.040с. Исходный критерий допускает speed≤1м/с (наблюдаемый max0.999917), |z-error|≤1,5м и XY-error≤3м; это не строгая неподвижность. Показания не получены из ACK; raw poses и проверенные критерии — data/candidate8_physical.csv.

При2с срок нового lease начинается через2,518367с, после фактического RFend1,999655с и свежего ответа прежнего канала2,146773с. T_UART3,930176с почти равен fixed3,926562с, timely ниже на0,667п.п.; это потенциально лишняя смена, не доказательство отсутствия любой пользы. Фаза сохранена в data/candidate8_fault_phase.csv.

При5с new relay2 выбран и его lease начался до RFend, но общее UART-восстановление заняло5,932с, уже после RFend; новое превышение4с не исключено. При10с выбранUAV5, при30сUAV2. Это показывает выбор альтернативыUAV4; не означает оптимальность по RSSI/геометрии: алгоритм ранжирует доступных по последнему прямому RTT.

При5с разность T_goal около−0,000370с не доказывает эквивалентность; при30с заметная физическая разность зависит от пропущенных целей и20с cadence. Изменение UART-доступности нельзя напрямую переводить в скорость выполнения новой цели. Отдельная2с sensitivity18 уже показывает потенциально лишние переключения после RFend/старого echo; не смешивать с новой geometry/filter конфигурацией.

## Сохранённые отрицательные попытки

До INPUT-only пробы два новых bidirectional INPUT+OUTPUT DROP запуска завершились failed до измерительного окна: Linux sendto возвратил EPERM/PermissionError на OUTPUT DROP; Network.send ловит только BlockingIOError. Это ошибка/граница локального transport path, не радиофейдинг и не неудачное переназначение в полёте. Raw сохранены как paper-candidate4-unavailable-{fixed,failover}-20261028; не включены в средние восьми completed. Алгоритм не исправляли; изменили явно объявленную инъекцию на недоступные входящие ответы, сохранив предыдущие результаты.

## Городской сценарий

Работающий профиль по-прежнему Friis без препятствий. Actual LOS/NLOS entry/exit, attenuation зданий и доля команд после выхода из-за здания **не измерены и N/A**, не ноль. Нельзя использовать Matrix fault_end как выход из NLOS. Отдельный реальный configuration probe failover+Sionna отклонён runner с кодом2 до полёта; urban_relay_guard.json содержит argv/env/output. Он не включён в10 инженерных попыток стенда и не проверяет производительность RT. [Точный feasibility audit](OBSTACLE_FEASIBILITY.md), [план облёта двух корпусов](scenario/industrial_block_plan.svg), [координаты/пять маршрутов](scenario/industrial_block_plan.json). План — новая идеализированная площадка, не измеренный Town01. Нужны отдельный propagation profile, проверка RT/geometry, горизонтальный MAVLink adapter и 3D completion; после этого отдельный freeze и physical pair. До этого оставить в статье limitation/future work.

## Воспроизведение

Полные новые raw — runs/native-radio-realtime/paper-candidate4-unavailable-*; журналы контроллера/анализа — runs/ispras-audit-20261028. Старые raw не изменены. Контроллер с существующими IDs отказывает вместо перезаписи. Для повторения выбрать свежие IDs и сохранить новый manifest.

```bash
cd /home/bas/bas_v2-paper-live
flock -n -E 75 /tmp/bas-paper-runtime.lock python3 reports/ispras/tools/run_candidate_probe.py --input-only --fault-seconds 5
# После завершения runtime: Python3.12 + PyYAML6.0.1 для экспорта, stdlib+rg для review.
flock -n -E 75 /tmp/bas-paper-runtime.lock python3 reports/ispras/tools/analyze_candidate_batch.py
flock -n -E 75 /tmp/bas-paper-runtime.lock python3 reports/ispras/tools/analyze_two_second.py
# Команда review первых6: candidate_analysis_provenance.json; новой2с пары: candidate8_analysis_provenance.json;
python3 reports/ispras/tools/review_candidate_runs.py --help
# После review новой2с пары (точный argv в candidate8_analysis_provenance.json):
python3 reports/ispras/tools/merge_candidate_reviews.py
flock -n -E 75 /tmp/bas-paper-runtime.lock python3 reports/ispras/tools/summarize_candidates.py --prefix candidate8
python3 reports/ispras/tools/export_candidate_phase.py
```

Plot generator требует Matplotlib; выполнен в pinned runtime image/Python3.10 под тем же lock. При остановке ручного runtime сначала LAND/disarm, затем docker stop --timeout 120 bas-v2-native-radio-five-uav. Все новые случаи — однохостовые вертикальные диагностические миссии, не доказательство городской радиореалистичности, crash-tolerance или hard realtime.
