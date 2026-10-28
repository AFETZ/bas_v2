# Независимая проверка маршрута северного осмотра

28.10.2026. Проверены `urban_north_inspection.json`, генератор `prepare_urban_gazebo.py`,
настоящий SDF неудавшегося preflight `urban-north-pilot02-failover` и ConfigStore boundaries
предшествующего `urban-block-pilot01-failover`. Новых симуляций reviewer не запускал.

## Геометрия и научная область

- Collision и visual обоих корпусов SDF точно соответствуют JSON: A=[40..70,20..65,0..30], B=[95..125,20..65,0..25].
- Реальные `ns3::Building/Boundaries` ConfigStore предшествующего пилота равны тем же объёмам. У нового pilot02 ns-3 не стартовал; это не доказательство его нового радио-запуска.
- Все объявленные waypoint-сегменты, включая взлёт, проходят вне увеличенных на3м объёмов зданий. Это проверка планируемых отрезков; tracking error, торможение и фактические межбортовые дистанции требуют actual-поз.
- В показательной северной конфигурации waypoint2 CP→UAV1/2/5 геометрически NLOS; UAV3 и UAV4 имеют LOS к НПУ и всем четырём остальным. В финальной конфигурации такой LOS-граф есть у UAV1/2/3/4.
- Геометрическая допустимость не равна свежему peer-подтверждению или доставке пакетов. Аппараты приходят в точки в разное время; сценарий не должен принудительно синхронизировать actual-позы.
- Единственные изменения маршрутов относительно исходного эскиза — последние точки пяти аппаратов: вместо возвращения на юг они остаются на севере с +4м для осмотра фасада. Геометрия корпусов и все radio-коэффициенты совпадают побайтно как JSON-значения.
- Это допустимая **pilot-informed revision до freeze** с областью «осмотр при потенциально доступном обходном пути». Она не является заранее замороженной до первого пилота и не доказывает работу алгоритма на произвольном маршруте.
- Сохранить исходный failed return-route пилот как отрицательный пример: исправление маршрута не отменяет потерю последнего follower при возвращении резерва. Новый маршрут и старый результат не объединять молча.
- При final north-позах снижение relay на западной стороне сохраняет геометрически открытый путь к UAV5 севернее зданияB. Это объясняет применимость маршрута; успешный LAND всё равно должен наблюдаться в новых raw.
- В добавленном `native_radio_command_post` есть только visual-marker, без collision; он не является третьим неучтённым радиозданием. Его центр[5,0,2] отличается от actual CP[0,0,2] — графики/подписи должны использовать actual CP, а визуальный маркер желательно согласовать отдельно.

## Обнаружен и передан блокер скорости до freeze

`mission.speed_mps=3` и ACK ACCEPTED на DO_CHANGE_SPEED не обеспечили полёт3м/с.
Независимый scan deduplicated actual Gazebo velocities исходного300s пилота:

| UAV | Full PARAM_VALUE `WP_SPD`, m/s | max actual horizontal speed, m/s |
|---|---:|---:|
|1|10|10,024343528|
|2|10|10,033123469|
|3|10|10,036768059|
|4|10|10,040253394|
|5|10|10,001656081|

Все пять WP_SPD имеют index206/count1388, типREAL32; у всех есть DO_CHANGE_SPEED178 ACK result0.
Первичные значения сохранены в `return_route_requested_speed.csv` с provenance/hash.
Указание3м/с как фактической скорости этих пилотов было бы неверным.

Причина согласуется с pinned ArduPilot `3c073f9a09590307e99f49f960dd0f4dac7fc5bb`:
`ModeGuided::pva_control_start` вновь устанавливает speed из WPNav default при инициализации position-подрежима;
предшествующий DO_CHANGE_SPEED изменяет controller limits, но не постоянный parameter default.
У новой версии имя/единицы **WP_SPD, m/s**, а не прежний WPNAV_SPEED, cm/s.
Рекомендуется явно загрузить WP_SPD3.0 штатным SITL default вне окна, подтвердить PARAM_VALUE всех пяти
и повторно проверить actual скорость в новой версии пилота. Это исправление исполняемой конфигурации;
radio/lease не менять, старые raw оставить с фактически измеренной скоростью.

Источники: [pinned AC_WPNav.cpp: SPD](https://github.com/ArduPilot/ardupilot/blob/3c073f9a09590307e99f49f960dd0f4dac7fc5bb/libraries/AC_WPNav/AC_WPNav.cpp#L43),
[get_default_speed_NE_ms](https://github.com/ArduPilot/ardupilot/blob/3c073f9a09590307e99f49f960dd0f4dac7fc5bb/libraries/AC_WPNav/AC_WPNav.h#L86),
[guided controller initialization](https://github.com/ArduPilot/ardupilot/blob/3c073f9a09590307e99f49f960dd0f4dac7fc5bb/ArduCopter/mode_guided.cpp#L230).

## Startup failure и upstream конфигурация Transport

Pilot02 сохранил настоящий clock_missing: одометрия поступала, clock callback не поступил.
Stats показывают работающую симуляцию; topic-info, снятый интегратором, показывал publisher/subscriber
с автоматически выбранным100.114.158.53. Этого недостаточно для доказательства причины сбоя.
Минимальное исправление `1bca7c5` закрепляет документированный GZ_IP127.0.0.1 у всех компонентов
одного хоста и пишет настройку в environment. Это уже используемый в проекте/upstream подход,
не синтетические часы и не ослабление freshness. Эффект нужно подтвердить новым raw.
Причинный вывод «Tailscale сломал clock» пока не установлен; автоматические повторы старого failed raw запрещены.
[Gazebo Transport13: GZ_IP](https://gazebosim.org/api/transport/13/envvars.html),
[официальный ros_gz localhost test](https://github.com/gazebosim/ros_gz/blob/ros2/ros_gz_sim/test/test_gz_simulation_interfaces.launch.py).

## Команды, без запусков стенда

```bash
python3 reports/ispras/urban_ns3/tools/review_urban_geometry.py \
 --config network/config/urban_north_inspection.json \
 --world runs/native-radio-realtime/urban-north-pilot02-failover/sitl/urban-native-live-cameras.sdf \
 --ns3-attributes runs/native-radio-realtime/urban-block-pilot01-failover/logs/ns3_attributes.txt \
 --previous-config network/config/urban_two_building.json \
 --output reports/ispras/urban_ns3/north_route_static_review.json
python3 reports/ispras/urban_ns3/tools/review_requested_speed.py \
 runs/native-radio-realtime/urban-block-pilot01-failover \
 --output reports/ispras/urban_ns3/return_route_requested_speed.csv
```

Вывод: статических блокеров общей геометрии/планируемых сегментов нет. До научной freeze нужны
новый actual-пилот с подтверждённой скоростью, real connectivity/полномочиями и корректным учётом failed preflight.
