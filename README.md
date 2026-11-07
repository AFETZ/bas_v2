# BAS core

Рабочий профиль совместного моделирования на одном Linux-хосте: Gazebo,
ArduPilot SITL, геометрическая модель или Sionna RT и native ns-3 Wi-Fi.
Это отдельная ветка развития стенда; публикационная серия сохранена отдельно.

| Часть | Ответственность | Реализация |
|---|---|---|
| Среда | Геометрия, столкновения, физика и показания датчиков | Gazebo / DART |
| Дрон | Модель корпуса и двигателей; управление полётом | Iris в Gazebo + ArduPilot SITL |
| Распространение | Потери сигнала с учётом положения и геометрии | `geometry_logdistance` или явный профиль Sionna RT в ns-3 |
| Сеть | Пакеты, Wi-Fi PHY/MAC, очереди и повторные передачи | native ns-3, ad-hoc |

Распространение выделено логически, но работает внутри ns-3. Отдельный сервис
или брокер ему не нужен. Общая сцена описана в `network/config/urban_low.json`:
из одного снимка генерируются здания Gazebo и входные данные радиомодели.
Модель распространения исследовательская, без калибровки по натурным измерениям.

## Запуск на ПК4

```sh
cd /home/bas/bas_v2-core
python3 bas.py inspect
python3 bas.py run --run-id urban-01
```

Настройки четырёх частей и опыта — `runtime.json`. Состав сцены должен точно
соответствовать `drone.count` (1–5, внутренние ID 1..N). Профиль использует
realtime и ad-hoc; по умолчанию — geometry_logdistance. Неподдержанные значения
отклоняются; автоматического переключения на другую модель нет.
`--duration 60` задаёт короткое измерительное окно; подготовка и посадка идут отдельно.
Это проверка соединения компонентов, а не воспроизведение всей серии статьи.

Локальный интерфейс использует тот же конфиг и контроллер:

```sh
python3 bas.py ui --port 8765
```

Откройте `http://127.0.0.1:8765/`: загрузите конфигурацию, создайте копию,
измените JSON-параметры и при необходимости сцену, проверьте и запустите опыт.
Отдельно видны состояние стенда, результат миссии и пригодность измерений;
доступны журнал, остановка и скачивание артефактов. Снимки ввода сохраняются в
`runs/control/ui-inputs/<id>/`. Интерфейс доступен только на loopback; на Windows
работают редактирование и проверка, запуск симуляторов требует Linux.
Для удалённого Linux-хоста используйте SSH-туннель с одинаковыми портами:
`ssh -L 8765:127.0.0.1:8765 <user>@<host>`. Закрытие UI не останавливает опыт.

Для Sionna создайте копию `runtime.json` и замените весь объект `propagation`:

```json
{
  "model": "sionna",
  "radio_config": "network/config/native_wifi_80211n_spectrum_product.yaml",
  "ground_material": "medium_dry_ground",
  "building_material": "concrete",
  "thickness_m": 0.2
}
```

Частота сцены должна совпадать с `radio.carrier_hz` профиля. Профиль задаёт
native Wi-Fi 802.11n, мощность, общий спектральный канал и параметры PathSolver,
включая `samples_per_src` и `max_num_paths_per_src`. Это инженерная модель,
не калиброванная модель Microhard. Sionna действительно рассчитывает канал;
отказ RT останавливает запуск, подмены геометрической моделью нет. Hybrid пока
не входит в проверенный публичный конфиг. Значения solver/cache не являются
гарантией real-time: пригодность проверяется по фактическим метрикам опыта.
`runtime.sionna_log_level` — строка `info` (по умолчанию) или `debug`, применяется
при новом запуске. `info` сохраняет сообщения о решениях solver и структурные
метрики; `debug` дополнительно пишет обращения к кэшу на каждом пакете и влияет
на производительность. Разрешённое значение входит в снимок конфигурации.

Офлайн-карты RF строятся `scripts/product/compute_radio_maps.py --config <json>
--output <new-directory>` в закреплённом GPU runtime. Проверка входа без GPU:
`--validate-only`. `point_grid` использует PathSolver и iso/V приёмники в центрах
до 256 ячеек; `radio_map` — штатный RadioMapSolver и неполяризованное усреднение
по ячейке. Конфиг задаёт подготовленные `mission_scene`, `scene_xml`, `radio_config`,
желаемый передатчик, приёмную полосу/температуру/NF, область/шаг/высоту/время и
необязательные развёрнутые источники из `prepare_native_sources.py`.
Результаты — `maps.npz`, `metadata.json`, `point_checks.json` и снимки входов.
RSS, SINR и J/S описывают RF-срез: занятость MAC, очереди и доступность классов
нагрузки из них не выводятся. Нулевые выборки RT имеют отдельную маску.

Из другого терминала:

```sh
python3 bas.py status urban-01
python3 bas.py events urban-01
python3 bas.py events urban-01 --event uart_admitted --limit 5
python3 bas.py events urban-01 --command-id '<command_id из журнала>'
python3 bas.py stop urban-01
```

`stop` создаёт запрос: при следующей обработке миссия прекращает задания и
пытается выполнить LAND через моделируемую сеть. Подтверждение запроса ещё
не означает посадку. Итог `stopped` означает завершённую остановку; ошибка
посадки сохраняется как `failed`. Во время раннего запуска запрос ждёт
готовности миссии. Ctrl+C отправляет такой же запрос. Принудительное убийство
процессов не является штатной остановкой.

Все запуски используют общий `/tmp/bas-paper-runtime.lock`: отдельные каталоги
и имена контейнеров не изолируют порты и TAP. При занятости команда возвращает 75.
Старый идентификатор запуска повторно использовать нельзя.

В `flows.channels` выбираются `["control"]` или `["control", "payload"]`;
по умолчанию включены оба UART. Payload запрашивает настоящий ATTITUDE у SITL
с периодом `flows.payload_attitude_interval_s` через тот же native эфир.
Прикладные команды полёта остаются в control. Метрики payload —
`metrics/payload_channel.json`. Это не подтверждение аппаратной пропускной способности.

Результаты разделены на `runtime_status`, `mission_outcome` и
`measurement_validity`. Завершение окна не подтверждает прохождение маршрута.
Посадку проверяют по допущенной UART-команде LAND, режиму автопилота,
снижению, земле и disarm. Если радио-подтверждение потеряно, используется свежая
локальная UART-запись для наблюдения; источник указан в результате, управление
продолжает проходить через радио. Lease failsafe сохраняет наблюдаемый LAND.

Последовательная серия использует тот же конфиг и контроллер:

```sh
python3 bas.py batch --config runtime.json --batch-id example --seeds 1,2 --rng-runs 1,2 --repeats 2 --dry-run
python3 bas.py batch --config runtime.json --batch-id example --seeds 1,2 --rng-runs 1,2 --repeats 2
```

План и все исходы серии сохраняются в `runs/control/<batch-id>/batch.json`.
Отмена текущего опыта прекращает серию. Ошибка runtime или невалидные измерения
останавливают очередь; неуспешная миссия при исправном стенде сохраняется как исход.

Подготовка двух внешних каналов выполняется существующими BSF1-адаптерами:
`python3 scripts/product/test_radio_hitl.py --config <request.json> --prepare --output <new-directory>`.
Каталог `network/config/hitl_reference_devices.json` содержит документированные
примеры Pixhawk 6C/Cube Orange+, Microhard P2400/pMDDL2450. Это интерфейсные профили,
без подтверждения рыночной доли в России, равенства Wi-Fi реальному модему и
аппаратной приёмки. `--dry-run` печатает разрешённые настройки без открытия устройств.

## Обмен и время

```text
Gazebo / Iris <-- ArduPilotPlugin --> ArduPilot SITL
     |
     | /clock и /model/uavN/odometry, Gazebo Transport
     v
position_tracker --> атомарный снимок ENU + история --> ns-3
                                                       |
                                       geometry_logdistance

НПУ --> TAP / native Wi-Fi --> ретранслятор --> native Wi-Fi
    --> адаптер UART --> SITL; ответы идут обратно через сеть
```

Физика и датчики принадлежат Gazebo; управление — SITL; исход доставки пакета —
ns-3. Прикладной координатор выбирает ретранслятор, но не подменяет радиомодель.
`network/position_tracker` переносит состояния, `network/scripts/serial_transport.py`
и `network/coordination/service.py` обслуживают сетевую границу и UART.
`bas.py` задаёт профиль и управляет жизненным циклом; низкоуровневый запуск
пока остаётся в `network/ns3/run_native_radio_five_uav.sh`.

Координаты среды — ENU, SITL использует NED; преобразование задано в модели и сцене.
В журналах различаются время Gazebo, монотонное время хоста и часы автопилота.
Глобальный lockstep всего стенда здесь не заявлен: ns-3 работает в realtime,
адаптеры проверяют свежесть и рассогласование. Предельный возраст позы 500 мс,
рассогласование источников 100 мс, lag ns-3 250 мс сохранены в существующих адаптерах.
Kafka, RabbitMQ, Redis и отдельная БД для этого профиля не используются.

## Результаты

- `runs/control/<id>/config.json`, `scene.json`, `run.json`: снимок параметров,
  версия исходников, эффективное окружение запуска и итог процесса.
- `runs/control/<id>/runtime.log`: вывод запуска.
- `runs/native-radio-realtime/<id>/`: исходные журналы, траектории, параметры,
  PCAP и `metrics/relay_experiment_summary.json`.

`events` объединяет существующие прикладные журналы по монотонному времени
одного хоста, сохраняя `command_id`, исходное событие и имя файла. Он не добавляет
предполагаемые события при потере пакетов. Радиособытия и PCAP остаются отдельными
источниками: сам `uart_admitted` не доказывает конкретную цепочку PHY/MAC событий.
Для траекторий используйте `logs/node_state.jsonl`, для ресурсов —
`logs/runtime_resources.jsonl`, для радиолинии — `logs/geometry_links.csv`.

## Зависимости и разработка

На ПК4 подготовлены собственные `.external`, `build`, `install`; изменяемые
каталоги не связаны с рабочей копией статьи. Используется существующий Docker
образ, закреплённый по ID в `runtime.json`. Новых контейнерных образов нет.
Восстановление использует закреплённый образ и архив native-зависимостей;
Dockerfile для полной пересборки этого образа в данной копии нет. Установка
текущей delivery-ветки на чистую машину пока не испытана.

Для переноса нужен выделенный Linux x86-64 хост с Docker, Bash, Git, Python
3.10+, PyYAML, NumPy, `flock`, `tar` и `zstd`; для CUDA RT — драйвер NVIDIA и
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
Исходники берите из текущей delivery-ветки вместе с `web/operator.html`.
Закреплённый образ и native ns-3 с git-метаданными доступны в
[публикационном выпуске](https://github.com/AFETZ/bas_v2/releases/tag/urban-article-v7-20261104).
Это исходная среда для обновления кодом delivery-ветки, а не её готовая сборка.
Архив журналов для установки не нужен. Скачайте `native-runtime.tar.zst.part00`,
`native-version.tar.zst.part00`, все четыре части `runtime-image.tar.zst.part00`–`03`
и [artifacts.json с SHA-256](https://github.com/AFETZ/bas_v2/blob/ed260ca9de57bb76df4de5baa8471a651814d499/artifacts.json).
В новой рабочей копии задайте `ASSETS` как абсолютный путь к скачанным файлам:

```bash
set -euo pipefail
ASSETS=/absolute/path/to/delivery-assets
python3 - "$ASSETS/artifacts.json" > "$ASSETS/SHA256SUMS" <<'PY'
import json, sys
from pathlib import Path
for artifact in json.loads(Path(sys.argv[1]).read_text())['artifacts']:
    if artifact['group'] == 'runtime':
        for part in artifact['parts']:
            print(part['sha256'] + '  ' + part['name'])
PY
(cd "$ASSETS" && sha256sum -c SHA256SUMS)
tar --zstd -xf "$ASSETS/native-runtime.tar.zst.part00" -C "$PWD"
tar --zstd -xf "$ASSETS/native-version.tar.zst.part00" -C "$PWD"
cat "$ASSETS"/runtime-image.tar.zst.part{00..03} | zstd -dc | docker image load
IMAGE="$(python3 -c 'import json; print(json.load(open("runtime.json"))["runtime"]["image"])')"
docker image inspect "$IMAGE" --format '{{.Id}}'
```

При несовпадении контрольной суммы не распаковывайте архив. Не накладывайте
старые архивы поверх уже изменённой `.external`. ROS-пакет собирается из текущих
исходников в том же образе и по тому же пути, который использует runner:

```sh
docker run --rm -i --network none --user 0:0 \
  -v "$PWD:/workspace/multiagent_simulation" -w /workspace/multiagent_simulation \
  "$IMAGE" bash <<'SH'
set -e
source /opt/ros/humble/setup.bash
source /workspace/ardu_ws/install/setup.bash
colcon build --packages-select multiagent_simulation --symlink-install
SH
```

Контроллер `bas.py run` переиспользует готовый native бинарник. После переноса или
изменения C++ его нужно собрать действующим inline bootstrap в
`network/ns3/run_native_radio_five_uav.sh`. Он проверяет точный ns-3.48
`d2add90b452d600cfb4859baed8e9ea633519447`, применяет совместимый стек патчей
из `network/ns3/patches/` (включая solver-budget и baseband-spectrum-phase) и собирает целевой scratch target.
Используются `.python-deps-py310` и `.tooling-py310` внутри `.external/ns-3-sionna-native`:
native Sionna/Sionna RT 1.2.0, Mitsuba 3.7.1 и Dr.Jit 1.2.0. Импорт библиотек из
другого site-packages образа не подтверждает версию native окружения.

Первая сборка следующим способом также запускает короткий интеграционный опыт,
занимает общий lock и пишет отдельные исходные артефакты. Это не приёмка миссии:
20-секундного окна недостаточно для штатного полного маршрута.

```sh
flock -n -E 75 /tmp/bas-paper-runtime.lock python3 - <<'PY'
import subprocess
import bas
config = bas.load_config(bas.ROOT / 'runtime.json')
config['experiment']['duration_s'] = 20
env = bas.environment(config, 'native-bootstrap-01')
env['BAS_NATIVE_FIVE_SKIP_BUILD'] = '0'
raise SystemExit(subprocess.run(['bash', 'network/ns3/run_native_radio_five_uav.sh'], env=env).returncode)
PY
```

Для повторной попытки выберите новый ID. После сборки используйте обычный
`python3 bas.py run --run-id <new-id>`; проверка возраста исходников запрещает
молча использовать старый бинарник. Модульные и программные интеграционные тесты
запускаются через `python3 -m unittest discover -s tests` в подготовленном
runtime-окружении. Они не заменяют полётную и аппаратную приёмку.

Для генерации общей треугольной геометрии нужен только дополнительный
[trimesh 4.10.1 (MIT)](https://pypi.org/project/trimesh/4.10.1/), без extras.
`requirements-terrain.txt` содержит точную версию и SHA-256 wheel. На машине с
интернетом скачайте его, затем перенесите `.external/terrain-wheels` на целевой хост:

```sh
python3 -m pip download --no-deps --only-binary=:all: --require-hashes \
  -r requirements-terrain.txt -d .external/terrain-wheels
python3 -m pip install --no-index --no-deps --only-binary=:all: --require-hashes \
  --find-links .external/terrain-wheels --target .external/terrain-python \
  -r requirements-terrain.txt
PYTHONPATH="$PWD/.external/terrain-python" python3 scripts/product/prepare_terrain_scene.py \
  --output .external/terrain-reference
```

Используйте Python с установленным NumPy; его бинарные модули должны соответствовать
версии Python. Команды pip не обновляют NumPy или native-зависимости.
Проверка хеша и установка без индекса описаны в [документации pip](https://pip.pypa.io/en/stable/topics/secure-installs/).
Путь `terrain_bundle` в JSON сцены задаётся относительно корня проекта, например
`.external/terrain-reference`. Сценарий должен согласовать начало ENU, частоту,
позиции, высоты и материалы с `source.json`/`scene-manifest.json`; для рельефа
требуется Sionna. Генератор создаёт процедурный пример 10×10 км, до 200 м рельефа
и до 15 этажей, общие OBJ для collision/visual Gazebo и Sionna, контрольные суммы
и копию MIT-лицензии. Это не съёмка местности заказчика; подготовка геометрии
сама по себе не подтверждает полёт, столкновения или real-time RT на такой карте.

Лицензия исходников — GPL-3.0; лицензии сторонних компонентов сохраняются в их
архивах. При передаче включайте закреплённую среду, метаданные ns-3, wheel и
лицензии вместе с исходниками. Архивы, зависимости и результаты исключены из Git.

Проверки этой версии и наблюдаемые ограничения: [network/STATUS.md](network/STATUS.md).
