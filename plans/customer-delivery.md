**BAS: план доведения до поставки заказчику — 11.11.2026**

Статус: план работ и предлагаемая программа приёмки, не отчёт о готовности.
Основание: пересланные итоги созвона 16.06, уточнения модема и ответ пользователя «до 5 дронов».
Документ заказчика о нагрузках прочитан через Google Drive:
[Требования к каналам связи БВС](https://docs.google.com/document/d/1sgiyis_40Qhkn7w2SwA7hpFQFvdSquJzDOfV0xBpY3s/edit).
Исходная проверенная ревизия — `46e1a522c6026f8138ce0354a5082e1b871bd27a`;
текущая реализация ведётся в `delivery/integration-20261107`, результаты привязаны к ревизиям запусков.
07.11 доступ к ПК4 восстановлен; новые результаты ниже отмечены отдельно от измерений 05.11.

**Текущее продолжение поставки — 11.11.2026**

Работа начата с чистого `a6285c8` на ПК4. Исторические результаты `0ec5f67` ниже
не подменяют проверки новых ревизий. Код, исходные данные и все неудачные попытки сохранены.
Единственный действующий план — этот файл. Старый `network/STATUS.md` перенесён
в исключённые артефакты; его содержание также остаётся в истории Git.

**Опубликованное программное состояние**

В приватный [UAVcosim/main](https://github.com/HSE-Intelligent-Unmanned-Systems-Lab/UAVcosim)
без force push отправлены `247f338`, `68b5845`, `7373a38` и
`25d5ae5`, `849734c`, `37313aa`, `cbaaf26` и
`8b910e6087557e1d61746ef4ed8de3816ade1444`.
Добавлен отдельный remote `UAVcosim`; существующих remotes не было.
Это инженерная публикация с известными отказами свежести, а не завершённая приёмка.

Release `runtime-dependencies-20261110` содержит закреплённые 75 wheels:
889845760 байт, SHA-256 `7a5be95a6036296f32193dc55bc99ae320b94ba0b695d68e8ecdd2b26cf34dd5`.
Поставочный `runtime-terrain.json` теперь задаёт исходные 10 Кбит/с на каждый БАС.
Сохранены исходный JSON геометрии, manifests ресурсов и сведения о лицензиях.
Штатный helper получает ресурсы и проверяет SHA. Рельеф воспроизведён побитово.
Выявленная скрытая зависимость NumPy устранена: используется 1.26.4 из закреплённого bundle.
Gitleaks 8.30.1 проверил подготовленные файлы и отправляемую историю: секретов не найдено.

**Новые попытки, без исключения отказов**

| Серия / ревизия | Заранее заданный объём | Фактический результат |
|---|---|---|
| `qualification-short-20261110-a`, 247f338 | 20 повторов | Первый preflight отказал: Spectrum library старее Doppler patch; 19 не запускались. Затронутые исходники и targets реально пересобраны. Timestamps библиотек для обхода guards не менялись. |
| `qualification-short-20261110-b`, 247f338 | 20 повторов | Три completed / succeeded / valid; четвёртый failed / invalid; 16 не запускались. |
| `github-clone-247f338-20261110-01` | Один контрольный E2E | failed / failed / invalid: один `host_sample_expired` cache miss, окно и посадки не завершены. Повтора не было. |
| `rt-stage-diagnostic-20261110-a`, 68b5845, trace=true | До пяти повторов, остановка на первом runtime-отказе | Четыре completed / succeeded / valid; пятый failed / failed / invalid. Все пять сохранены. |
| `gazebo-wait-diagnostic-20261110-a`, 7373a38 | До пяти, остановка на первом отказе | Первая failed / invalid: emitter SDK не завершился через 62,264 мс. Четыре не запускались. BPF не успел подключиться; это отдельный незакрытый источник задержки. |
| `gazebo-wait-diagnostic-20261111-b`, 7373a38 | До пяти, остановка на первом отказе | Первая completed / succeeded / valid; вторая failed / invalid, физическая пауза 99,124 мс; три не запускались. BPF второй попытки подключился поздно из-за повторной компиляции helper. |
| `gazebo-wait-diagnostic-20261111-c`, 25d5ae5 | Ровно пять, остановка на первом отказе | Все пять completed / succeeded / valid. Возраст применённой позы: 97,645 / 93,175 / 90,488 / 89,122 / 90,047 мс. Guards не менялись. |
| `clone849-interframe-20261111-a`, чистый GitHub clone849 | 20 попыток, остановка на первом runtime-invalid | Первая failed / failed / invalid: native applied pose stale/future; 19 не запускались. Новых повторов этой серии нет. |
| `pickup500-joint-20261111-a`, 8b910e6 | Ровно одна интеграционная попытка после production rebuild | exit0; completed / succeeded / valid. Все пять маршрутов, посадок и disarm; radio landing5/5; applied pose max84,178мс; 858725cache hits /0miss. Не квалификация20повторов. |
| `hybrid5-5f62c59-20261111-a`, 5f62c59, UI | Первая из двух заранее заданных разных конфигураций; stop-on-first-invalid | exit1; runtime completed / mission failed / measurement invalid. Preflight120с не получил control HEARTBEAT/GPS. Не взлетел ни один БАС; окно не начато. Payload bidirectional5/5, CIR694290hits/0miss, pose max65,245мс. Вторая canonical конфигурация не запускалась. |


Это повторы исполнения seed 1 / RngRun 1 / Sionna seed 42, а не независимая статистика RT.
Смена RngRun изменяет подстримы ns-3; RT имеет отдельный seed. Явных AssignStreams нет.
Во время части серии B работал отдельный CPU0 benchmark постобработки.
В диагностической серии ошибка scoped-audit запустила `rg` между командами
20:11:13.265–20:12:14.734 UTC. Завершены только его PID. Это зарегистрированные
условия фоновой нагрузки, а не доказанная причина отказов и не строго пустой хост.

В первой успешной B-попытке повторно проверены сценарий, cache, journals и capture SHA:
максимальный возраст 93,814 мс, ноль cache misses, окно 20 с, RTF 0,999945.
Первый новый диагностический опыт: максимум возраста 86,621 мс.
Ни один из этих успехов не закрывает обнаруженные ниже дефекты повторяемости.

**Установленные цепочки и незакрытые причины**

- Посадка 09/10: финальный HEARTBEAT UAV5 прошёл UART и оба радиоперехода и был принят PHY НПУ
  на 11,081555 / 0,564647 мс после `experiment_done`. В 11 приём был на 1,960639 мс раньше.
  Это окончание наблюдения; RF loss не доказан. Новый итог сохраняет точную границу,
  `radio_confirmation_status` и времена. Исходный boolean, timeout и критерий не менялись.
- B4: последняя применённая поза достигла 102,675803 мс. Tracker оставался свежим (12,685 мс),
  native lag был около 0,05–0,07 мс; desired job готов за 20,368 мс, обязательный emitter job
  не готов через 64,734 мс. Исходная причина внутри незавершённого job не доказана.
- Точный frozen-geometry replay на 68b5845 выполнил 200 пар с тремя workers и 200 с двумя.
  Все завершены; первые задания новых процессов пересекли 100 мс, последующие — нет.
  Терминальная задержка B4 не воспроизведена; оснований выбирать два workers нет.
- Диагностический case 5: независимые Gazebo world stats подтвердили паузу продвижения физики
  на 91,303279 мс около sim time 61,932. Clock / odometry перестали продвигаться вместе.
  Tracker корректно увеличивал возраст; новая одометрия пришла после cache expiry.
  Emitter тогда не требовался, предыдущий desired SDK job занял 17,549 мс.
  Причина внутри Gazebo / SITL ещё отделяется от scheduler и вычисления физики.
- В фактическом ArduPilotPlugin `cc0290d964dfa373531963a8fc39093a0836af0a` lockstep
  может повторять 10-мс ожидание servo UDP; при текущем count=5 SendState повторяется после
  шестого timeout. Наличие механизма ещё не доказывает его участие в конкретной паузе.
  Подготовлена штатная BPF-диагностика; timeout, lockstep и предел 100 мс не изменены.

В серии C BPF подключился до preflight каждого опыта. Ни в одной из пяти попыток
не зарегистрированы вернувшиеся ожидания ≥10 мс или незавершённые ожидания в END maps.
Потеря ring-buffer не сообщалась; CPU stack profiles частичны из-за ограничений maps.
Это отрицательный диагностический результат: прежние physics / SDK отказы не исправлены.
Выявлен пробел наблюдения: scheduler учитывался только внутри PreUpdate.
Отдельное расширение helper измеряет межкадровые интервалы и scheduler для собственных TID
также вне callback. Оно ещё не участвовало в полётах; следующая серия заранее фиксируется.

**Проверенные изменения новой программной версии**

Полный Python suite на 68b5845: 541 tests PASS, один существующий платформенный skip. После strict scene validation —550tests, после профилей и class-map attachment — 581 tests PASS с тем же skip.
После пакетного сравнения и переносимого анализа: 606 tests PASS, один тот же skip.
Оба native targets реально пересобраны. Дополнительно: 24 clock tests на скоростях 0/3/30 м/с,
yaw 0/±90/180° и задержках до/за 100 мс; native history contract — 354 проверки движения.
Свежий Doppler replay: 203 CIR, 3248 случаев, 626864 bin-наблюдения, подписанный Doppler
и времена 0/1/25/99 мс; численное отличие мощности от прежнего порядка около 2,33e-15.

`runtime.rt_stage_trace` — bool, default false, только worker_pool. Значение фактически
прошло в worker readiness. Шесть этапов содержат host/thread CPU clocks и fingerprints,
последние записи незавершённого job ограничены по памяти и сохраняются при закрытии.
Native отдельно публикует bounded histograms возраста когорт и lag. Наблюдаемые spans между
превышениями не выдаются за непрерывную длительность, последний эпизод цензурирован.

Source-only export переведён на streaming и стандартный SQLite. На capture 09/11 summary
побитово совпал с прежним, peak RSS около 32 МиБ вместо 12,1 ГиБ на 09.
Raw capture и его SHA сохранены. Есть явный дисковый бюджет spill и очистка собственных
временных файлов при исключении. Полный графический экспорт длинных трасс не квалифицирован.

Объединённый RT через common antenna basis не интегрируется: LoS совпал, terrain дал
различия до 1,92667 дБ на действующем канале. Небольшое ускорение не закрывает корректность.
Все четыре попытки технической пробы, включая ошибки окружения, сохранены.

Аудит сцены выявил молчаливое принятие NED, ground offset, неизвестных physics/failsafe и version.
В текущем рабочем дереве общая проверка UrbanScenario отклоняет неподдержанные поля,
дубли JSON и неверные типы; легитимные metadata и legacy defaults сохранены.
Общий suite после этой проверки: 550 tests PASS, один существующий платформенный skip. Это проверка достоверности настроек; ниже зафиксировано последующее узкое расширение поддержанных physics/failsafe.

Сцена теперь поддерживает только новые-run параметры physics.max_step_size_s
(0,25 / 0,5 / 1 мс) и GCS failsafe action disabled / rtl / land, timeout 2–120 с.
Использованы существующие SDF и последний SITL --defaults, без новой подсистемы.
Defaults сохранены; FS_OPTIONS не переопределяется, актуальные MAV_GCS_SYSID / HI
не подменены устаревшим SYSID_MYGCS. runtime-settings.json различает configured input
и ещё не выполненный runtime readback и входит в effective capture.

После этого изменения: 612 tests PASS, один прежний skip. Stock Gazebo подтвердил
три точных шага через δsim/δiterations. Pinned SITL PARAM_VALUE подтвердили action
0/1/5, timeout 2/5/120 и MAV identity. Отдельный тест использовал настоящие generator-produced
.parm и два TCP MAVLink канала с system255: при потере control 10,1 с AP-time с живым
payload сохранялись GUIDED / armed; после потери обоих наблюдались LAND / RTL через
3100 / 3000 мс от последнего heartbeat при timeout 3 с. Использован forcearm в
изолированном stock SITL 10×; отметки реакции опираются на GLOBAL_POSITION_INT с периодом 100 мс.
Полные посадка / disarm и совместная Gazebo / ns-3 реакция здесь не проверялись.
Доказательства: runs/control/publication-physics-failsafe-20261111/analysis.json
и отдельный worktree runs/control/physics-failsafe-probe-20261111/gcs-loss-01/results.json.

**Установка, оборудование и критерии**

Отдельный GitHub clone `247f338` получен в новый каталог. Старые SDK, среды и build не копировались.
Ресурсы получены из Release, SDK заново скачан из официального GitLab, 15 patches проверены.
ROS и оба native targets собраны заново за 260,53 с; check-only: ready=true, gaps=[].
Единственный E2E этой копии отказал, как указано в таблице. Новые ревизии требуют своей проверки.

Новый независимый GitHub clone `849734c` создан в отдельном каталоге
UAVcosim-github-verify-849734c-20261111. Из Release заново получены wheels,
официальный SDK клонирован и пропатчен; private environments, ROS и native targets
собраны заново за 267,06 с. Ни SDK, ни environments, ни build из прежнего каталога
не копировались. Проверки bootstrap (ready=true, gaps=[]), assets и inspect terrain прошли.
Закреплённый Docker image уже был на хосте и проверен по digest.
Первая попытка фиксированной серии clone849-interframe-20261111-a отказала:
failed / failed / invalid; 19 не запускались. Это новая доказанная неустойчивость,
не успешный E2E и не завершённая проверка воспроизведения.
SDK job занял 20,395 мс, новый кадр был готов. Receiver enqueue→native poll занял
1,026 мс, decode — 88,826 мкс; held-поза достигла 100,194 мс до замены новым кадром.
Межкадровые паузы Gazebo <2,048 мс; SDK-tail и physics-freeze здесь не подтверждены.
Простое снятие pre-commit guard отклонено: прежний verifier требует maximum_applied_pose_age≤100 мс.
Внесён более частый pickup готовых результатов: 500 вместо 1000 мкс. Те же guards,
строгий verifier и max/hist сохранены; прежний 1-мс response forecast margin выделен
отдельно, не уменьшен. Native RED старой cadence провалил новую independent-deadline
проверку, GREEN прошёл: bundle887/cache340/live354/prefetch45. Дополнительно 187 Python
native-interface tests PASS. Все девять компонентных CPU samples сохранены; медианная
добавка 1,524 мс CPU на model-second (~0,1524% одного CPU этого компонента), без claim
о полной real-time производительности. Production main/worker действительно пересобраны,
байты/mtime Spectrum, Wifi и TAP библиотек не менялись. Единственный новый совместный
запуск на чистом8b910e6 прошёл, как записано в таблице. Native poll_period_ns=500000,
response_forecast_margin_ns=1000000; maxlag31,845мс. Queue-enqueue→post-poll proxy
p99=0,962мс / max1,438мс включает decode и scheduler: это не C2latency и не wall-clock
гарантия500мкс. Были1129queue drops/6863retries при помехе; это модельные исходы,
а не CIRcache misses. BPFv6 подключён до active phase, ENOENT flood0, normalEND;
CPU profile частичен (stack collisions/map-full), timinghelper ошибок не сообщил.
Во время preflight был250-мс readonly scan39КБ nice19CPU0; хост не объявляется пустым.
Все собственные runtime PIDs/containers завершены, GPU освобождён.
Квитанция: runs/control/pickup500-integration-20261111-a/verification.json,
SHA2567b98adda8fc41c5ece463d94c7f3d537dbd4f2a604a7ccb679390a55b8e43a0f.

Наблюдатель C1 дал 170,7 МБ ожидаемых ENOENT от delete отсутствующих BPF map keys.
Это зарегистрированное вмешательство диагностического инструмента, не пустой хост.
Timing histograms сохранились, CPU profile частичен. Новая версия v6 сохраняет -k,
guards для delete и исходные ownership checks. Настоящий attach к собственному
синтетическому callback binary: stderr=0, 512 событий, 209 suppressed, END/pending
и retirement проверены. Это kernel-harness test, не новый полёт.

[Компактные доказательства на GitHub](https://github.com/HSE-Intelligent-Unmanned-Systems-Lab/UAVcosim/releases/tag/engineering-evidence-20261111-849734c):
166 исходных сводок и команд, включая отказы, 878628 байт,
SHA-256 7b8e078ee65794e7504e044f4168c0bc262f3833dda02c6a3a9fd032afa9f78c.
Архив скачан обратно, SHA подтверждён; Gitleaks не нашёл секретов.
Эта публикация предшествует новой сборке849 и physics/failsafe tests; новые результаты
в неё задним числом не добавляются.

Владелец подтвердил: оборудование пока не подключено. Нужны реальные два UART и Ethernet,
модем/контроллер и их фактические версии. Инвентаризация не нашла ttyUSB/ttyACM.
16 software HitL tests и подготовка профилей не заменяют аппаратные испытания.
Подготовлены интерфейсные references китайских плат/модемов; native Wi-Fi
остаётся инженерной моделью, эквивалентность произвольному устройству не заявляется.

Решение владельца: выбрать и реализовать рабочие SLA / hybrid profiles по опубликованным
практикам, без ожидания отдельного согласования. Документ заказчика задаёт шесть скоростей,
но не численные задержки и потери. Проектные пороги реализованы в service_profiles_v1.json:250/250/500/1000/1000/5000мс и99,9/99,9/99/99/99,9/99% для шести классов; минимум и rolling window60с. Наблюдаемая оценка отделена от10независимых ns-3(seed,RngRun) репликаций и10000записей. Профиль содержит источники и границу UART OS-write; договорная сертификация не заявляется. Карты дополнены строгой привязкой реальных packet windows к клеткам/условиям RF; пока без60-секундных bindings клетки не получают pass.

Установка host теперь воспроизводится через отдельный venv и requirements-host.txt:
NumPy 1.26.4 / PyYAML 6.0.2 с официальными wheel SHA для Linux x86-64 CPython 3.10–3.12.
На ПК4 создана новая .venv-host; hash-checked install и inspect terrain прошли.
Проверены CPython 3.10 / 3.12 environments, для 3.11 проверены wheels, не запуск интерпретатора.

`bas.py analyze` запускает существующий анализатор в записанном Docker-образе:
код и raw read-only, новый каталог анализа writable, без сети / GPU / host pymavlink.
Реальный новый успешный C1 проанализирован с сохранением completed / valid.
`--service-batch` пересчитал все 20 плановых B-попыток: три insufficient_evidence,
одна invalid, 16 not_started. Оценка not_qualified; отказы и неподнятые попытки не исчезли.
Повторы seed/RngRun не добавляют независимости. Сводка привязывает профиль, исходные
packet lineage и условия каждого опыта; новый анализ не становится новым полётом.

Добавлен документированный китайский serial reference Holybro SiK V3 100mW.
Это один UART, point-to-point TDM/FHSS, RF default 64 Кбит/с; он не закрывает автоматически
два UART, многоточечность или эквивалентность Wi-Fi. Модель устройства владельца
по-прежнему неизвестна, физические устройства не подключены.

Переносимый maps entry в рабочем дереве использует тот же pinned container:
RF через общий lease, CPU overlay без GPU, единственный writable runs/maps output.
Реальные CUDA / LLVM RF point-grid и PNG прошли за 8,09 / 6,35 с; все четыре
клетки рассчитаны, point_cells выбирает дополнительные контрольные сравнения, не область
основного расчёта. Различие RSS backend на этих четырёх клетках — до 0,42 дБ при 20k samples
и одинаковом seed; общая сходимость из четырёх точек не заявляется. CPU overlay сохранил 54 not_measured.
Добавлен поставочный radio_map_terrain.json: baseline CP→iso/V, полный квадрат 10×10 км,
100 ячеек, ENU Z=230 м, источники выключены. Все параметры CP / радио взяты из фактического
каноничного frozen input. Новая подготовка из tracked terrain config и настоящий CUDA
расчёт этого preset (менялись только три input paths) прошли за 8,51 с; PNG визуально проверен.
Белый J/S означает J=0, серый — sampled-zero / отсутствие данных, не доказанный outage.
Исправлены найденные при review внешние sidecar symlinks и перенаправление runs/maps
в source tree. После границ файлов и SIGTERM cleanup — 624 Python tests PASS,
один прежний skip. Реальный cancellation harness01 не достиг условия отправки сигнала
за своё короткое окно; вычисление завершилось успешно, это не проверка отмены.
Квитанция сохранена. Исправленный bounded harness02 послал настоящий SIGTERM работающему
map CLI: exit143 / attempt cancelled, только его Docker container удалён; raw входы сохранены.
Следующий LLVM compute прошёл с новым output, lease / ресурсы освобождены.

Hybrid R3 подготовлен изолированно: qualified-hybrid-r3.patch,
SHA 50d80bc45d5014c28ed6a0576c43d495eadcf877debbcca48c3b7c53e355c6b2.
Закрыты review-дефекты public shell gates и body orientation; нулевой RT-reference
не заменяется положительной Friis-мощностью. 95 Python / native boundary checks прошли.
UI Save + hybridR3 интегрированы в рабочее дерево после8b910e6. Full Python:
635 проверок:634 PASS и один прежний skip (python-suite-06-command.json). Skip —
ветка сообщения об ошибке для non-Linux; на ПК4 Linux она не выполнялась. Он не
считается успешной проверкой. Ранее указанные размеры suite — числа unittest Ran,
которые также включали этот skip. R4 очищает hybrid
Python callback под существующим GIL: реальный native refcount/deleter regression
проверяет повторное clear и уничтожение после interpreter finalize. Production build /
joint этой интеграции ещё не выполнены. Domain только static iso/V SISO empty scene
2,412 ГГц; для прочих условий видимый fullRT fallback. Parent extra CUDA context при
explicit hybrid — известный ресурсный риск, ускорение не заявляется.

UI Save замораживает копии входов без runtime_started; duplicate ID не перезаписывается,
ошибки валидации не создают draft. Настоящий браузер подтвердил Save→reload→validate,
изменения duration/speed/physics/failsafe и видимые неизвестные параметры. Закрытый
неуспешный clone849 показан с раздельными Error/Error/Invalid и причиной. Runtime не
запускался этим браузерным тестом. Доказательства: publication-ui-e2e-20261111/analysis-save.json.

Fresh-process audit выявил отдельный host gap: config/UI импорт NumPy создавал32BLAS
threads на всех ядрах, хотя runtime child masks уже делились. Отдельный per-process
OPENBLAS1 probe устранил pool, без OS/global environment изменений. Прошлые runtime
отказы этому не приписываются. Минимальный entrypoint fix интегрирован: existing
stdlib runtime-monitor helper вызывается из bas/UI/batch main до NumPy. Host affinity
не меняется, повторного деления нет. 6 tests плюс actual fresh CLI/batch/оба HTTP UI:
NumPy1.26.4/1native thread, inherited stack mask сохранена. Production runner partition
не менялся. После mode-validation вместе прошли31 targeted tests.

Найден конкретный preflight gap: любые непустые data/control mode имена принимались
до ns-3 abort. Actual pinned ConstantRateWifiManager Set/Get и PHY rate API подтвердили
HtMcs0..7/NSS1 и восемь ERP modes; HtMcs8 требуетNSS2. Добавлена минимальная whitelist
в существующий radio validator, defaults неизменны. Regression RED:10 subcases
показывали поздний/неименованный отказ; GREEN:31 radio+host tests PASS.
Review нашёл обход direct town01 shell: inner RADIO_VALUES читал YAML напрямую.
Один общий mode-validator теперь используется обоими входами; тест выполняет
настоящий inner heredoc, проверяет36baseline values и ранний отказ неверных modes. Это API/конфиг
совместимость, не квалификация пакетной ёмкости65Мбит/с. Proof publication-radio-modes-20261111.

Практическое расширение hybrid на физическую землю проверяется отдельно: область
выбрана до RT по официальному lossy-slab API medium_dry_ground, без собственной
формулы распространения и без замены на неподходящий TwoRayGround. Frozen inputs /
seeds / budgets: publication-hybrid-ground-20261111. Full-01 завершён:17010/17010
RT-вызовов,546/546 ожидаемых групп по1000unique links,447,99с. Все положительные
static/dynamic numerical и reference группы прошли: max|bias|0,06616дБ, p95abs0,43893дБ,
p99abs0,46953дБ; top-budget convergence p99≤2,05e-6дБ. Negative angle/wall/metal/crosspol
сохранили большие ошибки и требуют RT. >100мс не даёт большой численной RSSошибки
автоматически: это invalid по временному контракту, native rejection проверяется отдельно.
Независимый review отметил ограничение постоянной attitude в пробе при существующем
cache допуске5°. Поэтому выбран минимальный onlineguard: actualRTPSD один раз плюс
штатная Friis один раз, единственный выходPSD; все positiveinput bins finiteRT>0 и
ошибка≤1дБ, иначе видимый RT. NonfinitePSD failclosed. Это новый явный hybridконтракт,
без собственного CIR/Doppler/cache и без обещания ускорения; реализация в изоляции.

R5 и два review fixes R5b интегрированы: numerical ground evidence дополнена
actual RT PSD≤1дБ onlineguard. Registered sources используют собственный RT,
невозможные fallback counts отклоняются. Native compile/link/cache contracts/main
syntax прошли на точных afterSHA;73 targeted Python в изоляции прошли. Независимый
review R5b PASS: ac2421678bd665006376612342b6b70a4a7b8196f68bead6f3e50b8750b5ece7.
В core общий suite после R5:646 всего,645 PASS/1non-Linux skip; после R5b —87
затронутых Python PASS и5 UI discovery PASS. Первая попытка адресного набора
сохранила ImportError test fixture при package invocation; исправлен только import
существующей fixture, оба способа запуска теперь проходят. Product code этой
ошибкой не затрагивался.

Поставлен отдельный runtime-hybrid.json / hybrid_flat_ground.json:40с,5БАС,
5×10Кбит/с, общий RT fullmesh100мс, без помехи. Existing generator создал XML
905807072f010dbca786a3842919de50d1d3988656bb08400cb428c1166acdde, actual pinned
LLVM loader подтвердил qualified_flat_ground_scene=true. Routes разнесены минимум
62,032м, ENUZ20,2м, углы74,491–75,367°. Input/domain review PASS, не полёт.
Дополнительная заранее замороженная emptyвыборка содержит ровно1000links внутри
2–10000м:567RTcalls exit0, p99abs0,114725дБ, convergence0. Прежние962inside-domain
более широкой выборки не выдаются за1000.

Read-only аудит coldworkers исключил гипотезу first-ever idleworker activation:
во всех4отказах и source8 worker2 idle/completed0; worker0/1 прогреты при native0.
B4 emitter имел168completed,737 —165 и предыдущийSDK16,754мс. Дополнительный
arbitraryidleprewarm не обоснован; SDK-tail/JIT/GPUwake причина не объявлена закрытой.
Proof publication-final-build-20261111/worker-first-call-audit.json.

Следующая fixed20 diagnostic series —qualification-short-20261111-d, setupv2:
immutable runtime-terrain copy отличается только rt_stage_trace=true;1/1/42
повторы исполнения. V1 сохраняется unused. Оценка по source8 —около87мин/40–65ГБ;
per-run tmpfs≤4ГиБ последовательно. V6 drycheck переиспользуется, actual BPF
LLVMcompile остаётся на каждый PIDattach; это явно инструментированная серия.
До запуска source freeze и настоящий production rebuild; пока не запускалась.

Source5f62c59 зафиксирован чистым;23подготовленных файла и вся отправляемая
история133коммита прошли Gitleaks8.30.1:0findings. Source5 ещё не отправлен как
проверенный совместный вариант. Первую production build попытку остановила ошибка
вспомогательного wrapper: он требовал отсутствующий в старом SDK Gitindex. Это
произошло до копирования/сборки. V2 сохраняет отсутствие index; build02 действительно
пересобрал main/worker за36,803с, source copy и исходники совпали, Spectrum/Wifi/TAP
байты/mtime и indexabsence сохранены. Фальшивый bootstrap receipt не создавался.

Первый E2E5f выполнен настоящим browser Save→Start→Observe→export на отдельном UI
port40911; исходный UI8765 не трогался. Source5clean, savedscene/radio SHA совпали,
readiness5SITL/5models/5odometry healthy. Первый nominalhybrid не начался из-за
связности на земле:337 reports UAV1 всегда missing3, ни один из1600GCSreports
не имел всех4peer echoes. available()/initialmaster policy не выдала ниодного grant;
localUART наблюдала126–127HEARTBEAT, ноtelemetry return безgrant не разрешён. Payload
получил120HEARTBEAT/598–599ATTITUDE на каждый БАС; это не измерительное окно.
Watchdogtracebacks появились43,9–53,1мс ПОСЛЕ missiontimeout; это cleanupследствие.
Ни UI configcorruption, ни пропущенный hybridgate, ни RTcachefault не найдены.

Причина отсутствующих peer links подтверждена по всему nativeCSV863МБ/SHA,
прочитанному один раз existingstreamparser (48,3с, peakRSS42,6МиБ). Для восьми
направлений149,69–164м:0successful intended control MPDU из32977PHY attempts;
31503(95,53%) drops PREAMBLE_DETECT_FAILURE. Все их signal arrivals ниже actual
readbackMinimumRssi−82дБм, примерно−83,65…−85,51. Это другой параметр, чем
RxSensitivity−101дБм; параметры не менялись. FullRT54calls/270rows на actualclosed
poses по3seeds/3budgets дал160м→−84,9403дБм,114,56м→−79,2345,62,77м→−68,8836;
coherentmultipath cancellation подтверждена. SNRfar≈9дБ выше4дБ, ноRSSниже−82.
Это результат инженерной модели, не калибровка физического модема. Неверный
номинальный стартовый input5f и весь отказ сохранены, не подменены новым успехом.

Для следующей версии принят новый вход: пять radial3м маршрутов на дуге82м
с углами−30/−15/0/15/30°, cruise20,2м; minimum separation21,406м, maximum async
peer span85м. ActualgeneratorXML9058 неизменён; cruiseуглы74,851–75,363° в прежнем
qualifieddomain. Контрфактический RT54calls/270rows дал worstpeer−73,4887дБм,
запас8,5113дБ до ТОГОЖЕ−82. Это numericalprediction, не полёт. Policy/PHY/ARQ,
100мс,120с timeout и определение success не менялись.

Закрыт выявленный пробел объяснения: read-only selection_diagnostics snapshot
сохраняется при прежнем timeout; UI показывает mission error/отсутствующих соседей
и отдельно native diagnostic. Independent AST review подтверждает неизменность
available/update_role/dispatch/telemetryeligibility и wait predicate. Source patch
6a4a162b5bc32e1d57d830dee60d481cede80cd30a1f2315e036f6e4009a740a;39 targeted PASS
и realChromium closedfailureCOPY rendering прошли, исходные результаты не правились.
Общий suite после этого изменения:652всего,651PASS/1non-Linux skip. Новая версия с этим объяснением и новым input требует нового E2E; пока не запускалась.

Ключевые proof: publication-final-joint-5f62c59-20261111-a/ui-hybrid-first/
native-packet-rf-01/{correlation,threshold-identity,selection-diagnostics-review}.json;
publication-hybrid-ground-startup-rf-20261111/evaluation.json;
publication-hybrid-ground-startup-rf-candidate-20261111/evaluation.json.

**Следующая ступень и доказательства**

Инженерная лестница: 20 коротких попыток; затем реальные полётные маршруты 600/900 с,
3600 с и 28800 с после успешных предыдущих ступеней. Длинные ступени не запускались:
короткая пока не пройдена. Подготовлены прямоугольные маршруты 240×120 м, 3 м/с,
1440/2160/8640/69840 м на БАС, высоты по общему рельефу, disk capture.
Они покрывают юго-западную часть рельефа; городское затенение проверяется отдельно.

Следующий непосредственный опыт — 20 заранее заданных коротких попыток с расширенной
scoped BPF-диагностикой после завершения hybrid probe и фиксации чистой ревизии.
Серия останавливается на первом runtime-invalid; это не повтор до удобного числа успехов.
Hybrid и physics/failsafe теперь реализованы и проверены компонентно; совместные проверки привязываются к следующей чистой ревизии.

Артефакты относительно рабочего корня:

- `runs/control/publication-integration-20261110/`: команды, exit codes, сборки, тесты, push.
- `runs/control/publication-integration-20261111/`: 606 tests, host install, portable single/batch analysis.
- `runs/control/gazebo-wait-trace-20261111-{b,c}/analysis.json`: все BPF попытки и ограничения наблюдения.
- `runs/control/publication-clean-clone-20261110/`: независимые установка и неуспешный E2E.
- `runs/control/publication-landing-20261110/analysis.json`: точная landing chain и пять прежних отказов.
- `runs/control/publication-contracts-20261110/`: regression, hardware inventory, RNG, RT replay, case 5.
- `runs/control/publication-20261110/`: scanner, dependency manifests и ресурсные пробы.
- `runs/control/publication-config-audit-20261110/`: контрпримеры, schema patch и условия нагрузки.
- `runs/control/common-pattern-probe-20261110/`: отклонённая RT-проба.
- `runs/control/qualification-duration-inputs-20261110/inputs.json`: входы длинных ступеней.

**Проверка совместного сценария — 10.11.2026**

На ПК4 три последовательных запуска на одной чистой ревизии
`0ec5f67f7342f0461f1cb6bc4c2f3f6217fe2ed7` завершили совместный сценарий:
**пять БАС + рельеф 10×10 км + Sionna RT full-mesh + направленная помеха + оба UART + 10 Кбит/с payload на БАС**.
В каждом запуске подтверждены взлёты, все маршруты, LAND и auto-disarm всех пяти БАС.
Фактическое завершение посадки наблюдается в бортовом UART; доступность финального радиоподтверждения указана отдельно.
Итоговая проверка исходных журналов: `runtime_status=completed`, `mission_outcome=succeeded`,
`measurement_validity=valid`, `runtime_verification_errors=[]`.

Использован исходный профиль
`runs/control/delivery-terrain-n5-source-load10-fastperf-20261108-02/config.json`,
seed 1 / run 1. SHA-256 исходных scene/radio/sources совпадают с профилем 08.11.
Лимиты возраста состояния и радиокэша остались **100 мс**, runtime lag guard — 250 мс.
Число БАС, геометрия, RF-параметры, RT samples/path limits, нагрузка и пятисекундное окно помехи не уменьшались.

| Запуск | Окно измерения, source / wall, с | Максимальный возраст применённой позы, мс | Самый долгий горячий RT job в первую секунду, мс | Радиоподтверждения посадки | Итог |
|---|---:|---:|---:|---:|---|
| `fix-pose-clock-full-n5-20261110-09` | 20.000 / 20.001 | 87.441 | 22.136 | 4/5 | completed / succeeded / valid |
| `fix-pose-clock-full-n5-20261110-10` | 20.010 / 20.005 | 88.836 | 19.624 | 4/5 | completed / succeeded / valid |
| `fix-pose-clock-full-n5-20261110-11` | 20.000 / 20.003 | 92.372 | 21.147 | 5/5 | completed / succeeded / valid |

В каждом запуске — ноль пропусков и отказов desired/source cache; все наблюдаемые возрасты ниже 100 мс.
Помеха подтверждена реальными native SignalArrival у НПУ и всех пяти БАС; SHA-256 полного CSV capture сверены.
Генератор отправил по 391 payload-сообщению на БАС без пропущенных слотов.
Это подтверждение заданной нагрузки и работы симулятора; доставка всех сообщений под помехой и SLA класса этим не заявляются.
В `…-09/10` финальное радиоподтверждение UAV5 к завершению сценария не наблюдалось, хотя бортовой UART
подтвердил посадку и auto-disarm. Исходный критерий lifecycle использует эту отдельную provenance и не требует
радиоподтверждения каждого финального состояния; критерий не менялся. Это остаётся ограничением завершения миссии
для НПУ и не скрывается за успешным runtime. Флаги всех БАС сохранены в итоговом ledger.

Причины и исправления, включённые в эту ревизию:

- `adcbef1`: исправлен перевод времени измеренной одометрии в host monotonic.
  Задержка доставки clock/odometry больше не омолаживает измерение; старые, будущие и регрессирующие timestamps отклоняются.
  Исходная ошибка воспроизведена шестью регрессионными тестами до изменения.
- `9f4a1ad`: Doppler-множитель вычисляется один раз на путь/порт, затем используется по частотным bins.
  Убрана повторная работа и копирование delay-phase матрицы в GenSpectrum; порядок суммирования путей сохранён.
  SDK patch включён в bootstrap, проверку patched tree и запуск.
- `367a4f6`: в live JSON публикуется только история, ещё способная пройти существующий 100 мс guard.
  Архив и внутренняя история сохранены; lockstep не сокращён.
  `6581e1e`: Gazebo публикует настоящие измерения одометрии с частотой 100 Гц вместо штатных 50 Гц,
  согласованно с tracker. Интерполяция, экстраполяция и подстановка свежего времени не применяются.
- `b3d7fa1`: добавлены наблюдения CPU и GC каждого RT job.
  В диагностическом `…-08` startup spike 45,72 мс содержал 37,81 мс CPU и около 7,91 мс ожидания CPU;
  GC внутри него отсутствовал. Изолированный replay 1020 jobs всплеск не воспроизвёл.
- `0ec5f67`: устранена конкуренция за radio CPU при старте управляющего сценария.
  Импорт NumPy создавал 32 OpenBLAS-потока на всех ядрах; отдельная проба дала 796 мс wall / 1,93 с CPU.
  При одном потоке — 29,54 мс wall / CPU. Теперь supervised scenario/control clients используют stack affinity
  и `OPENBLAS_NUM_THREADS=1`; в live `…-09` наблюдались один поток и CPU `0-7,16-23`,
  отдельно от radio CPU `8-15,24-31`. Политика GC не изменена.

Проверки: 23 tracker, 25 relay, 44 radio-contract, 54 bootstrap, 35 worker и 5 topology tests прошли;
native live-state contract — 354 проверки, включая равенство выбора позы до/после сокращения истории.
Native spectrum phase/Doppler и receiver-batch suites прошли. Численная проверка на 203 сохранённых CIR
(3248 случаев, 626864 bin-наблюдения, также MIMO/нули/deep null) подтвердила новый SDK;
максимальное относительное отличие интегральной мощности от прежнего порядка операций — 2,34×10⁻¹⁵.
Первоначальный probe с устаревшим ожиданием bit-exact старого порядка сохранён как неудачный;
исправленный oracle отдельно проверяет новый SDK и численное отличие от старого.

История попыток сохранена: `…-01/02/04/05/07` закончились отказом freshness/coupling;
`…-03/06/08` прошли на промежуточных ревизиях, но не доказывали повторяемость.
Приёмочная серия после последнего исправления — только `…-09/10/11`.
Полный ledger всех 11 попыток и проверки каждого условия:
`runs/control/fix-pose-clock-20261110/final-series-verification.json`.
Диагностика старта: `startup-contention-evidence.json`, `numpy-startup-probe.json`, `scenario-affinity-09.json` там же.
Численные проверки: `spectrum-probe/attempt02/result.json`; native contract: `bounded-history-native-contract.json`.
Исходные результаты каждого запуска находятся в `runs/control/<ID>/run.json` и `runs/native-radio-realtime/<ID>/`.

Это три полных полётных цикла с коротким 20-секундным измерительным окном на ПК4.
Многочасовая устойчивость, дополнительные seed/профили, шесть классов нагрузки и физическая аппаратная приёмка
остаются отдельными пунктами программы. Сведения 08.11 ниже сохранены как история и не заменяют результаты 10.11.

**История выполнения — 08.11.2026**

Базовый запуск `delivery-baseline-20261107-01` на неизменённом `46e1a522`:
пять реальных взлётов, окно 60,010 с, RTF 1,000133; `failed` при посадке.
Локальный UART UAV5 показывает LAND, затем HOLD/BRAKE после истечения lease.
У UAV3/4 доступность радио-подтверждения отличается от локального режима LAND.
Артефакты на ПК4: `runs/native-radio-realtime/delivery-baseline-20261107-01/`.

| Требование | Подтверждённое состояние и следующий шаг |
|---|---|
| 1–5 UAV | Проверено на `4ef00a8`: полные короткие циклы N=1..5 с обоими paced UART и 10 Кбит/с payload на UAV. Найденная потеря retry исправлена в SDK14. Новый публичный N=5 на `e703c86`: все маршруты, посадки, разоружения; 391/391 timely на каждый UAV, payload p95 20,71–23,37 мс, p99 28,70–34,59 мс. Runtime и lineage пригодны. Proof: `control/delivery-fixed-path-storage-runtime-20261108/public-n5-link-analysis-02/`. Статистические серии, RT и совместная приёмка открыты. |
| Жизненный цикл | Проверено в указанных коротких циклах: свежий фактический LAND не заменяется BRAKE при expiry lease; посадка имеет отдельную наблюдаемую provenance. Пересылаемые ArduPilot MAVLink с чужим SYSID отбрасываются с сохранением очереди provenance, не останавливая миссию. Длительный исходный `delivery-five-20261107-01` оборван watchdog из-за 34,8 с отставания; причина не установлена, результат сохранён. |
| Результаты | Проверено на реальных запусках: runtime_status, mission_outcome, measurement_validity; native stop reason, capture publication и обязательные артефакты участвуют в итоговом критерии. Незавершённые маршруты не становятся успешными по окончании окна. |
| Serial/Ethernet | Частично: `delivery-native-pty-n1-20261107-01` проверил два Serial PTY через действующий native PHY/MAC; `delivery-native-ethernet-n1-20261107-01` — UDP control + TCP payload через штатный socat к реальным UART SITL. В обоих 24/24 команды приняты, payload AUTOPILOT_VERSION получен. Ethernet-прогон также подтвердил no-bypass: PHY sleep 10,52 с, UART источника продолжает выдачу, до НПУ не дошло ни одного кадра обоих потоков. Артефакты: `control/delivery-native-{pty,ethernet}-20261107/`, `native-radio-realtime/<ID>/metrics/`. Физическое оборудование отсутствует. |
| Аппаратная приёмка | Заблокирована внешней зависимостью: оборудования нет. Подготовлены проверяемые профили Pixhawk 6C/Cube Orange+, Microhard P2400 Serial и pMDDL2450 Ethernet, CLI `test_radio_hitl.py --prepare`. Популярность в РФ и эквивалентность модему заказчика не подтверждены. Два физических UART, Ethernet с реальным устройством и failsafe остаются открытыми. |
| Два UART через общий эфир | Проверено в коротких циклах N=1..5: control и payload одновременно через native Wi-Fi Spectrum PHY/MAC. Устранена потеря ToS на UAV TAP ingress: штатный device selector теперь даёт control TID5/AC_VI, payload TID1/AC_BK. Настоящий TAP + YansWifi regression воспроизвёл defect до patch и прошёл после при backlog обоих направлений; p95 control queue residence UAV 402,78 → 1,26 мс (`control/delivery-qos-contract-20261107/verification.json`). Это очередь EDCA, не подтверждение полного UART/RF SLA и не strict priority. |
| Большая карта, Sionna/hybrid | Частично: explicit RT прошёл полный короткий N=1 цикл `delivery-ui-rt-n1-20261107-01` на `1796c4d`; native stats подтверждают Sionna drives RX PSD, один общий Spectrum-канал и ноль scalar base models. Общий OBJ рельеф 10×10 км / 200 м и здания до 15 этажей проверены Gazebo collision + Sionna 1.2.0 в `delivery-terrain-probe-20261107/runtime-result.json` на `f33b74b`. Исправлена причина DART crash: отсутствующие OBJ normals. Полёт N=1 над рельефом с RT/двумя потоками прошёл в `delivery-terrain-n1-20261107-02` (1 Вт по вводным заказчика). Первая попытка с прежним 10 мВт профилем сохранилась как mission incomplete / measurement valid. Синхронный RT N=5 остаётся неработоспособным в заданном realtime-бюджете; попытки и причины ниже. Hybrid запрещён в CLI до квалификации правил. |
| Помехи и карты RSS/SINR/J/S | Частично: GPU `control/delivery-map-probe-20261107/verification.json` подтвердил RF-срезы и разворот диаграммы. Полный `delivery-source-rt-n1-20261107-06` на `2065c4e`: N=1, рельеф, оба потока, 1 Вт tr38901; completed / succeeded / valid, runtime verification errors отсутствуют. Экспорт после закрытия capture проверен: input/actual/summary сформированы, SHA-256 capture записан в `native-radio-realtime/<ID>/metrics/native_source_summary.json`. До cochannel-помехи 532 MPDU/5 с, во время 0 и 273 PHY drops/5 с, после 1086/10 с. Контроль `...-05` со сдвигом только частоты 2412→2447 МГц: во время источника 552 MPDU/5 с, PHY drops 0. Proof: `control/delivery-source-packets-20261107/spectral-overlap-verification.json`. Это native RF/packet-наблюдения, не application PDR или MAC/SLA-классы карт; аппаратная и совместная многопользовательская приёмка открыты. |
| Интерфейс, серии, установка | Частично: loopback UI `bas.py ui` редактирует/проверяет конфигурацию, запускает controller, показывает причины и экспортирует артефакты; HTTP API запустил успешный RT N=1. Batch сохраняет immutable attempts, seeds/runs/repeats и отмену; реальная серия двух seeds N=1 прошла. Независимый чистый clone `d045f3b` прошёл SDK13/O3 bootstrap/check-only, pinned image и 75 hash-checked wheels: `control/delivery-bootstrap-sdk13-o3-probe-20261108/`. Отдельная чистая машина и независимый оператор остаются открытыми. |
| Длительные опыты | Реализовано и локально проверено на `7a250f5` (68 tests): duration до 86400 с, расчёт native/service lifetimes из длительности и существующих фаз, обязательный disk capture свыше 2400 с; readiness, radio/cache и stop guards не ослаблены. Часовая и восьмичасовая нагрузочная квалификация не подтверждены; ёмкость диска и длительная стабильность требуют целевой проверки. |
| RT workers и когерентный кэш | Частично: новый N=5 `delivery-rt-coherent-n5-20261108-08` на чистом `42dc494` — completed / succeeded / valid. Все пять маршрутов, посадок и разоружений подтверждены; окно 20,000 source / 20,005 wall с. 7016 coherent commits, 825450 cache hits / 0 misses, максимальный host cache age 96,91 мс при пределе 100 мс. RT/pose журналы полностью сверены. N=1 source `…-07` на той же версии также completed / succeeded / valid: 5140 commits, 10149 hits / 0 misses. Совместная приёмка с помехами, полной нагрузкой и оборудованием открыта. |

Артефакты находятся на ПК4 в `/home/bas/bas_v2-core/runs/`:
`control/<ID>/run.json` и `runtime.log`, `native-radio-realtime/<ID>/metrics/` и `logs/`.
Все попытки, исходные итоги и ревизии сохраняются; повторная проверка имеет отдельный выход.
Публикационная ветка и результаты статьи не изменяются.

Причина исходной непосадки исправлена: свежий LAND больше не заменяется BRAKE после
истечения lease; локальное UART-наблюдение отделено от радио-подтверждения.
N=1..5 на 07.11 и новый N=5 RT `delivery-rt-coherent-n5-20261108-08` на `42dc494`
подтвердили маршруты, посадку и разоружение всех аппаратов. Исправлен ToS на UAV TAP:
настоящий TAP/native Wi-Fi regression дал control TID5/AC_VI, payload TID1/AC_BK,
p95 residence очереди 402,78 → 1,26 мс. Это EDCA, не полный UART/SLA:
`control/delivery-qos-contract-20261107/verification.json`.

Общие OBJ используются Gazebo collision/visual и Sionna: 10×10 км, 200 м рельефа,
здания до 45 м, 32768 terrain triangles. DART crash устранён добавлением normals.
`control/delivery-terrain-probe-20261107/runtime-result.json` и исходники bundle
сохраняют трансформации/хеши; геопривязка и ENU/NED проверяются при подготовке.
`runtime-terrain.json` и три входа в `network/config/` воспроизводят короткие явно
заданные маршруты пяти UAV и источник +5..10 с после начала измерений.
Подготовка: `prepare_terrain_scene.py --output .external/terrain-reference`.
Этот пример не проверяет всю площадь карты или дальность 20–50 км.

RT packet path использует C++ CIR cache, без Python/RT/I/O на cache lookup.
Исправлены повторная carrier phase, Doppler Hz и эпоха CIR; SDK13 содержит opaque
движущиеся входы и fixed storage axis PathSolver. Предел 90 не обрезает найденные пути:
переполнение останавливает опыт, padding проверяется и снимается при экспорте.
Фактические Sionna RT 1.2.0 / Mitsuba 3.7.1 / DrJit 1.2.0, source hashes и счётчики
проверяются в ready/results/final; idle worker не считается проверенным live producer.
CPU/GPU SDK13 proof: `control/delivery-fixed-path-storage-runtime-20261108/`
(`build-02-command.json`, fullmesh/emitter/receiver contracts).
Различия full-mesh и одиночных RT sampling сохраняются; pointwise сходимость ещё
не подтверждена (100K→500K до 4,789 дБ): `control/delivery-fullmesh-rt-probe-20261108/`.
Старый координатный shortcut hybrid отклонён регрессией; public hybrid закрыт до квалификации.

Измеренные задержки устранены в нескольких действующих границах: RT poll/warm poll
1 мс с сохранением 100 мс guards; чтение поз на native zero перед первым dispatch;
источник со стартом +5 с не решается постоянно до measurement anchor.
Журналы RT и Gazebo/ROS tracker вынесены в общий bounded stdlib QueueListener.
Синхронный архив на HDD ранее задержал live-публикацию на 130,017 мс при продолжающейся
odometry. Теперь source timestamps сохраняются, atomic live publication отделена от архива;
оба closed ledgers сверяются с реальными JSONL count/bytes/SHA и участвуют в итоговом gate.
Доказательства: `source-publication-05.json`, `checks-09-command.json` (439 tests)
и worker/journal поля последующих `run.json`.

На `42dc494` новый N=5 `delivery-rt-coherent-n5-20261108-08` прошёл весь цикл:
7016 coherent commits, 825450 hits / 0 misses, максимальный host cache age 96,91 мс
при 100 мс; RT 7019 записей и pose 16242 записей полностью сверены.
N=1 source `delivery-rt-source-bundle-n1-20261108-07` также completed / succeeded / valid,
5140 commits, 10149 hits / 0 misses. Предыдущий `…-06` на `db64320` имел успешный
полёт, но первичный failed / invalid из-за потери coupling в загрузчике отчёта.
Real prepare→loader regression и отдельный `reanalysis-source06-after-loader-fix.json`
подтвердили исправление; исходный итог `…-06` не переписан.

Совместный public N=5 `delivery-terrain-five-source-public-20261108-01` на `3314484`
failed / invalid до включения источника: host cache age >100 мс. Подробные trace-контроли
`delivery-terrain-five-trace-20261108-{01,02,03,04,05}` также не прошли; ни один не считается
приёмкой. В первой попытке native lag ~49 мс делал причинные входы старыми уже на dispatch;
публикации 10 мс и odometry 20 мс исправны. Trace05 с actual /proc observer (449 samples,
без пропусков) показал другой случай: SDK solver 43,39 мс, затем следующий результат
не успел за оставшиеся 33,66 мс. Устойчивой перегрузки main thread здесь не доказано;
scheduler lag при отказе 2,83 мс. Ошибка clock anchor на 2–18 мс не подтвердилась.
Proof с SHA: `control/delivery-fixed-path-storage-runtime-20261108/n5-trace-05-observed-01/`
(`trace05-causal-analysis.json`, `clock-anchor-analysis.json`).
Неудачные попытки подключения observer в `…-03/04` не выдаются за измерения.

CPU-проба штатного fmt 8.1.1 проверила byte-exact 8237 сохранённых CSV строк, 15 edge cases
и 12 измерений. С текущим ReadPhase: 2404 → 1959 нс CPU/строку; выигрыш около 18 мс CPU/с
при 40K строк/с не объясняет задержку solver. Production formatter не заменён.
Proof: `control/delivery-csv-fmt-cpu-probe-20261108/attempt01/{result,ledger}.json`.
Следующий исполнимый RT шаг — наблюдение worker0 в совместном запуске через штатный perf
и guarded /proc observer, затем исправление подтверждённой причины и новый полный N=1/N=5.
Guards, budgets и RF-модель ради успеха не ослаблять.
Отдельно установлен дефект распределения ресурсов: на Ryzen 9 9950X logical CPU
16..23 являются SMT-соседями stack CPU 0..7, хотя прежний radio mask 8..31 их включал.
Новый per-run partition использует sysfs package/core и фактически доступный affinity,
сохраняет SMT-соседей в одной роли; применяет существующий taskset, хост не перенастраивает.
50 локальных и target tests прошли, включая noncontiguous masks, два socket и отсутствие topology.
`checks-18` подтвердил actual taskset child masks и ноль физических пересечений:
`cpu-topology-application-01.json`. N=1 10kbps `…-03` всё равно failed до нагрузки:
последний завершённый job 21,48 мс, следующий ответ не наблюдался за 51,37 мс.
Native lag 0,275 мс, tracker исправен. Proof: `n1-10kbps-03-causal-analysis.json`.
CPU-tail этим исправлением не устранён. Replay 50 original-pose заданий без retimestamp
дал hot p95 16,81 мс, новых JIT misses/GC внутри producer не наблюдалось;
live причину это не устанавливает: `trace05-replay-02-derived-verification.json`.

Нагрузочный downlink `flows.payload_load` реализован штатным MAVLink TUNNEL через
существующий FlightHarness/BSF1/общий канал, без нового транспорта. На `5be6c3f`
`delivery-payload-n1-1kbps-20261108-01` завершил полный цикл: 157/157 полезных сообщений,
1004,8 бит/с за точные 20 с (целое число сообщений), ноль generator skips.
Это прежний PTY без baud pacing, не аппаратная задержка или SLA класса.
Первый анализ отклонил различие planned 20,000 с и фактической фазы 20,002 с;
`57ac639` проверяет frozen schedule внутри наблюдаемого окна. Real UDP/BSF1/OS pipe
регрессия RED→GREEN, 29 target tests и отдельный analysis-02 подтверждают generator/lineage
eligibility. Оба анализа сохранены: `control/delivery-fixed-path-storage-runtime-20261108/`
`payload-n1-1kbps-analysis-{01,02}/`. N=1 10kbps `…-01/02` остановились до нагрузки
по cache freshness; класс не проверен. Upstream и шесть классов остаются открытыми.

`50c780b` применяет общий SerialBaudBudget только к Linux PTY в uart-adapter;
реальный TTY paced драйвером. Raw UART bytes, отдельные duplex clocks, bounded credit,
partial/EAGAIN, исходные timestamps и stop учтены; BSF1/IP не расходуют baud.
На ПК4 10 tests прошли: два одновременных production PTY 57600/115200, wire-time floor,
частичные записи, EAGAIN и SIGTERM перед выдачей очередей (`checks-13-command.json`,
`development-paced-01/`). `504f172` подключает тот же budget к владельцу control UART
с сохранением UARTAdmission/lease/deadline и FIFO HOLD→LAND. Два дефекта аргументов
Linux-теста исправлены отдельно (`72985b1`, `7d951c7`); исходные checks-15/16 сохранены.
На `7d951c7` real control-PTY прошёл отдельно и в полном наборе:
475 tests (474 pass, 1 skip), `checks-17-command.json`, `development-control-solo-02/`
и `development-paced-control-03/`. Это программные границы, без RF/hardware.
Новый диагностический N=1 RT `delivery-payload-n1-10kbps-perf-20261108-01` на чистом
`9747259` прошёл весь цикл за 120,27 с: completed / succeeded / valid, 4924 RT records
сверены, оба paced UART, 391/391 payload timely, 10009,6 бит/с, p95 8,47 мс.
Независимый анализ `rt-perf-n1-payload-analysis-01/` подтвердил lineage и sampled runtime
timing; это одиночный опыт с диагностической нагрузкой, не доказательство устойчивости.
Perf/proc сохранили данные, но завершились с ошибкой identity guard при выходе процессов;
валидность границ и символов проверяется отдельно, статусы collector не переписываются.
На `e734ab4` простой профиль `delivery-simple-payload-n1-10kbps-20261108-01`
выполнил весь полёт с обоими paced UART и нагрузкой. Однако отдельный анализ
отклонил load-window: source clock завершил фазу чуть раньше заданных 20 с host time.
Исправление `measure()` ожидает оба объявленных интервала и сохраняет exact host end;
после source cutoff новые control offers/goals не создаются. Причинная регрессия
2,998 с вместо 3 с воспроизведена RED, затем 67 tests (65 pass, 2 POSIX skip) GREEN.
На `4ef00a8` повтор N=1 `…-02`, N=2..5 `…-01` прошли полный цикл и независимый анализ
обоих окон: 391 planned/created на UAV, без generator skips. N=1: 391/391 timely,
10009,6 бит/с, p95 8,44 мс до UART OS-write end. N=5: 385–391 timely на UAV,
p95 36,63–435,55 мс, p99 до 523,02 мс. Это исправный измерительный тракт с наблюдаемой
деградацией связи, не приёмка SLA; нужны причинный разбор хвостов и повторная серия.
Все raw commands/exit codes и исходные итоги сохранены; производные данные и SHA:
`simple-series-02-verification.json` в той же папке control.
Отдельная регрессия дробных duration 1,001/1,003/1,005 с устранила рассогласование
`int`/`round` на 1 нс между `measure` и аудитом нагрузки; 54 focused tests прошли.
Минимальный публичный вход — `runtime-link-test.json` с tracked миссией, без ссылок на `runs/`.
Разбор 19 missing доказал ошибку promiscuous MacRxMiddle: чужой адресат загрязнял
учёт одинакового source/TID/sequence, свой успешный retry терялся до TAP, затем BSF1
удерживал следующие записи. SDK14 разделяет QoS unicast state по RA; PHY/ARQ/promisc
и timeout не менялись. Actual FEM/RxMiddle regression RED (retry/fragment cases) → GREEN
(6 cases), четыре stock Wi-Fi suites: 119 child PASS. Доказательства и SHA:
`simple-n5-10kbps-causal-01/{causal.json,red01,green02}/` в той же папке control.
Публичный `delivery-public-link-n5-macfixed-20261108-02` на чистом `e703c86` прошёл весь
цикл за 185,90 с: 391/391 timely на каждый UAV, 10009,6 бит/с, p95 20,71–23,37 мс,
p99 28,70–34,59 мс; независимые runtime/lineage checks прошли. Это один опыт, не SLA серии.
Предыдущий `…-01` остановился до симуляции из-за неверно размещённой shell declaration
в раздельном коммите; исправление проверено actual Bash для всех четырёх scene branches
(`launcher-declaration-regression-01.json`), исходный failed attempt сохранён.

Источники действительно влияют на Spectrum PHY. Старый полный N=1 на `2065c4e`
и контроль со сдвигом 2412→2447 МГц разделили cochannel loss и внеполосный источник:
`control/delivery-source-packets-20261107/spectral-overlap-verification.json`.
Новые fixed-role desired/source bundles проверены CPU/GPU и в N=1 `…-07`.
RSS/SINR/J/S GPU карты проверены: `control/delivery-map-probe-20261107/verification.json`.
Это RF snapshots, не MAC/SLA карты; доступность классов и совместный N=5 остаются открытыми.

UI HTTP/Chrome ранее проверен на `2a1a436`; свежий HTTP UI на `7d951c7` проверил
terrain/source→10kbps validation, четыре negative cases и actual result/manifest export
со сверкой байтов и отказом traversal. Proof: `control/operator-ui-20261108/current-payload-01/http-api-proof.json`.
Это HTTP validation/export, не browser DOM или native effect новых полей; временный сервер закрыт.
Новый SSH tunnel заблокирован автоматической проверкой, браузерная проверка не выполнена.
Последовательные
серии сохраняют все immutable attempts; историческая серия двух seeds N=1 прошла.
Чистый независимый clone `d045f3b` прошёл SDK13/O3 bootstrap 278,84 с и check-only 21,05 с:
`ready=true`, `gaps=[]`, 75 hash-checked wheels, общих inode нет.
Proof: `control/delivery-bootstrap-sdk13-o3-probe-20261108/`; receipt SHA
`b180159201e869d0e0a183df993a189ea5bd36caaa17e2ae20126beaee3e455b`.
Это новый checkout на том же ПК4 с существующим pinned image, не отдельная машина,
сборка image или независимый оператор. Чистая установка текущей версии, длительные
нагрузочные серии, квалифицированный hybrid и физическая аппаратная приёмка открыты.

**1. Что сдаём**

Оператор без изменения исходников выбирает или создаёт сценарий из поддержанных элементов,
задаёт параметры, проверяет конфигурацию, запускает 1–5 UAV и одну НПУ, наблюдает полёт,
каналы и источники помех, подключает реальный контроллер через Serial/Ethernet,
сохраняет результаты и повторяет опыт. Основной демонстрационный состав — пять UAV + НПУ.

| Обязательная возможность | Граница первой поставки |
|---|---|
| Мир | Карта 10×10 км, рельеф с перепадами до 200 м, ПГТ со зданиями до 15 этажей; единые геометрия, координаты и единицы для физики и радио |
| UAV | Одна квалифицированная модель мультикоптера с ArduPilot SITL, 1–5 экземпляров, индивидуальные позиции, высоты, маршруты и профили параметров |
| Радио | Один общий радиоресурс; point-to-point и point-to-multipoint; известный MAC, очереди и повторные передачи; профиль 2,4 ГГц / 1–2 Вт с явно заданными антеннами и полосой |
| Данные | Одновременные MAVLink control и payload поверх двух UART; документированное отображение в общую радиосеть, приоритет управления |
| Распространение | Sionna RT для сложной геометрии и квалифицированные простые модели для подходящих участков; явные условия выбора модели |
| Помехи | Отдельные моделируемые источники: положение, мощность, частота/полоса, диаграмма, ориентация и расписание работы |
| Карты связи | RSS, SINR, J/S и доступность заданных классов нагрузки; выбранная высота, момент/окно времени, направление связи и допущение о трафике |
| Radio-HitL | Проверенный обмен с реальным устройством через Serial и Ethernet; время хоста, ограниченные очереди и измеренные задержки |
| Работа оператора | Компактный интерфейс выбора сценария/параметров/запуска, наблюдение в Gazebo, журнал причин и отчёт; тот же механизм запуска доступен через CLI |
| Поставка | Закреплённая среда, исходники и рецепты сборки, установка на чистую поддерживаемую Linux-машину, примеры, инструкция и протокол приёмки |

Предварительное планирование/оптимизация миссий исключено согласно запросу заказчика.
Маршруты здесь являются входом теста. Полный HIL автопилота с подменой датчиков,
100×100 км, более пяти UAV, новые классы летательных аппаратов, ускоренный или
распределённый расчёт — отдельные расширения. Прикладное распознавание объектов
не требуется для проверки связи: соответствующие потоки воспроизводятся заданными нагрузками.

**2. Исходное состояние на 46e1a522 и обязательные уточнения**

- В core доступны только пять аппаратов, geometry_logdistance, ad-hoc и realtime;
  payload, GUI и помехи выключены. Это ограниченный профиль, а не готовое выполнение ТЗ.
- 38 целевых тестов прошли. В `core-smoke-20261105-01` взлетели пять UAV,
  измерено 60,011 с при RTF 1,000115; посадка/disarm всех пяти не подтверждены.
  Отдельная остановка во время взлёта UAV1 проверена. Полный успешный городской цикл не заявлен.
- Имеются serial/Ethernet адаптеры, очереди и источники Spectrum, но аппаратная
  проверка не выполнена. Внешний адаптер требует измерения и устранения вероятного
  ограничения скорости: 192 байта за итерацию и пауза 1 мс не доказывают 20 Мбит/с.
- Старый hybrid выбирает Friis по фиксированным координатным условиям. Он требует
  замены правил выбора; его нельзя переносить на произвольный рельеф как готовую модель.
- Текущая метрика SINR из MonitorSnifferRx относится к декодированным MPDU:
  для карты отсутствия связи и J/S нужны измерения, не зависящие от успешного приёма пакета.
- Данные 1–2 Вт, 2,4 ГГц и 10–20 км не определяют PHY/MAC реального модема.
  Native Wi-Fi — отдельный reference-профиль. Профиль модема требует сведений
  о полосе, модуляции/FEC/MCS, чувствительности/PER, duplex/MAC/ARQ, антеннах,
  буферах, пакетизации UART и управлении скоростью. Уточнить мощность на разъёме или EIRP.
- Диагональ карты 10×10 км около 14,1 км. Проверка 20 км и заявленных идеальных
  50 км выполняется в отдельной протяжённой сцене; эти дальности не гарантируются из мощности.
- Шесть уровней 1/10/100/500 Кбит/с, 2/20+ Мбит/с — требования к прикладному
  сервису, а не готовые радиорежимы. Уточнить единицы, на один UAV или суммарно,
  направления, одновременность, размеры/периоды сообщений, допустимые задержки и потери.
- UART и Ethernet принимаются отдельно. Например, UART 115200 8N1 передаёт
  максимум 92,16 Кбит/с байтов данных ещё до MAVLink/прочих накладных расходов.
  Требование 20+ Мбит/с нельзя обещать на таком физическом интерфейсе.

**3. Целевая архитектура**

```text
Сценарий + профили оборудования + seed
                 |
       проверка и разрешение параметров
                 |
     один контроллер запуска / CLI / интерфейс
                 |
Gazebo / модель UAV <-- штатный plugin --> ArduPilot SITL
          |
          +--> состояние / время / ENU --> адаптер радиоканала
                                             |
                                Sionna RT / простая модель
                                             |
НПУ или внешнее устройство <--> UART/Ethernet <--> native ns-3 PHY/MAC
                                                   ^
                                        спектральные источники помех
```

Ответственность: Gazebo — механика и датчики; SITL — автопилот; радиомодель —
распространение между соответствующими узлами; ns-3 — эфир, PHY/MAC и пакетный исход.
Прикладная ретрансляция — выбираемый сценарий, она не должна быть обязательной
для любой передачи. Контрольный трафик не получает скрытого обхода моделируемой сети.
Такое разделение опирается на [ArduPilot Gazebo](https://ardupilot.org/dev/docs/sitl-with-gazebo.html)
и [native ns-3 Wi-Fi](https://www.nsnam.org/docs/models/html/wifi-design.html).

Контроллер запуска управляет готовностью, дедлайнами стадий, остановкой и ресурсами.
Файловые снимки состояния можно сохранить при соблюдении свежести и причинности;
смена транспорта сама по себе не исправляет синхронизацию. Для одного хоста
не нужны Kafka/RabbitMQ, Kubernetes и отдельная база телеметрии. Очередь серий
может быть частью контроллера, результаты — обычными файлами. Одновременно один
опыт на хосте до отдельной квалификации изоляции и производительности.

Контракт адаптера фиксирует версии, ID узла/запуска/команды, время источника и
получения, систему координат, единицы, предел свежести и реакцию на пропуск данных.
Передача состояний не допускает применения будущих поз к прошлым радио-событиям.
Учитываются ENU/NED, AGL/AMSL и географическая привязка, в том числе над рельефом.

Для HitL основной ход времени — wall-clock; часы источников измеряются отдельно.
Пауза/ускорение недоступны при подключённом физическом устройстве.
Измеряемая задержка модели сети отделяется от задержки вычислений/моста.
Превышение вычислительного бюджета даёт непригодность измерения, а не выдуманную
потерю в эфире. ns-3 realtime scheduler сам по себе не гарантирует своевременную
работу всего комплекса: [ns-3 RealTime](https://www.nsnam.org/docs/manual/html/realtime.html).

**4. Публичная конфигурация**

Один документ сценария с версией ссылается на переиспользуемые профили. Перед стартом
сохраняется полностью раскрытая конфигурация. Порядок приоритетов: стандартный
профиль → сценарий → явные настройки оператора/CLI. Скрытые переменные окружения
не меняют научные параметры. Для каждого поля: единицы, диапазон, значение по
умолчанию, совместимость, точка применения и способ чтения фактического значения.

| Группа | Настраиваемые параметры |
|---|---|
| Сцена | Геопривязка, размер, рельеф/DEM, здания, материалы, физический шаг, точки НПУ/UAV; контроль согласованности collision и radio geometry |
| UAV | ID/SYSID, модель/профиль прошивки, spawn, ориентация, маршрут, высота с указанием отсчёта, скорость, failsafe; физические параметры только в поддержанном диапазоне модели |
| Радио | Частота, полоса, мощность с указанием точки определения, антенны/ориентации, шум/коэффициент шума приёмника, PHY/MAC, режимы скорости, ARQ, очереди, topology и routing как разные настройки |
| Распространение | Модель, материалы/параметры, настройки RT, условия выбора hybrid, шаг/предел ошибки кэша, максимальный возраст канала |
| Потоки | control/payload, направления, baud/framing, MAVLink IDs, размеры/периоды сообщений, полезный bitrate, burst, приоритет и deadline |
| Помехи | ID, координаты/высота, диаграмма и ориентация, PSD/полоса/частота, мощность, start/stop и расписание; допустимое количество подтверждается нагрузочным тестом |
| HitL | Конкретный endpoint serial/UDP/TCP, endpoint ↔ канал ↔ UAV, serial pacing/flow control, reconnect и ограничение очередей |
| Опыт | Длительность, число повторов, семена всех случайных подсистем, критерии миссии, пределы времени и ресурсов, состав журналов |
| Карты | ROI, сетка/адаптивный шаг, высота, момент или окно, желаемый передатчик, активные источники, направление связи и метрика |

Изменение поля должно менять нужный backend и читаться обратно из эффективных
настроек. Неизвестные поля, невалидные сочетания и неподдерживаемые режимы
отклоняются до старта с понятной причиной. Частота должна совпадать у PHY и RT;
изменение мощности не должно добавляться дважды. Изменение массы без согласованной
модели динамики/настройки управления не принимается как произвольный «ползунок».

**5. Этапы выполнения**

Каждый этап завершается проверяемым изменением в отдельной ветке, при необходимости
делится на небольшие PR. Публичная ветка статьи и исторические результаты сохраняются.
До восстановления SSH разработка плана локальная; тяжёлые запуски — на назначенном
интеграционном хосте под общим lock. Откат — к предыдущему рабочему runtime/profile;
исходные результаты и конфигурации запусков никогда не перезаписываются.

**Этап 0 — согласовать приёмку и снять главные технические риски. Зависимости: нет.**
Контекст: ТЗ выше, `runtime.json`, `network/config/*`, таблица текущего выполнения.
Получить модель/образец контроллера и спецификацию модема; закрепить карту и
материалы, ОС/CPU/GPU/RAM, набор и предел источников помех, UART/Ethernet режимы.
Задать предварительный предел сложности карты (здания/треугольники/материалы),
глубины и бюджета RT, числа частотных вариантов, источников (проверка минимум двух
одновременных), разрешения/частоты обновления и одновременных запросов карт.
Первые пробы должны покрыть худшее допустимое сочетание этих параметров.
Зафиксировать по каждому классу трафика полезную скорость, задержки p95/p99/max,
допустимые потери/возраст сообщений и условия доступности. Первые пробы: runtime
адаптера Ethernet на 20+ Мбит/с, реальный serial loopback, RT выбранной городской
области с холодным/тёплым кэшем. Получить базовый бюджет времени и памяти.
Зафиксировать соответствие часов host monotonic/Gazebo/SITL/ns-3, начало отсчёта,
максимальные lag/skew и поведение при сбросе часов. Искусственно задержать каждый
компонент: нарушение должно обнаруживаться даже при исправных остальных процессах.
Определить остановку/инвалидирование интервала и условия возобновления; в HitL
после нарушения требуется новая подтверждённая синхронизация, а не скрытое догоняние.
Выход: измеренные ограничения и согласованная таблица «требование → профиль → проверка».
Если данных модема нет, отдельно согласовать поставку reference-модели; соответствие
реальному модему остаётся открытым обязательством, а не подразумеваемым свойством Wi-Fi.

Пользователь поручил BAS определить недостающие рамки приёмки. Ниже закреплены
**внутренние инженерные критерии delivery-v1** до квалификационных серий.
Это цели поставки; старое `measurement_validity=valid` подтверждает только записанные
runtime guards конкретного опыта. Короткий опыт не подтверждает статистический SLA.

| Критерий | Рамка delivery-v1 и граница измерения |
|---|---|
| Control | Создание уникального обновления → последняя UART-запись: p95 ≤100 мс, p99 ≤150 мс; доля доставленных до 250 мс ≥99,9%. Просроченные/повторные grants никогда не применяются; действующий canonical deadline 250 мс сохраняется. |
| Jitter control | Для соседних доставленных offered slots одного UAV/потока/epoch: p95 абсолютной разности задержек ≤50 мс, p99 ≤100 мс. Пары через потери исключаются из jitter и остаются в delivery denominator. |
| Command transaction | Короткий запрос → соответствующий ACK: p95 ≤250 мс, p99 ≤400 мс, общий deadline 1 с вместе с retries. Принятие не заменяет проверку выполнения. |
| Payload | Базовый поток: p95 ≤250 мс, p99 ≤500 мс, уникальные записи до 1 с ≥99%; объекты/файлы требуют размера, deadline завершения и проверки целостности. |
| Перегрузка / потеря связи | Control выполняет критерий до заявленной максимальной нагрузки. Выше — bounded drops/backpressure. Предусмотренный blackout не обязан сохранять nominal delivery; стенд сохраняет время, причины, deadlines и фактический failsafe. Возврат проверяется свежими IDs/telemetry, без вывода по одному накопленному ACK. |
| Время HitL | Неопределённость общих timestamps ≤5 мс для one-way; иначе оценивать RTT. Lag в измерительном окне: p99 ≤20 мс, observed max ≤50 мс; превышение делает соответствующий SLA-интервал непригодным. 250 мс остаётся аварийным abort, не допуском точности. Физическую выдачу байтов проверять отдельно от возврата write ОС. |
| Поза / RF cache | Возраст позы ≤min(100 мс, 1 м / заданная max скорость). 500 мс — защитный потолок, не доказательство точности. Cache/hybrid дополнительно проходят сравнение одинаково определённых RF-метрик. |
| Hybrid против full RT | Абсолютный bias ≤1 дБ, p95 абсолютной ошибки ≤3 дБ, p99 ≤6 дБ; ≥1000 парных точек отдельно на LoS, город, рельеф, границы тени/ориентации/помех. Сначала проверить сходимость full RT. Когерентный канал не сравнивать как ту же величину с некогерентной усреднённой radio map. |
| Классы нагрузки | 1/10/100/500 Кбит/с, 2/20+ Мбит/с полезных данных проверяются с фиксированными размерами/периодами, направлениями, активными UAV и deadlines. Всегда сохранять per-UAV и aggregate load. Карта класса учитывает конкурирующую нагрузку; непроверенное значение обозначается модельной оценкой. Ложное «доступно» ≤1% с односторонней уверенностью 95%. |
| Достаточность доказательств | ≥10 seeds и ≥10000 уникальных offered сообщений на поток/UAV для p99/SLA серии, исходы не объединять в среднее по UAV. При независимых событиях ноль нарушений из 2995 даёт 95% верхнюю границу около 0,1%; корреляцию оценивать по сериям/блокам. |
| Максимальная нагрузка / длительность | Ступени 25/50/75/90/100/110/125% ожидаемой ёмкости; максимальный прошедший профиль удерживает все критерии час, затем совместный soak 8 ч на 5 UAV. Все попытки и превышения остаются в серии. |

Обоснование: [MAVLink heartbeat](https://mavlink.io/en/services/heartbeat.html)
не задаёт обязательный timeout; [ArduPilot GCS failsafe](https://ardupilot.org/copter/docs/gcs-failsafe.html)
с default 5 с является аварийным механизмом. При refresh 1 Гц возраст удерживаемого
состояния до 1,25 с совместим с доставкой свежего обновления до 250 мс; это не inner loop.
Определения метрик: [RFC 7679](https://www.rfc-editor.org/rfc/rfc7679.html),
[RFC 3393](https://www.rfc-editor.org/rfc/rfc3393.html),
[MAVLink command protocol](https://mavlink.io/en/services/command.html).
Совместная проверка услуг опирается на подход [ITU-T Y.1564](https://www.itu.int/rec/T-REC-Y.1564-201602-I/en),
а численные пределы выше являются выбором BAS.

Ограничения профиля не скрываются: 115200 8N1 даёт 92160 бит/с байтов данных на
порт/направление до накладных расходов, поэтому ≥100 Кбит/с полезного потока нужен
более быстрый UART или Ethernet. 20 Мбит/с одновременно от каждого из пяти UAV —
≥100 Мбит/с полезного общего канала; ориентир pMDDL2450 с паспортными около 25 Мбит/с
этого не обеспечивает. Классы проверяются с явно заданной одновременностью;
пять потоков по 20 Мбит/с потребуют другого оборудования/радиорежима.

**Этап 1 — исправить и стабилизировать жизненный цикл. Зависимости: 0, можно начать по текущим логам.**
Контекст: `bas.py`, `relay_experiment.py`, runner; `core-smoke-20261105-01` на ПК4.
Разобрать UAV3/4/5: отправка LAND → приём → UART → режим → траектория → disarm →
доступность подтверждения. Различить реальную непосадку и потерю телеметрии.
Ввести независимые runtime_status, mission_outcome и measurement_validity;
завершение измерительного окна не означает выполнение маршрута. Добавить живой
статус компонентов, тайм-ауты стадий, владение ресурсами и восстановление после
смерти контроллера. Различать «посадить через сеть» и «прервать вычисление».
Проверки: штатный цикл, отказ компонента, потеря связи, stop до готовности/в полёте,
повторный старт. Выход: нет ложного успеха, зависших процессов и загрязнения следующего опыта.

**Этап 2 — конфигурация, состав 1–5 UAV и независимые сценарии. Зависимости: 0; интеграция после 1.**
Контекст: `bas.py`, `service.py`, `urban_mission.py`, `prepare_urban_gazebo.py`, C++ scratch.
Убрать независимые константы пяти узлов; единый реестр ID/SYSID/портов/topic/node.
Разделить мир, аппараты, миссию, радио и воздействия. Отделить relay от обычного
НПУ↔UAV режима; случай одного UAV не требует фиктивного ретранслятора.
Ввести версионируемый конфиг, проверку совместимости и снимок применённых параметров.
Проверки: каждый N=1..5, дубликаты ID/портов, ошибочные ссылки, граничные параметры;
изменённые поля прослеживаются до backend. Выход: новый штатный сценарий без правки Python/C++/shell.

**Этап 3 — карта 10×10 км и единая геометрия. Зависимости: 0, 2.**
Контекст: генератор сцены, Gazebo SDF/models, вход сцены Sionna; прежний JSON содержит простые боксы.
Подготовить рельеф до 200 м и ПГТ, дать зданиям высоты в метрах и радиоматериалы.
Использовать общий источник геометрии; визуальное упрощение не должно незаметно
менять препятствия радио/столкновений. Проверить AGL/AMSL, посадку на склоне/ровной
площадке, точки появления и геопривязку. Подготовить отдельную дальнюю линию для 20–50 км.
Проверки: контрольные координаты/высоты и пересечения лучей в обоих представлениях,
расход RAM/VRAM/время загрузки. Выход: карта загружается и все пять летают в согласованных координатах.

**Этап 4 — распространение и производительный hybrid. Зависимости: 0, 2; полная карта после 3.**
Контекст: `native-spectrum-sources.h`, RT integration и текущий pinned Sionna fork.
Зафиксировать проверенную версию Sionna и реально доступные механизмы. Удалить
выбор простой модели по магическим координатам; выбирать её по явной области
применимости, геометрии/рельефу/условиям Fresnel и утверждённому пределу ошибки.
Проверить свободное пространство, отражение от земли, холм/край, угол здания,
перекрытие Fresnel и границы моделей. Не смешивать когерентную интерференцию
многолучёвости с некогерентным сложением мощностей радиокарты.
Кэшировать каналы/пути с корректными ключами и возрастом; карта от НПУ не заменяет
движущиеся UAV↔UAV пары. Для шести узлов возможно 30 направленных пар плюс источники.
Изменение активности источника немедленно меняет спектральную нагрузку; оно не
должно ждать нового расчёта статической геометрии. Учитывать края тайлов и холодный кэш.
Выход: эталонные проверки точности и измеренные p95/p99/max расчёта на целевой машине;
устаревший или отсутствующий канал не маскируется молчаливым fallback.
Дополнительно сравнить hybrid с полной RT-моделью на тех же траекториях в выбранных
сложных областях и на переходах; reference допускает расчёт медленнее realtime.
До сравнения задать допуски ошибки канала/RSS, разрывов при переключении и границ
сервисных зон при одинаковой нагрузке. Совпадение с RT не заменяет натурную калибровку.

**Этап 5 — общая радиосеть и два потока. Зависимости: 0, 2; RT подключается после 4.**
Контекст: native ns-3, `serial_transport.py`, QoS, coordinator service.
Включить control/payload одновременно через один радиоресурс; восстановить
полезную нагрузку без обхода ns-3. MAC/PHY/ARQ/очереди соответствуют выбранному
профилю; простая ad-hoc связь не заявляется автоматически как mesh routing.
Реализовать приоритет управления и предсказуемые ограниченные очереди; профили
адаптации скорости использовать только при наличии модели/данных, а не по произвольному RSS-порогу.
Проверки: одновременная передача пяти узлов, насыщение payload, разные packet sizes,
полудуплекс/дуплекс согласно профилю, очереди и deadlines, все классы из документа заказчика.
Выход: измерены полезные bitrate/PDR/latency/jitter по направлениям и классам;
20+ Мбит/с подтверждается только для подходящего интерфейса/радиорежима и условий.

**Этап 6 — помехи и карты связи. Зависимости: 2, 4, 5.**
Контекст: `prepare_native_sources.py`, native Spectrum, измерения PHY и будущий модуль карт.
Переиспользовать WaveformGenerator/PSD и настроенные диаграммы; добавить только
недостающие заявленные формы спектра/расписания. Считать распространение от каждого
источника до приёмника и спектральное перекрытие во времени; не учитывать одну помеху дважды.
Проверить источник вне/внутри полосы, on/off, ориентацию, несколько источников и смену позиций.
Измерять желаемую мощность S, отдельно J и N в заданной полосе, включая зоны без
декодированных пакетов. Карта сервисного уровня учитывает нагрузку/MAC/профиль
модема и SLA; одна карта RSS не доказывает доступность приложения.
Выход: синхронизированные карты с легендой/единицами/высотой/временем/допущениями,
сопоставленные с измерениями пакетов в контрольных точках; известен предел ошибки и свежести.

**Этап 7 — radio-HitL Serial/Ethernet. Зависимости: 0; адаптеры параллельно, сквозная приёмка после 4–6.**
Контекст: `external_endpoint.py`, `serial_transport.py`, реальное устройство заказчика.
Зафиксировать границу: реальные UART/сетевые байты проходят через модель радио;
это не проверка физического RF-тракта модема и не полный HIL датчиков автопилота.
Указать замещаемый внешний узел, источник его положения для радиорасчёта и тестовый
режим контроллера; не обещать физическому автопилоту замкнутый контур датчиков Gazebo
как автоматическое следствие подключения COM-порта.
Проверить baud/framing, byte pacing, flow control, размеры/возраст буферов, reconnect,
потери и deadlines; устранить программный предел Ethernet после измерения.
Проверять с реальным портом и контроллером, сначала loopback, затем сеть, RT и помехи.
Логировать timestamps входа/выхода моста, вычислительный lag, моделируемую задержку,
глубину очередей и фактические timeout/failsafe устройства. При overload не выдавать
накопленные устаревшие команды пачкой и не записывать вычислительную задержку как RF-потерю.
Выход: оба интерфейса прошли аппаратный протокол; нагрузка payload и cold-cache RT
не нарушают согласованные временные бюджеты. В отсутствие оборудования этап не закрывается.
Обязателен отдельный аппаратный опыт с двумя UART одновременно: двунаправленный
MAVLink control/payload через общий эфир, насыщение payload, измерение задержки
control, правильное соответствие канал/UAV и отсутствие перекрёстной доставки.
Проверить разрыв посреди MAVLink-кадра и корректное восстановление потока после reconnect.

**Этап 8 — интерфейс сценариев и отчёт. Зависимости: 2; сквозные экраны после 5–7.**
Контекст: `bas.py` и существующие JSONL/CSV; собственного пользовательского интерфейса пока нет.
Сделать выбор/копирование сценария, редактирование поддержанных полей, проверку,
start/stop, состояния компонентов и устройств, 3D через Gazebo, карты связи и графики.
Показывать фактическую/заданную траекторию, истинное/оценённое состояние, свежесть
телеметрии, очереди и причину окончания. Добавить сохранение/загрузку сценария и
отчёт с конфигурацией, версиями, итогами и исходными журналами. Онлайн-наблюдение
не должно становиться причиной нарушения HitL-бюджета; тяжёлые карты считать отдельно.
Выход: другой инженер создаёт вариант сценария, запускает его и находит причину
деградации без редактирования кода и поиска по десяткам файлов.

**Этап 9 — серии и воспроизводимость. Зависимости: 2, 5; покрытие RT/помех после 6.**
Контекст: run directories и анализаторы; публичная серия статьи неизменяема.
Добавить последовательные серии и перебор параметров/семян, лимит числа опытов,
оценку объёма хранения, отмену серии после безопасной остановки текущего опыта.
Сохранять все попытки, применённые параметры, версии, семена, конфигурацию машины,
данные кэша/модели и причины невалидности. Разделять повторное воспроизведение
журнала и новый эксперимент; realtime не обещает побитовое совпадение.
Выход: одинаковый замысел запуска воспроизводится из сохранённого пакета;
статистическое сравнение использует заранее заданные допуски и все исходы попыток.

**Этап 10 — совместная квалификация и производительность. Зависимости: 1–9.**
Контекст: матрица приёмки этапа 0 и реальные артефакты всех этапов.
На целевом хосте одновременно проверить пять UAV, карту 10×10 км, оба канала,
направленные источники, Sionna/hybrid, HitL и включённое наблюдение в предельном
профиле сложности и нагрузки, согласованном на этапе 0. Отдельные
успехи компонентов этого не заменяют. Зафиксировать предел источников, частоту
обновления канала и карт, разрешение сетки, CPU/GPU/RAM/диск и холодный старт.
Предлагаемые испытания: 20 повторов номинального полного цикла; 1..5 UAV;
не менее 3 seed для ключевых сравнений; серия 8 ч; нагрузка за границей допуска;
обрыв/возврат устройства, остановка backend и заполнение выделенной квоты журналов.
Проверить время до обнаружения отказа, отсутствие скрытого обхода радио и утечек ресурсов.
Числа испытаний и допустимые отклонения утверждаются в этапе 0; это не доказательство
универсальной безотказности. Выход: обязательные сочетания прошли, ограничения перечислены,
все критические дефекты устранены; ожидаемая потеря миссии при радиопотере трактуется по сценарию.

**Этап 11 — выпуск и сдача. Зависимости: 10; упаковка начинается после 2.**
Контекст: нынешняя установка использует архив среды и ручную ROS-сборку.
Подготовить закреплённый установочный комплект, воспроизводимые рецепты сборки,
версии/лицензии компонентов и ассетов, hardware requirements и офлайн-установку,
если у заказчика закрытая сеть. Убрать привязки к /home/bas, ПК4 и личным данным.
Сохранить исходники, тесты, примеры, README и инструкцию оператора; raw и тяжёлые
assets/образы хранить отдельно. Проверить сетевую доступность интерфейса, управление
доступом при удалённой работе и необходимые привилегии TAP/serial без лишних служб.
Выход: другой человек на чистом целевом хосте устанавливает, настраивает своё
устройство, выполняет номинальный и помеховый/HitL сценарии, экспортирует отчёт;
заказчику переданы release, протокол приёмки, известные пределы и порядок поддержки/отката.

**6. Проверки, которые не заменяются демонстрацией**

Каждый поддержанный параметр проходит проверку схемы, применения/readback и
смысленного влияния в изолированном опыте. Все N=1..5 и ключевые пары параметров
проверяются явно; полный декартов перебор не нужен. Для источников/канала проверяют
контрольные случаи и крайние условия, а не требуют монотонности случайного PDR
в одном коротком запуске. Семена повторяются по плану; вывод о точности требует
эталона либо натурных измерений с заранее согласованной ошибкой.

Минимальная библиотека приёмки: один полёт; пять разнесённых маршрутов; LOS/NLOS
в ПГТ; затенение холмом; дальняя линия; насыщение payload при control; совместный
доступ пяти узлов; направленный/внеполосный/включаемый источник; потеря и возврат
связи с заданным failsafe; radio-HitL отдельно Serial и Ethernet; холодный кэш/overload.
Результат «миссия не выполнена из-за предусмотренного воздействия» может быть
правильным результатом симулятора. Он отличается от падения процесса и от
прогона с нарушенным временем. Согласованный сценарий определяет ожидаемый исход.

**7. Порядок, команда и предварительная оценка**

Критический путь: 0 → 1/2 → 3/4/5 → 6/7 → 10 → 11.
Интерфейс (8), серии (9) и подготовка поставки идут параллельно после стабилизации конфигурации.
Существующие детали переиспользуются; рефакторинг runner выполняется по рабочим
границам адаптеров, с сохранением предыдущего профиля для сравнения каждого изменения.
Самостоятельное переписывание физики, автопилота и MAC в план не входит.

Оценка при трёх профильных разработчиках (runtime/HitL; radio/ns-3/Sionna;
конфигурация/UI/поставка) и участии испытателя — **12–18 календарных недель**.
Это инженерная оценка до технических проб, не обещание даты. Условия: доступ к
оборудованию и спецификациям в начале работ, согласованный поддерживаемый PHY/MAC,
доступная карта и целевая машина. При необходимости разработки собственного
PHY/MAC модема, отсутствующем оборудовании или непрошедшем RT-бюджете срок пересчитывается.

| Интервал, ориентировочно | Основной результат |
|---|---|
| Недели 1–2 | Матрица приёмки, modem/interface budget, разбор посадки, первые RT/throughput/serial пробы |
| Недели 3–6 | 1–5 UAV, конфигурация, карта, два потока, базовые native радио-профили |
| Недели 5–10 | Проверенный hybrid, источники и карты, сквозной аппаратный HitL; интерфейс и серии |
| Недели 10–14 | Совместные испытания, производительность, исправления, установка на чистый хост |
| Недели 15–18 | Резерв на выявленные интеграционные дефекты и приёмку, если потребуется |

После этапа 0 оценка пересматривается по измерениям. Изменение модели/предела/критерия
поставки отражается в этом одном плане и матрице приёмки, а не в новых разрозненных статусных файлах.

**8. Проверенные технические источники**

Sionna описывает [radio maps](https://nvlabs.github.io/sionna/rt/api/radio_map_solvers.html)
и [некогерентное сложение путей в radio-map solver](https://nvlabs.github.io/sionna/rt/tech-report/S4.html).
Возможности текущей документации требуют проверки в закреплённой версии стенда.
Карты SINR не заменяют моделирование расписания передач MAC и фактического трафика.
Для источников во времени/частоте опора — [ns-3 Spectrum](https://www.nsnam.org/docs/models/html/spectrum.html).
Для физического мира — [Gazebo SDF worlds](https://gazebosim.org/docs/harmonic/sdf_worlds/).
Рекомендации о кэше, этапах и бюджетах выше — проектные решения, которые ещё предстоит измерить.
