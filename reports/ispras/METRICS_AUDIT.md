# Аудит метрик статьи, 28.10.2026

Проверен PDF `paper/build/manuscript_anonymous.pdf` (разделы3–6, таблицы1–3), сохранённый в `bb026a052a2474df29bee3603cc98af27592e207`. Измеренная основная версия `8e16ec223e895c11891e6e7d7c39dff8cee39cac`, отдельная sensitivity18 — `e6453c8`. Новые вычисления не меняют исходные raw, параметры алгоритма или опубликованные числа. Симуляторы и regression tests этим аудитом не запускались.

## Источники и воспроизведение

Первичный пересчёт `recompute_metrics.py` не импортирует production analyzer. Для всех30 frozen run читает только `logs/coordination_gcs.jsonl`, `coordination_uav*.jsonl`, native `fault_start/end`, raw `metrics/relay_control_refresh.csv`, run config/summary. JSONL фильтруются `rg`, PCAP не перечитывается. До запуска проверены уникальные request/CID и пары плана `network/config/relay_campaign_30.csv`; результаты сверены с сохранёнными `campaign-analysis/.../relay_analysis.json` после расчёта, а не использованы как ожидаемый результат алгоритма.

```bash
python3 reports/ispras/recompute_metrics.py --repository /home/bas/bas_v2-paper-live --output reports/ispras
python3 reports/ispras/audit_metric_tables.py --repository /home/bas/bas_v2-paper-live --output reports/ispras
```

Первая команда читает множество raw: выполнять вне нового live-окна. Вторая — лёгкая таблицовая сверка retained exports. Обе нужны для полного набора CSV. Зависимости: Python3 stdlib и `rg`. Все времена — host-monotonic ns→s; отсутствие отказа имеет N/A recovery, а не0.

## Определения и границы проверки

`timely` — точный offered CID имеет GCS→master_receive→master_forward (кроме собственного адресата)→follower_receive→UART-цепочку с теми же UAV,epoch,leader,creation timestamp,payload SHA и target ENU; финальная UART-запись завершается внутри окна за≤250ms от создания команды и внутри выданного lease. Unsent остаётся offered. Это прикладная цепочка журналов; новый all30 native-PCAP аудит не выполнен.

T_UART — минимум по общим `(leader,epoch)` максимумов первого подходящего postfault-created refresh для UAV1…5. T_goal — максимум первых подходящих postfault-created **новых** физических целей пяти UAV: отличающаяся target≥1m, точный CID/goal/UAV/epoch, maneuver/dwell поля и хронология admission→completion. Старые carryover исключены. `metrics_goal_lineages.csv` сохраняет100 индивидуальных исходов (selective и total); no-fault N/A.

Здесь заново проверена первичная completion-lineage, но не выполнен новый покадровый replay поз. Физические displacement/dwell ранее независимо проверены по исходным позам для всех50 selective-целей: сохранённый `runs/independent-review-20261027/stats/physical_first_goal_dwell.csv`, документирован в `doc/LIVE_LEADER_REVIEW_20261027.md`; минимум displacement3,939m, dwell2source s, gap≤40ms. Ни UART, ни ACK сами по себе не объявляются физическим выполнением.

## Таблица1 основной серии: воспроизведена

| Условие | Политика | timely / offered | T_UART mean, s | T_goal mean, s |
|---|---|---:|---:|---:|
| none | fixed |4478/4500 =99,5111%|N/A|N/A|
| none | failover |4478/4500 =99,5111%|N/A|N/A|
| selective | fixed |2957/4500 =65,7111%|62,356160656|67,714723769|
| selective | failover |4420/4500 =98,2222%|**3,963387414**|21,302193034|
| total_GCS | fixed |2957/4500 =65,7111%|61,970666720|67,447539914|
| total_GCS | failover |2964/4500 =65,8667%|62,178413662|67,542971653|

Все30 per-run результатов timely/T_UART/T_goal и общего числа UART совпали с предыдущим расчётом с точностью1e−9; `metrics_replay.json:discrepancies=[]`. Цифра статьи3,963s воспроизводится округлением3,963387414s. Это пять исходов одной миссии, не гарантия≤4s.

Парные разности failover−fixed, по пяти независимым парам каждого условия; exact percentile-bootstrap перечисляет3125 целых парных ресэмплов с linear-interpolated quantiles:

| Selective endpoint | Средняя разность | Разведочный95% интервал |
|---|---:|---:|
| timely/offered |+32,511111 п.п.|[32,266667;32,755556] п.п.|
| T_UART |−58,392773241s|[−58,798919910;−57,993935788]s|
| T_goal |−46,412530735s|[−46,810213130;−46,015671388]s|

`metrics_pairs.csv` содержит все45 planned endpoint-points, `metrics_intervals.csv` —9 оценок, включая N/A no-fault recovery. Равенство средних none не означает эквивалентность: interval timely-разности [−0,5111;+0,4889]п.п. Точный перебор bootstrap не гарантирует95% coverage при n=5; пакеты/UAV не являются независимыми повторами.

## 27000 предложений и75058 допусков — разные множества

| Множество уникальных событий основной серии | Число |
|---|---:|
| Предложенные request `(run,request_id,UAV)` |27000|
| Предложения с выданным CID / без отправки |22472 /4528|
| Target-bearing UART внутри измерительного окна |22254|
| Target-bearing UART вне окна |636|
| Non-target UART внутри окна |22335|
| Non-target UART вне окна |29833|
| Все `uart_admitted` / уникальные `(run,UAV,CID)` |**75058 /75058**|

Сумма последних четырёх категорий75058. В знаменатель timely не входят служебные heartbeat/команды подготовки, возврата Guided, arming, взлёта и LAND, но они проходят тот же UART gate. Code evidence: `relay_experiment.py:pump` отправляет heartbeat каждому UAV каждую wall-секунду; setup/restore/landing выполняют отдельные команды. `non_target` классифицирован строго по отсутствию зарегистрированного ENU-target, а не по угаданному типу MAVLink из размера пакета. Точного поштучного разделения этих52168 non-target на MAVLink types в этом аудите нет.

Первичная проверка всех75058 допусков выявила0 duplicate `(UAV,CID)` и0 несоответствий выданному grant, адресу, сроку записи или age250ms; это не проверка native-доставки всех75058 кадров. Локальная BRAKE-запись sidecar имеет другой event `hold_uart_written` (`service.py:write_hold`) и не добавляется к `uart_admitted` или offered.

Отдельный CID-join сохранённых command CSV с raw offered (`metrics_sent_intersection.csv`) подтверждает:22472−22254=**218** отправленных CID не имеют допущенной команды даже вне measurement window в command export. Они не названы автоматически потерянными радиопакетами: rejection/lease/очередь — другие возможные причины.636 target-admissions вне окна принадлежат другим CID и не являются этими218 «поздними успехами».

## Цели отличаются от refresh и от допусков

По первичному goal_created/offer —**1350** уникальных `(run,UAV,goal_id)`,45/run. Вторичный join raw offers→retained commands/goals (`metrics_goal_coverage.csv`) даёт1200 целей с хотя бы одним sent/admitted/completed и150 целей без отправки/завершения. Во всех шести ячейках225created; completed: none225/225, selective175fixed/225failover, total175/175.218 неудачных CID-refresh не означают218 потерянных физических целей: другой refresh той же цели мог исполниться. Для1200 sent-goals хотя бы один refresh допущен и цель завершена.

## Таблица2: числа совпали, интерпретацию уточнить

`metrics_table2.csv` заново усредняет сохранённое независимое lineage-разложение `paper/data/decomposition.csv`; выбранные goal/UAV и completion-refresh CID совпали с новым primary replay. Это reuse предыдущего расчёта времён выбранной lineage, а не новый pose replay. Fixed:54,968841+7,386555+5,359328=67,714724s; failover:14,972628+0,983371+5,346194=21,302193s. Средний goal→UART splits: fixed7,376517 waiting+0,010038 delivery; failover0,972830+0,010541s. Последняя часть включает processing/relay/UART, не чистую RF latency.

**Предлагаемая правка:** заменить «ожидание создания цели54,969s» на «время до создания выбранной первой успешно выполненной postfault-цели». Генератор не молчал весь этот период: ранее созданные цели обновлялись/сменялись, но некоторые не были отправлены в blackout. Отказ при45model s; selected fixed goal обычно создаётся около100model s, тогда как цели около60/80s уже существовали. Это условное разложение выбранного исхода, не причинная сумма независимых задержек алгоритма. Первый CID цели и последний refresh CID перед completion различаются.

## Таблица3: отдельные18, не смешивать с30

Лёгкая сверка сохранённого18-run export даёт те же six-cell means: nominal0,5s UART1,260929/1,270645;2s3,602386/3,943490;10s11,616733/3,939410s (fixed/failover). `metrics_sensitivity_cells.csv` сохраняет counts15completed/3failed и `metrics_sensitivity_intervals.csv` отдельно пересчитывает27-resample интервалы по3 парам. Полного нового raw replay этих18 здесь нет: сохранённый независимый primary review находится в `paper/evidence/followup/all18-independent.json`.

Все18 измерительных окон допустимы; failed после окна остаются failed. Два handover4,123131166 и4,134810747s превышают4s и не исключены. NominalRF не равен actual; различия actual exposure внутри пары и фаза renewal/refresh сохранены в предыдущем анализе. Усреднение новых2/10s с основной60s серией недопустимо. Обобщение≤4s опровергается отдельными18, а узкое утверждение о пяти исходах основной серии подтверждено.
