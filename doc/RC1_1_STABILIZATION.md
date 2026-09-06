# BAS v2 RC1.1 — reporting исправлен, real-time остаётся limited

Дата: 2026-09-06. Решение: вариант B из задания; дополнение к существующему `HANDOVER.md`.
`software_release_status=limited`; `independent_acceptance=pending`; `hardware_validation_status=blocked_external`; `full_tz_status=blocked_external`.
Ветка `feature/bas-v2-rc1-stabilization` создана в отдельном worktree от handover `b90c38a3e1ce42fc8d6ae948eaeb2385822557ef`.
Проверены clean status, local/remote refs и merge-base: handover/release = `263dfd5`, handover/demo = `70c5157`.
Release/demo/handover refs, исходный пакет, восстановленный checkout, старые runs и видео сохранены. Merge main не выполнялся.

## Исправление reporting

Commit **06b22939a6fe18ff9952b70e82da86faebf9027e**: `write_latency_diagnostic_report` получает `native_sources` через существующий
`summarize_native_sources(run_dir, events)` из фактического run. Ошибка NameError воспроизведена на копии сохранённого C/05 serial до исправления.
При неполных границах on/off helper сохраняет конфигурацию и события, но не выдумывает окно измерений: `windows={}` и причина недоступности.
Нет фиктивного PASS, общего подавления исключений или замены отсутствующих measurements нулями. Полные наблюдённые окна считаются прежним способом.
Focused tests исполняют проблемную ветку и запись PNG/Markdown/JSON: **17 passed in 4.43s**; `py_compile` и `git diff --check` прошли.
Покрыты serial/UDP/TCP, failed/limited/diagnostic_complete, источники с заданными CSV samples, отсутствие конфигурации/CSV,
пустые/некорректные данные, только on, только off и обратные границы. Исходные scenario/endpoint данные и статусы проверяются на сохранность.
Команда: `docker run --rm --user 1003:1003 -e MPLCONFIGDIR=/tmp/bas-rc11-mpl -v /home/bas/bas_v2-rc11:/workspace/multiagent_simulation -w /workspace/multiagent_simulation multiagent_simulation:latest python3 -m pytest -q -p no:cacheprovider network/tests/test_native_latency_report.py`.
Host Python не содержит pytest/matplotlib; проверки и окончательное пересоздание выполнены в уже поставленном image, без установки зависимостей на host.

Три отчёта пересозданы в отдельном `corrected-reports/`: `demo-05-serial-20260905T072003Z`, `demo-05-tcp-20260905T072128Z`, `demo-05-udp-20260905T073418Z`.
У каждого исходный runner exit **2**; исходный diagnostic status отсутствовал (`null`), исправленный остаётся `null`, reporter exit **1**.
Отчёт теперь сформирован; simulation PASS не присвоен. `original_report/` сохраняет существовавшие старые outputs, включая traceback; старого `report.md` не было.
`ORIGIN.txt` содержит raw path, исходный exit/verdict, команду и reporting commit. C/05 environment сообщает HEAD `263dfd5` и 18 dirty paths;
demo ref `70c5157` не выдаётся за точный execution HEAD. Raw не редактировались; повторных симуляций/монтажа для reporting нет.

## Сравнение A/01, A/02 и восстановленного B

| Run | Runtime HEAD | Dirty paths | Timeout scale | Gates | steady n | p95 / p99 / sampled max, ms | >50 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A/01 `rc1-customer-final-01` | `547a5364d9a2b0334618391bc33425c57208dfef` | 1 | 1 | 20/20 | 396 | .336842 / 4.379660 / 58.010540 | 1 (0.253%) |
| A/02 `rc1-customer-final-02` | `2fb157be7f9c3da408a976379cb63b616d6248ab` | 0 | 1 | 20/20 | 400 | 16.076663 / 51.124492 / 108.432918 | 6 (1.500%) |
| B `rc1-restored-customer-20260906T100415Z` | `263dfd5b494a3471038af7e79687fd20ae482cfb` | 0 | 5.0 | 19/20 | 395 | 57.760693 / 111.075204 / 129.202154 | 22 (5.570%) |

Steady = строки `realtime_lag` вне `preflight`/`unclassified`, включая неудобные поздние отсчёты. Интервал ровно **0.5 sim s** во всех трёх наборах.
p95: сортировка, линейная интерполяция позиции `(n−1)×.95`; пересчёт совпал с исходными JSON. p99 добавлен только для сравнения.
Sampled max не является гарантированным максимумом всех задержек. Cold start отдельно; окна, sampling и gate не менялись.
Проверенные tracked runtime inputs (scratch C++, patches, runner, scenario/radio/source YAML, tracker/UART, camera injector/fragment, monitor/logger/capture)
между A HEADs и release совпадают; список в `realtime-analysis/source-and-collectors.json`. Личность dirty файла A/01 не сохранена.
Resolved environment radio/solver/cache, predeclared routes/load/gates совпадают. Отличаются HEAD/dirty, timeout scale, RUN_ID/время, ROS domain/partition и временные пути.
Timeout scale умножает deadlines ожидания ACK/позиции/завершения, а не solver, sampling, mission geometry или retry interval; реальная хронология всё же различается.
Зависимости всех environment: ns-3.48 `d2add90b452d600cfb4859baed8e9ea633519447`, Sionna/Sionna RT 1.2.0, Mitsuba 3.7.1,
DrJit 1.2.0, pybind11 2.11.1, cppyy 3.5.0; GCC 11.4.0, CMake 3.31.6, Python 3.10.12. Compatibility patch включён.
Восстановлен поставленный image `sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5`.
Packaged/restore scratch, CMakeCache и build.ninja совпали. Target `ns3.48-upstream-sionna-tap-spike-default`: profile `default`,
`-Os -g -DNDEBUG -std=c++23`, `NS3_BUILD_PROFILE_DEBUG`, `NS3_ASSERT_ENABLE`, `NS3_LOG_ENABLE`, `SIONNA_RT=1`, ccache. Это не доказательство идентичности исторических ELF A.
Per-run ELF/build IDs A не сохранены; одинаковые pins/build logs не устраняют эту неопределённость. Пересборка в RC1.1 не выполнялась.
Штатные камеры: overview 1280×720, obstacle/uav_focus 2560×1440, все 2 Hz; 3 image bridges, один screenshot collector и 8 captures на run.
Сохранены один resource monitor (1 s), 6 tcpdump, tracker 10 Hz, UART batched_trace, native debug log; continuous video процессов нет в snapshots A/B.
Стек CPU 0–7, native 8–31. Runner не задаёт quota/memory cap; фактический parent cgroup/throttling прошлого B не измерен.

| Дополнительная метрика | A/01 | A/02 | B |
| --- | --- | --- | --- |
| Scenario wall duration, s | 198.119 | 199.801 | 197.321 |
| obstructed transit / return transit / land, wall s | 33.562 / 39.535 / 36.608 | 33.652 / 39.650 / 38.386 | 33.612 / 39.483 / 36.654 |
| RTF mean | .996689 | .996043 | .993056 |
| Pose age p95, ms | 19.876 | 33.249 | 30.339 |
| Radio age p95 / max, s | 13.928 / 19.880 | 13.870 / 19.880 | 13.880 / 19.880 |
| Channel computations, включая cold | 539 | 538 | 538 |
| Steady channel compute p95, logger ms | 39.184 | 40.432 | 40.703 |
| First scene load / first channel compute, logger ms | 425.556 / 912.173 | 415.267 / 905.147 | 3202.494 / 3763.593 |

Функциональный результат A/B: пять flight/LAND/auto-disarm, 10 UART, 26 ACK result=0, P2P 100/100, P2MP 20 roots/100 deliveries,
shared uplink 100/100, fairness 1; штатный источник t=100–110 s, 10.5 s no-bypass, cleanup пройдены. В B failed только объединённый RT gate из-за lag.
RTF B mean/p5 .993056/.949876 и pose p95 30.339 ms проходят прежние границы. Raw summary B остаётся failed, make exit 2.

## Причина и точный blocker

**Confirmed:** `GetChannel` синхронно выполняет CalculatePaths/GetNewChannel при cache miss/displacement/TTL. У всех 22 превышений B в предыдущих
0.5 sim s есть 2–7 channel computations. `all_exceedances.csv` содержит каждый отсчёт, фазу, пары, invalidation, предыдущий lag, источник, capture и ближайший monitor.
Распределение B: los_transit 1; obstructed_candidate_transit 2; return_transit 13; land_all 5; no_bypass_stop 1. Все 395 steady samples сохранены.
Например, перед t=126 s пары 2–4, 2–6, 2–3, 2–5, 3–4, 1–2 в sim span ~36.6 ms заняли суммарно ~231 logger ms; lag=129.202 ms.
Повторная загрузка сцены не обнаружена (одна инициализация на run). `PollSionnaPaths → GetParams` только читает cache; лишний solve этим запросом не подтверждён.
Число channel computations A/02 и B одинаково; дублирования одной пары в приведённой серии нет. Стоимость одного steady расчёта близка.
**Supported hypothesis:** серии синхронных обновлений разных пар создают локальное накопление lag; различие фаз этих серий относительно 500 ms sampler
и реальной одометрии/packet arrivals объясняет вариацию sampled p95 лучше, чем рост общего числа solve. Это не установленная причина различия A/B.
22 отсчёта не совпали с frame_receive→PNG_complete интервалом; ближайший endpoint capture ~1.240 s. Source switch удалён ≥3 sim s;
t=107 s попадает внутрь active источника. Эти наблюдения не исключают постоянную нагрузку камер/GPU и последствия помех.
**Unknown:** producer write-blocking, callback backlog/scheduler wait, CPU throttling и GPU sharing. Native CPU ближайших 1 s samples B ~2.95–26.57%
одного core; monitor записал GPU memory, но не GPU utilization/конкурентов/ожидание. Эти числа не доказывают отсутствие contention.
Logger ставит monotonic timestamp при чтении FIFO и пишет строки синхронно; длительности выше включают неопределённость планирования читателя/IO.
**Blocker:** сохранённые данные не разделяют стоимость solve, ожидание CPU/GPU и backpressure журнала внутри конкретных всплесков;
историческая идентичность ELF A тоже не восстановима. Подтверждённой устранимой ошибки runtime/config/build/cache нет, поэтому безопасная конкретная правка не обоснована.
Коротких диагностических запусков: **0**; сохранённые логи не выделили конкретную устранимую операцию для однофакторной проверки. Полных final runs: **0**;
условие их запуска — обоснованный runtime fix — не выполнено. Исторические A не доказывают новый binary; limited сохранён без попыток «до PASS».

## Передача

Дополнение: `/home/bas/bas_v2-handover/rc1.1-2026-09-06/addendum`; вход `README.md`, рядом `BAS_v2_RC1_1_addendum.zip`.
В нём reporting patch/source/tests, corrected reports/original reports, этот отчёт, сравнение и CSV всех превышений, offline analysis script и logs проверок.
Команды применения reporting patch к новой ветке восстановленной установки находятся в README; `git apply --check` на clean release пройден без изменения checkout.
Runtime/OS/image/dependencies не менялись. Физика PHY/MAC/Sionna, 5 БАС/10 UART, геометрия, маршруты, помехи, нагрузка, fidelity, TTL/displacement и gate 50 ms сохранены.
Пакет/image/assets и фильмы не переупакованы. Независимая приёмка pending; физический FC и согласование диапазона применимости/прав CAVISE остаются внешними ограничениями.
