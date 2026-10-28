# Первый городской пилот: восстановление и последующее разделение группы

Разбор 28.10.2026, **один инженерный failover-пилот, не основная серия**.
Raw: `runs/native-radio-realtime/urban-block-pilot01-failover` в интеграционной
копии `/home/bas/bas_v2-paper-live`; source `281b6ed9083c5f3c9379c6316a15ab3b9065dca7`,
recorded dirty paths=0. Модель `hypothesis_nominal_v1`, 2,412 GHz,
LOS exponent2/NLOS3 — некалиброванные гипотезы, физические корпуса A/B из raw
`logs/urban_scenario.json`. Никакого Matrix/drop-by-schedule в этом сценарии.

Измерительное окно полностью наблюдалось: 300,009 model / 300,182069543 host s.
Статус **failed** сохранён: после окна штатная посадка/auto-disarm состоялись
у четырёх аппаратов; UAV5 не удалось посадить по доступному сетевому пути.
Это не crash измерительного окна и не основание переименовать попытку в completed.
Cleanup явно начинается после окна; его события не включены в recovery.

## Наблюдаемый механизм первой смены

Все времена ниже — host seconds от начала измерения. Геометрический переход
имеет интервал наблюдения, а не точность до наносекунды физического пересечения.

| Событие | Время, с | Первичный источник |
|---|---:|---|
| CP→UAV1 LOS→NLOS, корпус A | 20,725750933 | geometry CSV; bracket79,781ms |
| GCS обнаружила timeout исходного relay1 | 21,512638204 | `link_failure_detected` |
| Максимальный expiry ранее выданных epoch1 grants | 23,255678364 | все `grant_issued` |
| Первый реконструированный допустимый кандидат UAV3 после detection | 27,082304713 | received probes + peer timestamps |
| Выдача назначения UAV3/epoch2 | 27,083008129 | `grant_issued(reason=selection)` |
| valid_from нового назначения | 27,082978324 | тот же grant560 |
| Наблюдение активации epoch2 на UAV5 | 27,104189614 | UAV5 `leader_activated` |
| Первое подходящее обновление допущено всеми пятью под epoch2 | 29,019040254 | UART write-end одного CID на UAV |

Первый общий post-NLOS UART latency = **8,293289321 s**; здесь отдельно
подтверждён direct-probe timeout, поэтому имеется реальное нарушение обмена.
Промежуток detection→selection **5,570369925 s** нельзя называть ожиданием lease:
к моменту появления кандидата старые полномочия уже истекли. Replay probes не
находит допустимого кандидата при detection; через5,570s свежие peer-подтверждения
ко всем четырём появились у UAV3. Между этим probe log и selection около0,703ms.

Candidate replay использует log receipt как прокси внутреннего присваивания
receipt, происходящего несколькими инструкциями раньше; микросекундные границы
политики не заявляются. Проверяются все три direct timestamps и четыре peer ages,
каждое≤1s. Геометрический LOS сам по себе не признаётся доступностью канала.
[Все checkpoint ages/reasons](pilot01_negative/candidate_freshness.csv).

Критический UAV5 CID `2:5:2041`: реальные `gcs_send→master_receive3→master_forward3
→follower_receive5→uart_admitted5`; payload/target/epoch/issued grant проверены
общим audit. Точный UART write-end отличается от более позднего log event.
[Машинный итог и CID-chain](pilot01_negative/summary.json).

## Почему затем потерян UAV5

Второй переход relay3↔UAV5 в NLOS через A произошёл на **41,345962103 s**,
после ухода резервного борта с северной стороны к конечной точке[15,0,16].
CP↔UAV5 уже был NLOS через B с19,466239s; CP↔UAV3 оставался LOS.
Последний target UART UAV5 зарегистрирован на **41,027915001 s**; далее до
конца300s новых target admissions UAV5 не было. На43,220065s его sidecar
наблюдает BRAKE. Последняя поза **внутри измерения**: [129,9991;71,4321;20,0244] ENU m.

При43,345962s реконструкция ещё находит допустимые UAV1/UAV2, но текущая
политика запускает смену только при недоступности **прямого GCS→current relay**.
UAV3 продолжает получать renewals; потеря одного follower не является её trigger.
К концу окна свежего peer5 уже нет у всех кандидатов. Это показывает совместное
ограничение moving topology и существующего trigger; нельзя утверждать, что
обходного пути не существовало во все моменты после второй потери.
Изменение алгоритма для лечения этого случая в данном пилоте не выполнялось.

## Управление, миссия и live — разные результаты

1500 offered,1212 sent target CIDs,1200 timely UART: timely/offered=**0,8**;
288 unavailable offers:20 `no_active_issued_lease`,268 ожидали свежий GUIDED
heartbeat. [Раздельные фазы](pilot01_negative/offer_phases.csv),
[последние target UART пяти UAV](pilot01_negative/last_target_UART.csv).

24 waypoint marks пришли в GCS телеметрии;23 `action_completed` независимо
перепроверены по Gazebo poses/dwell/goal-CID/lease/configured target.
Для `waypoint2:uav3` есть telemetry mark, но нет физического completion event;
эти два счётчика не объединяются. UAV5 завершил4/5 точек по обоим источникам.
Доля завершённого маршрута не равна доле своевременных refresh удержания.

Измеренный integral RTF0,9994177331, p5 примерно1s окон0,9974873782;
native lag p95/max0,5198/4,6838ms. Все пять реально двигались горизонтально.
Одного пилота недостаточно для парного эффекта политики или доверительного интервала.

## Воспроизведение

Из корня интеграции (только чтение raw, exporter пишет отдельно):

```bash
python3 scripts/product/analyze_urban_experiment.py run runs/native-radio-realtime/urban-block-pilot01-failover --output /tmp/urban-pilot01-analysis
python3 reports/ispras/urban_ns3/tools/review_pilot_topology.py runs/native-radio-realtime/urban-block-pilot01-failover --analysis /tmp/urban-pilot01-analysis --output /tmp/urban-pilot01-topology
```

Source SHA256 и analyzer provenance — в summary JSON. [Таймлайн всех переходов,
назначения, активаций и HOLD](pilot01_negative/timeline.csv). Новая северная
миссия должна иметь отдельный config/pilot/freeze; этот отрицательный исход
сохраняется и не заменяется более удобной последующей попыткой.
