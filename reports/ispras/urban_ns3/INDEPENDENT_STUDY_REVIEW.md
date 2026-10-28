# Независимая численная проверка городской серии

29.10.2026. Reviewer GPT-6 Astra Ultra не создавал проверяемые анализаторы. Исходная серия: `4d96d196e29792e8fd508f97994b78251efaee12`, freeze `runs/urban-ns3-20261028/freeze.json`; все пути raw ниже относительно интеграции `/home/bas/bas_v2-paper-live`. Замеры и исходный `results/` не изменялись. Отдельный `results-final/` содержит последующие уточнения семантики, а не новые опыты.

Числа первичных исходов воспроизводятся. Собственный [stdlib-скрипт](tools/review_final_study.py) без импорта BAS-анализаторов проверил 624 соотношения: 56 контроллеров/raw summaries/per-run exports, знаменатели, все 36 парных контрастов, шесть interaction-контрастов и первичные missing bounds. Численных расхождений нет. [JSON](INDEPENDENT_STUDY_REVIEW.json) сохраняет значения, SHA256, область проверки и замечания R1–R6. Исходные per-run анализаторы и исходный агрегат соответствуют frozen-версиям; текущий агрегатор версии 3 является отдельной postseries-правкой.

Всего 56 попыток: main 40, sensitivity 12, engineering boundaries 4. Статусы: 36 completed / 20 failed. У 19 failed причиной в summary указан неудачный landing/disarm после полного окна; эти измерения допустимо сохранить, но нельзя переименовать попытки в completed. Один crash внутри окна оставляет **55 полных окон**, main 39: geometry — 9 полных пар, LOS-only — 10, interaction — 9 полных четырёхслучайных seed blocks. Unplanned/replacement попыток в агрегате нет.

## Воспроизведённые основные числа

Средние ниже используют доступные полные окна каждой ячейки; единица — запуск.

| Радио / политика | Полные окна | Timely / offered, % | Выполненные конфигурационные waypoint, % | Все маршруты завершены |
|---|---:|---:|---:|---:|
| Geometry fixed | 10 | 34,6583 | 54,8148 | 0/10 |
| Geometry failover | 9 | 96,4722 | 99,5885 | 8/9 |
| LOS-only fixed | 10 | 99,5583 | 100,0000 | 10/10 |
| LOS-only failover | 10 | 99,4083 | 99,6296 | 9/10 |

Парные эффекты failover−fixed и interaction `(F−X)geometry−(F−X)LOS-only`, процентные пункты:

| Показатель / контраст | n | Эффект | 95% exploratory bootstrap | Bounds по всем 10 planned blocks |
|---|---:|---:|---:|---:|
| Timely, geometry | 9 | +61,8333 | [61,4630; 62,1389] | [52,1667; 62,1667] |
| Timely, LOS-only | 10 | −0,1500 | [−0,3167; ≈0] | [−0,1500; −0,1500] |
| Timely, interaction | 9 | +61,9630 | [61,5926; 62,3519] | [52,3167; 62,3167] |
| Waypoint, geometry | 9 | +44,8560 | [43,6214; 46,0905] | [34,8148; 44,8148] |
| Waypoint, LOS-only | 10 | −0,3704 | [−1,1111; 0] | [−0,3704; −0,3704] |
| Waypoint, interaction | 9 | +45,2675 | [44,4444; 46,5021] | [35,1852; 45,1852] |

Все 18 main-интервалов независимо воспроизведены по 50 000 resamples целых пар/блоков, seed 20261028. В geometry-парах fixed тоже имеет n=9: разность двух непарных средних из первой таблицы не заменяет парный эффект. Bounds используют ограниченность исходов [0,1] и весь planned набор; они не CI и не заполнение отсутствующего исхода. Малые n и условность complete-case интервалов сохраняются; пакеты, UAV и эпизоды n не увеличивают. Sensitivity n=3 на вариант и boundary n=1 остаются отдельными описательными сравнениями.

## Проверка выбранной исходной цепочки

Для `urban20261028-main-nominal-nominal-geometry-{fixed,failover}-r03` независимо прочитаны исходные coordination JSONL. Сопоставлены run/UAV/CID/epoch/leader, actor, payload hash, target, создание CID, выданный НПУ grant и его срок на UART write start/end/final check. Проверены все целевые CID этой пары; повторных target UART, несовпадений lineage и перекрывающихся grants разных scopes не найдено. Это выборочная проверка двух запусков, не независимая проверка всех допусков всей серии.

| Стадия r03 | Fixed | Failover |
|---|---:|---:|
| Все offered refresh opportunities | 1200 | 1200 |
| Созданные target CID | 433 | 1176 |
| Master receive | 422 | 1163 |
| Forward observed / required | 336/345 | 928/939 |
| Application follower receive | 414 | 1158 |
| Валидный target UART / timely | 414 | 1156 |
| Уникальные физические waypoint по per-run export | 15/27 | 27/27 |

`timely` означает валидный UART не позднее 250 host ms после создания CID; знаменатель включает все offered, в том числе без CID. Receipt не равен UART, а повторные CID не равны новым целям. В selected pair выполненный waypoint включает исходные/вертикальные позиции: только 9/12 соответственно имеют горизонтальное перемещение ≥5 m; «27 горизонтальных манёвров» неверно. Все 56 полных/частичных opportunity exports также проверены: случаев timely по CID, но >250 ms от scheduled opportunity, не найдено.

Для пяти first-success new goals в failover r03 отдельно воспроизведены raw `node_state.jsonl`: реальные ENU-позы, frame/freshness, target tolerance, скорость, непрерывность одной source-clock session и удержание ≥2 source s. Максимальный промежуток 40 ms. Все goal/CID/epoch/target lineage и chronology совпали. Первый допущенный CID отличается от refresh CID в момент completion; замена одного другим исказила бы разложение.

## Время, пропуск и границы интерпретации

- **R1, interrupted applicability.** Order 29 `geometry-failover-r01` завершился без наблюдённого NLOS после 70,238974 host s неполного окна. Исходные `not_applicable_no_trigger`/`not_applicable_no_observed_NLOS_transition` смешивали прерывание с полным окном без события. Для restricted endpoint версия 3 корректно сохраняет null и `interrupted_before_observed_trigger`, выделяет unknown applicability, убирает seed1-r01 из N/A. Численные [0,60] bounds по всем десяти нельзя получить без дополнительного предположения о наступлении trigger; primary [0,1] bounds этого предположения не требуют.
- **R2, отсутствующие final stats.** В crash отсутствует `metrics/native_radio_stats.json`. Исходная подпись «mode differs» не доказывала mismatch; версия 3 отличает отсутствие сведений. В `logs/ns3_sionna.log` действительно записаны hard real-time jitter 31 790 122 927 ns и `NS_FATAL`. Причина задержки хоста не установлена; приписывать её диску, логированию, GPU или другой нагрузке нельзя.
- **R3, короткая окклюзия.** В boundary `short_occlusion` только failover вернулся в LOS через 6,920123365 host s. Fixed остался NLOS до конца followup 200,804595960 s. Это наблюдаемое влияние политики на траекторию и восстановление связи, не парный одинаковый экзогенный обрыв заданной длительности.
- **R4, origin времени.** Во всех девяти полных main geometry failover first-post-NLOS UART — 4,203750–4,256496 s, mean 4,234080 s: все >4 s. От фактического `direct_GCS_probe_timeout` до all-five UART — 3,249442–3,446396 s, mean 3,321975 s. В r03 соответственно 4,228029900 и 3,279015235 s. Это разные начала отсчёта; геометрический NLOS сам по себе не доказывает service outage. В LOS-only геометрический trigger сохраняется как абляционная отметка, но изменения радио и recovery из него не следуют.
- **R5, T_goal conditional.** Main geometry failover имеет 9 наблюдений all-five first-success new-goal, mean 53,824648 s; fixed — 0, причём во всех десяти fixed нет новых post-trigger целей. Это условная характеристика выбранных успешно завершённых целей, не paired mean recovery и не чистое время полёта. В r03 critical UAV1: 53,942335968 = 18,393202566 ожидания создания выбранной цели + 0,830501819 до первого успешного refresh CID + 0,016934973 до UART + 34,701696610 до completion. Все слагаемые на host monotonic; только физический dwell проверяется отдельно в source time. Нельзя складывать максимумы разных UAV либо считать последний интервал доказательством непрерывных полномочий/движения.
- **R6, полнота запуска.** 55 eligible windows не равны 55 успешным полным циклам. Whole-attempt failures, UART availability и physical route completion остаются разными исходами. В `no_candidate` обе политики имеют 0 новых relay selections, восстановление all-five не наблюдалось до конца окна; это цензурирование и boundary n=1, не нулевое время и не доказательство универсальной гарантии.

Проверка версии 3 отдельно сравнивает все per-run outcomes, paired estimates и interaction estimates с исходным JSON: численные словари неизменны. **R1/R2 исправлены в окончательном экспорте:** все 13 endpoints crash имеют статус interruption с сохранённым `original_status`, вложенные UART/detection согласованы, отсутствие profile/fault stats больше не названо несовпадением/наличием fault. SHA256 и детали — `reviewed_overlay` JSON; исходные per-run exports сохранены. Полная независимая raw-проверка 56 полётов, native PHY/PCAP, параметрических снимков и аппаратных гарантий здесь не выполнялась. CPU/GPU-нагрузка и причина crash не устанавливались.

Воспроизведение: `python3 reports/ispras/urban_ns3/tools/review_final_study.py --root /home/bas/bas_v2-paper-live --output /tmp/urban-independent-review.json`. Скрипт читает исходные `results/`, имеющийся `results-final/` и raw выбранной пары; симуляторы, Docker и сборки не запускает.
