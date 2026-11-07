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
В radio YAML `sionna.receiver_batch_mode` принимает `pairwise` (по умолчанию)
или `receiver_batch`. Второй режим использует штатный PathSolver сразу для
приёмников CP/UAV с iso 1×1 V антеннами; число samples сохраняется, лимит путей
выделяется на каждого приёмника. Переполнение буфера останавливает расчёт.
Это отдельный режим с измеряемым отличием выборок RT; его точность относительно
реального модема и допустимая ошибка заказчиком пока не утверждены.
`sionna.execution_mode`: `in_process` (по умолчанию) или `worker_pool`.
Для pool нужны `receiver_batch_mode: receiver_batch`, `synthetic_array: true`,
`cache_expiry_jitter_fraction: 0`, не более 90 путей, возраст/смещение кэша ≤0,5 с/м.
`worker_count` — целое 1–3, по умолчанию 3; в `in_process` допустимо только 3.
Подготовка сохраняет defaults в `generated/radio-resolved.yaml`; исходный YAML не изменяется.
Проверка отдельных workers не подтверждает полный полёт N=5 в realtime.
`runtime.sionna_log_level` — строка `info` (по умолчанию) или `debug`, применяется
при новом запуске. `info` сохраняет сообщения о решениях solver и структурные
метрики; `debug` дополнительно пишет обращения к кэшу на каждом пакете и влияет
на производительность. Разрешённое значение входит в снимок конфигурации.
`runtime.event_logging` — `metrics_only` (по умолчанию, суммарные счётчики)
или `batched_trace` (пакетные события PHY/MAC и наблюдения источников).
Для сравнения влияния помех включайте `batched_trace` до запуска; сводка явно
показывает отсутствие пакетных наблюдений при отключённой трассировке.

Помехи включаются полем `"interference": {"sources": "network/config/sources.yaml"}`
в конфигурации запуска, в том числе через JSON-редактор UI. Отсутствующий раздел
равнозначен `{"sources": null}`. Ненулевое значение требует Sionna; путь — YAML
внутри проекта. Пример содержимого файла:

```yaml
time_reference: measurement_start
sources:
  - id: interferer1
    position_m: [0, -40, 10]
    orientation_rad: [0, 0]
    pattern: tr38901
    gain_dbi: 0
    power_w: 0.01
    center_hz: 2412000000
    bandwidth_hz: 20000000
    start_s: 10
    stop_s: 20
    period_s: 0.01
    duty_cycle: 1
    mode: continuous
    spectral_shape: rectangular
```

`power_w` — мощность источника до дополнительного усиления и диаграммы.
Несмотря на имя, `gain_dbi` источника задаёт дополнительное усиление питающего
тракта в дБ, а не итоговый peak gain антенны: PSD равна
`power_w * 10^(gain_dbi / 10) / bandwidth_hz` ([место применения](network/ns3/scratch/native-spectrum-sources.h#L182)).
Для `tr38901` Sionna уже применяет штатную OUTDOOR-диаграмму: peak 8 dBi,
ширина 65°/65°, Amax/SLA 30 dB. Поэтому `power_w: 1`, `gain_dbi: 0` дают
peak EIRP ≈6,31 Вт. `gain_dbi: 8` добавит ещё 8 дБ к встроенным 8 dBi
(16 дБ в расчёте EIRP относительно `power_w`);
итоговый peak gain диаграммы в это поле не подставляют.
Внешняя Spectrum-антенна источника остаётся iso 0 dBi. Это поле не настраивает
Wi-Fi TX-антенну UAV/НПУ. SDK-конвертер `src/spectrum/model/sionna-rt-channel-model.cc`
(`GetAntennaElementPattern` / `MakePlannarArray`) передаёт тип `iso`/`tr38901`, но не настройки INDOOR/custom;
публичный профиль их не поддерживает.

Позиции заданы в общей ENU-сцене, ориентация — азимут/наклон в радианах.
`time_reference` — `simulation_start` (по умолчанию, секунды ns-3) или
`measurement_start` (секунды от первого наблюдения фазы `experiment_normal`
нативной моделью, опрос каждые 20 мс). Значение применяется при новом запуске.
`pulsed` задаёт duty cycle; для `sweep` вместо `center_hz` нужны
`sweep_hz` и `dwell_s`. Исходный YAML замораживается в
`runs/control/<id>/sources.yaml`, его SHA-256 входит в `run.json`; развёрнутое
расписание — `logs/native_sources.json`. После фактической привязки появляется
`logs/native_sources_actual.json`: anchor и абсолютные интервалы в наносекундах,
используемые ns-3. Если измерительное окно не началось, относительное расписание
не получает фиктивного anchor. Для карт такого запуска используйте actual-файл.
Источники проходят существующий
спектральный тракт ns-3/Sionna. Частоты проверяются по области применимости
выбранных ITU-материалов; просрочки real-time остаются ошибкой измерений.
Городской экспорт сохраняет `metrics/native_source_summary.json`: привязку
времени, события включения/выключения и доступность наблюдений в каждом окне.

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
ToS 184 для control и 40 для payload передаются штатному selector ns-3 и на
TAP-входе UAV: очереди AC_VI и AC_BK соответственно. Это EDCA; сквозные задержки
и доставку проверяют под нагрузкой отдельно от правильного выбора очереди.

Результаты разделены на `runtime_status`, `mission_outcome` и
`measurement_validity`. Завершение окна не подтверждает прохождение маршрута.
Посадку проверяют по допущенной UART-команде LAND, режиму автопилота,
снижению, земле и disarm. Если радио-подтверждение потеряно, используется свежая
локальная UART-запись для наблюдения; источник указан в результате, управление
продолжает проходить через радио. Lease failsafe сохраняет наблюдаемый LAND.

Инженерные критерии `delivery-v1` закреплены в `plans/customer-delivery.md`.
Существующий анализатор сохраняет per-UAV latency/jitter и все offered исходы:
`python3 scripts/product/analyze_relay_experiment.py --run-dir <raw-run>
--output-dir <new-analysis-directory> --no-plots --skip-live-export`.
Одиночный короткий запуск получает статус недостаточности доказательств;
он не подтверждает SLA серии. Отсутствующие ACK/payload измерения не заменяются
оценкой задержки control до UART.

Последовательная серия использует тот же конфиг и контроллер:

```sh
python3 bas.py batch --config runtime.json --batch-id example --seeds 1,2 --rng-runs 1,2 --repeats 2 --dry-run
python3 bas.py batch --config runtime.json --batch-id example --seeds 1,2 --rng-runs 1,2 --repeats 2
```

План и все исходы серии сохраняются в `runs/control/<batch-id>/batch.json`.
Хеши сцены, radio YAML и включённых источников проверяются перед каждой попыткой
и сопоставляются с фактическими снимками запуска; изменение входа останавливает серию.
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
каталоги не связаны с рабочей копией статьи. Используется Docker образ,
закреплённый по ID в `runtime.json`. Базовый рецепт `.devcontainer/Dockerfile`,
exact `.repos` и hashed pip lock перенесены из исходного проекта, ревизия
`4434789473643fa68d5f95953a44b4e0ae3e2c25`. `bash scripts/build_container.sh`
собирает этот рецепт, но версии apt-пакетов не заморожены: получить тот же ID
образа пересборкой не обещаем. Установка delivery-ветки на чистую машину пока
не испытана.

Для переноса нужен Linux x86-64 хост с Docker, Bash, Git, Python 3.10+, PyYAML,
NumPy, `flock`, `tar` и `zstd`; для CUDA RT — драйвер NVIDIA и
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
Исходники берите из текущей delivery-ветки вместе с `web/operator.html`.
Закреплённый образ доступен в
[публикационном выпуске](https://github.com/AFETZ/bas_v2/releases/tag/urban-article-v7-20261104).
Скачайте четыре части `runtime-image.tar.zst.part00`–`03` и
[artifacts.json с SHA-256](https://github.com/AFETZ/bas_v2/blob/ed260ca9de57bb76df4de5baa8471a651814d499/artifacts.json).
Архивы журналов и старого native runtime для новой сборки не нужны. В чистой
рабочей копии задайте абсолютный путь `ASSETS` к скачанным файлам:

```bash
set -euo pipefail
ASSETS=/absolute/path/to/delivery-assets
python3 - "$ASSETS/artifacts.json" > "$ASSETS/SHA256SUMS" <<'PY'
import json, sys
from pathlib import Path
for artifact in json.loads(Path(sys.argv[1]).read_text())['artifacts']:
    for part in artifact['parts']:
        if part['name'].startswith('runtime-image.tar.zst.part'):
            print(part['sha256'] + '  ' + part['name'])
PY
(cd "$ASSETS" && sha256sum -c SHA256SUMS)
cat "$ASSETS"/runtime-image.tar.zst.part{00..03} | zstd -dc | docker image load
IMAGE="$(python3 -c 'import json; print(json.load(open("runtime.json"))["runtime"]["image"])')"
docker image inspect "$IMAGE" --format '{{.Id}}'
```

При несовпадении SHA-256 не загружайте образ. Сборку выполняет обычный Linux-
пользователь в отдельной чистой рабочей копии. Новый
`scripts/product/bootstrap_native_runtime.py` использует canonical mount
`/workspace/multiagent_simulation`, общий lock стенда и собирает ROS-пакет,
ns-3.48 `d2add90b452d600cfb4859baed8e9ea633519447`, десять patches и оба native targets.
Симуляторы при этом не запускаются, GPU не требуется.

```sh
python3 scripts/product/bootstrap_native_runtime.py --check-only
```

`--check-only` возвращает JSON с конкретными пробелами и exit code 1, пока сборка
не подтверждена. Проверяются image ID, wheels и lock hashes, точный patched SDK
tree, private environment и build receipt. Пользовательские каталоги и
изменённые файлы сохраняются: при конфликте выберите чистую копию.
Архивные `.external`, `build` и `install` для этой процедуры не нужны.

На машине с интернетом один раз разрешите Python-зависимости внутри закреплённого
образа с Python 3.10. Штатный [pip wheel](https://pip.pypa.io/en/stable/cli/pip_wheel/)
включает транзитивные зависимости и собирает недостающие wheels. Затем создаются
два lock-файла с SHA-256, manifest и выполняется пробная установка без сети.
Это фиксация полученных wheels; побитовую повторяемость их исходной сборки не
обещаем. До успешной генерации полный lock отсутствует.

```sh
python3 scripts/product/bootstrap_native_runtime.py --resolve-lock \
  --lock-bundle .external/native-runtime-wheels
python3 scripts/product/bootstrap_native_runtime.py --bootstrap \
  --lock-bundle .external/native-runtime-wheels
python3 scripts/product/bootstrap_native_runtime.py --check-only \
  --lock-bundle .external/native-runtime-wheels
```

Передавайте весь wheel bundle вместе с исходниками и образом. Python
устанавливается только из этих wheels с `--no-index --require-hashes`;
отсутствующий SDK загружается из официального Git с обязательной проверкой HEAD.
Для автономной установки также заранее подготовьте точный patched checkout SDK.
Native окружение отдельно от базового: Sionna/Sionna RT 1.2.0, Mitsuba 3.7.1,
Dr.Jit 1.2.0, pybind11 2.11.1, cppyy 3.5.0, CMake 3.31.6. Обычные site-packages
образа содержат другую версию RT. Хеши входов и результатов сохраняются в
`.external/native-bootstrap.json`, команды и вывод сборки —
в `.external/native-bootstrap.log`. Успешная сборка ещё не подтверждает E2E.

Для отдельной проверки runtime используйте `python3 bas.py run --run-id <new-id>`.
Модульные и программные интеграционные тесты запускаются через
`python3 -m unittest discover -s tests` в подготовленном runtime-окружении.
Они не заменяют полётную и аппаратную приёмку.

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

Критерии приёмки, выполненные проверки и открытые ограничения: [plans/customer-delivery.md](plans/customer-delivery.md).
