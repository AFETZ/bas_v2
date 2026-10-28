# Анализ городской серии: спецификация до основного freeze

28.10.2026. Это методика и код, **не новые численные результаты**.
Первичные показатели: timely/offered и доля физически подтверждённых
конфигурационных waypoint на общем замороженном горизонте. Завершение всех
маршрутов, число переназначений и RTF — отдельные показатели.

```bash
python3 scripts/product/analyze_urban_study.py --freeze FREEZE_JSON \
  --analysis-root PER_RUN_EXPORTS --controllers-root CONTROLLERS \
  --runs-root runs/native-radio-realtime --output NEW_AGGREGATE_DIR
```

Для каждого planned run требуется `PER_RUN_EXPORTS/RUN_ID/metrics/urban_analysis.json`
и лежащий рядом `relay_audit/metrics/relay_analysis.json`; controllers — JSON
launcher по тому же run ID. Сохраняются все planned строки, attempted/failed,
отсутствующие экспорты и обнаруженные unplanned контроллеры. Последние не
подменяют неудачные planned попытки. Проверяются source/image/clean/RNG/mode,
снимок scenario, policy, отсутствие искусственного отказа и общий горизонт.
До расчётов основной frozen block должен содержать ровно четыре factorial cells,
совпадающие RNG seed/run, горизонт и scenario/parameter variant. Config SHA пары
fixed/failover одного радиорежима одинаков. Auxiliary blocks — ровно две политики
с теми же условиями. Дубликаты/неполная frozen grid отвергаются до статистики;
это отличается от отсутствия результата запланированной попытки. Interaction
группируется также по scenario/parameter variant. Фактический digest executable
из environment после сборки сверяется с `freeze.native_binary.sha256`; пропуск
либо несовпадение сохраняется как provenance error, такой run не объединяется.

Полный корректно записанный measurement window остаётся доступен для расчётов,
если после него не удался teardown: attempt всё равно failed. Crash/пропуски
внутри окна дают отсутствующий fixed-horizon исход, не ноль и не успешный run.
При пропусках приводятся число complete pairs и worst/best bounds по всему
запланированному набору для ограниченных показателей[0,1]. Эти bounds не являются
доверительным интервалом или заполненными наблюдениями.

Основная единица — seed block: четыре случая `(fixed/failover)×(geometry/LOS-only)`.
Разности failover−fixed вычисляются внутри пары каждого радиорежима. Interaction
`(failover−fixed)geometry − (failover−fixed)LOS-only` требует все четыре случая
одного блока. Нельзя получать interaction из разных подмножеств seeds.

Для5 complete pairs/blocks percentile bootstrap перебирает3125 упорядоченных
resamples; для10 применяется50000 resamples с seed20261028. Resampling идёт по
целым независимым разностям, не по пакетам/аппаратам/эпизодам. Интервалы остаются
разведочными и не гарантируют95% покрытия. Auxiliary sensitivity n3 и engineering
boundary n1 показываются описательно и не объединяются с основной серией.

Дополнительный endpoint заранее объявлен: **τ=60 host seconds** после первого
наблюдённого CP↔initial relay LOS→NLOS. `min(T_first_UART,60)` вычисляется лишь
при≥60s корректно наблюдённого followup. Валидное отсутствие update получает
bounded loss60 с цензурированным флагом; это не «восстановился за60s».
Crash/административно короткий followup<60 оставляет исход отсутствующим даже
при сохранённом раннем событии, которое показывается отдельно. Без самого NLOS
endpoint N/A. Пропуски с неприменимым trigger не получают выдуманных значений.

Название — first-post-NLOS UART latency. Геометрия сама не доказывает нарушение
доставки. Если доступны raw, отдельно вычисляется endpoint после фактического
`link_failure_detected(reason=direct_GCS_probe_timeout)` исходного relay: первый
свежий post-detection-created target CID, допущенный всеми пятью под одним
relay/epoch. Network receipt, UART и физическое достижение не объединяются.

Вторичный буквальный `new_goal_after_NLOS`: только independently replayed
physical completion конфигурационной точки с goal_created≥t_NLOS и проверенной
goal/CID/epoch/target lineage. Per-UAV выбирается первая успешно завершённая такая
цель; all-five — максимум пяти времён с явным составом relay/epoch. Разложение
принадлежит одному критическому UAV: создание выбранной успешной цели → создание
её первого успешно допущенного refresh CID → первый UART → completion/dwell.
Порядок времён проверяется до сложения. Первый созданный (возможно потерянный)
CID и первый допущенный CID сохранены отдельно от latest completion-refresh CID.
Ожидание создания относится к **выбранной успешной** цели и не означает отсутствия
более ранних созданий. Отсутствие post-NLOS goal opportunity отмечается счётчиком;
старые цели не подставляются. Полное невосстановление цензурировано, crash/пропуски
и отсутствие trigger имеют отдельные статусы. Это descriptive secondary CSV/JSON;
он не добавляет primary CI и не меняет прежний inspection_resume(old goal allowed).

Выход: JSON происхождения/определений, CSV всех запусков/пар/four-case blocks и
unplanned attempts. CSV предназначены для графиков всех точек и отсутствующих
исходов. Упорядоченный список опорных источников и SHA256 экспортов/контроллеров
сохраняются; исходные raw и freeze не изменяются.
