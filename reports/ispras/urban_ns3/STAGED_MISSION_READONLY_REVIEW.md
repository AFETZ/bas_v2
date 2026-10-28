# Проверка поэтапной миссии и первого frozen запуска

28.10.2026, конечная проверка во время выполнения основной серии.
Рецензент является автором метрик, поэтому **не объявляет независимую проверку
собственного анализатора/статистики**. Проверяемая часть создана другими агентами:
mission/barrier driver, root launcher и фактические frozen настройки.
Интеграция оставалась на `4d96d196e29792e8fd508f97994b78251efaee12`, tracked diff пуст.
Во время проверки не запускались Docker/build/native/plots, raw не изменялся.

## Миссия и источник решения

В `relay_experiment.py:682–757` продвижение берёт `telemetry_pose` из доставленного
LOCAL_POSITION_NED. `urban_barrier_ready` проверяет уже зарегистрированные
`waypoint_reached_telemetry` других UAV и свежесть их сетевых poses. Gazebo
GoalObserver/self.poses не являются входом этой функции. Gazebo source clock
используется для горизонта наблюдения/диагностики, не как обход получения позиции.

Барьер1:2 держит исходный relay после высокого южного сегмента, пока UAV2–5 не
подтвердят северную точку2. Барьеры2:3/3:3/4:3/5:3 удерживают followers, пока
UAV1 не подтвердит точку3. Это часть сценария осмотра и поддержки связи, одинаковая
для политик; не изменение критерия выбора relay. При критическом событии followers
преимущественно удерживают точки — одновременное движение всей пятёрки не доказано.

Raw-replay `urban-staged-pilot05-failover` проверил все5 release events: требуемые
предыдущие waypoint marks уже существовали; producer→received→release упорядочены,
producer возраст≤500ms; каждый gated goal_created следует своему release.
У `urban-staged-pilot06-fixed` наблюдён единственный release1:2; четыре остальных
барьера не были обойдены, несмотря на их незавершение до конца окна.

Отдельно проиграны реальные последовательности:

| Проверка | Failover05 | Fixed06 |
|---|---:|---:|
| Marks с непрерывным3s dwell по доставленной телеметрии | 27/27 | 15/15 |
| Completion events с2s dwell по настоящим Gazebo poses | 27/27 | 15/15 |
| Преждевременный gated goal / некорректный release | 0 | 0 |
| WP_SPD до окна, пять MAVLink таблиц | 3m/s у всех | 3m/s у всех |
| WP_SPD после опыта | 3m/s у всех | не записан |

Три секунды route progression и две секунды физической оценки имеют разные
источники времени. При replay повторные samples не продлевают интервал; stale/
разрыв>0,5s сбрасывает удержание. Начальные удержания и поддерживающие waypoint
входят в27; это не27 независимых манёвров. Fixed attempt остаётся failed при полном
measurement window, поскольку последующий LAND всех пяти не завершился.

Для выбранной UAV1/waypoint4 raw цепочка: NLOS518316109385255 →
goal-created518334597219490 → first-command518335328573416 →
first-UART518335333801708 → physical-completion518370054245072 host ns.
Порядок подтверждён, разности18,487834235+0,731353926+0,005228292+
34,720443364=**53,944859817s**. Первый допущенный CID2:1:2340 отличается от
completion CID2:1:2680. Это арифметическая опора raw выбранной миссионной цели;
независимое рецензирование реализации выбора метрики остаётся отдельной задачей.
Большая часть времени здесь обусловлена барьерами/манёвром, а не чистой сетью.

## Freeze и первый настоящий main case

Все шесть frozen config SHA совпали с файлами; nominal и LOS-only JSON совпадают
полностью после удаления `radio.mode`. Наблюдённый native ELF SHA совпал с
`freeze.native_binary.sha256`: `3f2341c4a368553f88d3b13da94e87f968069b824f6fece7fe9d4aa0170281ea`.
Frozen manifest содержит56 cases, из них40 основных,10 RNG блоков. Source/image,
240model s, τ60host s и workers5 объявлены до запуска. Старые town01/Friis/Matrix
значения в `base_environment` являются наследуемой базой; launcher явно задаёт
urban/geometry_logdistance/fault=none,240s и workers5 поверх неё.

Первый main ID `urban20261028-main-nominal-nominal-los_only-failover-r03` длиной56
символов завершён с controller rc0. По реально записанным полям совпали source,
native ELF, config SHA, image, RNG seed1/run3, mode los_only, duration240,
parameter workers5 в config/controller и pilot=false; tracked source чист.
Freeze timestamp предшествует controller start. Авторский run_record при отдельном
read-only вызове также дал primary_eligible=true и пустые provenance/recording
errors; это упражнение контракта, не независимое статистическое подтверждение.
Результаты остальных55 случаев этой проверкой не утверждаются.

## Доказательства и воспроизведение

Детали/хэши: [staged_mission_readonly_review.json](staged_mission_readonly_review.json).
Скрипт не импортирует runtime или metrics модули:

```bash
python3 reports/ispras/urban_ns3/tools/review_staged_mission.py \
 --integration /home/bas/bas_v2-paper-live --output /tmp/staged-mission-review.json
```

Native epoch2 evidence отдельно прослеживает CP→UAV4→followers1/2/3/5 и обратную
телеметрию по actual radiotap captures: `pilot_pair/native_epoch2.json/.md`.
Это четыре выбранных пакетных цепочки; не полный PHY-loss анализ всей серии.
Никакие review артефакты этой задачи не должны интегрироваться в main до завершения
frozen серии; исходный source/executable остаётся её неизменяемой идентичностью.
