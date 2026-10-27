# BAS v2

Проверенные live-результаты для статьи: [основные 30 запусков](doc/LIVE_LEADER_RESULTS.md),
[параметры, оператор и отдельные 18 попыток чувствительности](doc/LIVE_LEADER_FOLLOWUP_RESULTS.md).
[Анонимный черновик PDF, 12 страниц](paper/build/manuscript_anonymous.pdf),
[DOCX](paper/build/manuscript_anonymous.docx), [CSV и графики](paper/README.md).
Полные raw и assets хранятся вне Git; failed и неполные post-выгрузки сохранены в отчёте.

Стенд для моделирования пяти БАС: ArduPilot SITL управляет полётом в Gazebo,
ns-3 моделирует радиосеть, Sionna RT рассчитывает распространение сигнала.
Один наземный пункт управления обменивается с пятью аппаратами реальными
MAVLink-байтами через десять UART-каналов, TAP и штатный Wi-Fi PHY/MAC ns-3.48.

[Быстрый старт](#быстрый-старт) · [Тесты](#тесты) · [Запуск стенда](#запуск-стенда)
· [Результаты](#результаты) · [Документация](#документация)

## Версия для тестов

**`main` — единственная постоянная рабочая ветка.** Воспроизводимая версия
для тестов закреплена тегом **`test-baseline-2026-10-21`**. В неё входят исправления
свежести ACK, барьера приёма GCS и генерации диагностического отчёта.

Тег фиксирует исходники и зависимости. Полный запуск Gazebo/SITL/ns-3/Sionna
нужно проверять на целевой машине: успешные компонентные тесты не подтверждают
готовность GPU-стенда, физического контроллера или работу в реальном времени.
Фактические результаты и ограничения записаны в [network/STATUS.md](network/STATUS.md).

Завершённые ветки и избыточные теги удалены после проверки истории. Помимо эталона
оставлены два архивных тега с историей, которой нет в `main`:

- `archive/2026-10-21/agent/publish-network-radio-milestones` — старый процесс разработки.
- `archive/2026-10-21/feature/bas-v2-rc1-stabilization` — работы RC1, включая демозапись и передачу стенда.

Архивы предназначены для восстановления. Для текущих тестов используйте эталон
выше. Коммиты удалённых milestone-тегов остаются в истории `main`.

## Что моделируется

| Компонент | Ответственность |
| --- | --- |
| Gazebo + ArduPilot SITL | Физика, датчики, управление и автономная миссия каждого БАС |
| ns-3.48 | Штатные 802.11n SpectrumWifiPhy/MAC, очереди, конкуренция за эфир, доставка и потери |
| Sionna RT | Распространение, отражения и состояние радиоканала внутри процесса ns-3 |
| MAVLink и UART-адаптеры | Два канала на аппарат: `control` и `payload`; передача исходных байтов |
| Дополнительный трафик | P2P, multicast P2MP и одновременная передача через ту же радиосеть |
| Источники помех | Штатные ns-3 WaveformGenerator с заданными мощностью, частотой и расписанием |

```text
Сценарий / MAVProxy ↔ GCS TAP ↔ ns-3 Wi-Fi + Sionna RT ↔ UAV TAP ↔ UART ↔ ArduPilot
                                                                             ↕
                                                                           Gazebo
```

Профиль Wi-Fi является некалиброванной инженерной моделью. Payload UART несёт
MAVLink-трафик. Прямой обход радиосети и незаявленная замена Sionna запрещены;
подробности — в [архитектуре](doc/PRODUCT_ARCHITECTURE.md).

## Требования

| Задача | Окружение |
| --- | --- |
| Редактирование и компонентные Python-тесты | Windows или Linux; Git и Python 3.10; зависимости из lock-файла |
| Linux-проверки UART и барьеров | Linux с PTY и `flock` |
| Полный стенд | Ubuntu 22.04, Docker Engine с NVIDIA Container Toolkit, совместимые NVIDIA GPU/драйвер и рабочий CUDA/OptiX |
| Town01 и customer-сцена | Оригинальные внешние CAVISE assets; встроенный `rock_demo` их не требует |

В Windows setup-скрипт использует [uv](https://docs.astral.sh/uv/getting-started/installation/).
Полный runtime работает в Linux. WSL2 требует отдельной проверки OptiX:
успешный `nvidia-smi` подтверждает доступ к GPU, но не работу трассировки лучей.
Пошаговая подготовка хоста: [LOCAL_DEVELOPMENT.md](doc/LOCAL_DEVELOPMENT.md).

## Быстрый старт

Получите фиксированную версию для тестов:

```bash
git clone https://github.com/AFETZ/bas_v2.git
cd bas_v2
git switch --detach test-baseline-2026-10-21
```

Для работы с текущим кодом используйте `git switch main`. Не переносите готовые
виртуальные окружения между Windows и Linux — создайте их на целевой системе.

### Linux: окружение разработки

При установленных Python 3.10 и модуле `venv`:

```bash
bash scripts/setup_dev.sh
source .external/dev-venv/bin/activate
```

### Windows: окружение разработки

В PowerShell из корня репозитория:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_dev.ps1
.\.venv\Scripts\python.exe -m pytest -q network/tests/test_gcs_lockstep.py network/tests/test_native_latency_report.py
```

Оба setup-скрипта используют [.devcontainer/requirements-dev.lock](.devcontainer/requirements-dev.lock).
Окружение разработки не устанавливает Gazebo, SITL или ns-3.

## Тесты

Базовый набор для транспортного пути, модельного времени и отчётов
запускается без симуляторов. В Linux после активации окружения:

```bash
python -m pytest -q \
  network/tests/test_gcs_lockstep.py \
  network/tests/test_simulation_clock.py \
  network/tests/test_serial_transport.py \
  network/tests/test_data_transport.py \
  network/tests/test_native_latency_report.py \
  network/tests/test_native_wifi_five_uav_product.py
```

В Windows можно передать те же файлы `.\.venv\Scripts\python.exe -m pytest`.
Тесты, которым нужны Linux PTY/`flock`, будут пропущены; этот результат нужно
учитывать отдельно от успешных проверок.

Для собственных изменений выбирайте ближайшие тесты по
[TEST_MATRIX.md](network/TEST_MATRIX.md). Проверки синтаксиса, ссылок и diff:

```bash
make test-changed
bash network/tests/check_five_uav_micro_ros_ports.sh
```

`make test-changed` проверяет изменения относительно `HEAD` и не заменяет pytest.
Для уже созданного коммита задайте базу: `BASE_REF=HEAD~1 make test-changed`.

## Запуск стенда

Команды этого раздела выполняются на подготовленном **Linux-хосте с Docker**.
Начните со встроенной сцены `rock_demo`, чтобы не зависеть от внешних карт.

```bash
make demo-preflight DEMO_SCENARIO=rock_demo DEMO_GUI=0 DEMO_BOOTSTRAP=1
BAS_NATIVE_FIVE_RUN_ID=baseline-rock make demo-rugged DEMO_GUI=0
```

Bootstrap создаёт отсутствующие зависимости и собирает окружение по закреплённым
версиям; первая подготовка может занять значительное время. Preflight проверяет
в том числе CUDA/OptiX. При ошибке сначала устраните её причину.

### Режим времени

По умолчанию используется `SIMULATION_MODE=realtime`: контролируются сроки
выполнения и возраст состояния. Для пошагового обмена модельным временем:

```bash
BAS_NATIVE_FIVE_RUN_ID=baseline-lockstep make demo-rugged \
  DEMO_GUI=0 SIMULATION_MODE=lockstep LOCKSTEP_STEP_MS=20
```

В `lockstep` RTT и таймауты относятся к модельному времени. Такой запуск не
подтверждает работу в реальном времени; внешние физические контроллеры в нём
не поддерживаются. При смене режима начинайте новый запуск.

### Town01, диагностика и управление

После подготовки оригинальных CAVISE assets по инструкции окружения:

```bash
export CAVISE_MAPS_DIR=/absolute/path/to/bundles
make demo-preflight DEMO_SCENARIO=town01 DEMO_GUI=0 DEMO_BOOTSTRAP=1
BAS_NATIVE_FIVE_RUN_ID=baseline-town01 make demo-town01 DEMO_GUI=0
```

Диагностика задержек без полётного сценария:

```bash
BAS_NATIVE_FIVE_RUN_ID=latency BAS_NATIVE_LATENCY_MODE=1 make demo-town01 DEMO_GUI=0
```

Для проверенного ручного управления используйте профиль Town01 из [отчёта](doc/LIVE_LEADER_FOLLOWUP_RESULTS.md) и `make operator DEMO_SCENARIO=town01 DEMO_GUI=0`,
затем во втором терминале — `make gcs`. Остановка из другого терминала: `make stop`.
Customer-сцена, источники помех, карты и режимы исследования описаны в
[руководстве оператора](doc/USER_GUIDE.md).

## Результаты

Каждому запуску задавайте отдельный `BAS_NATIVE_FIVE_RUN_ID`. Каталог результатов:

```text
runs/native-radio-realtime/<RUN_ID>/
runs/native-radio-lockstep/<RUN_ID>/
```

Внутри находятся `report.md`, метрики в `metrics/`, журналы в `logs/`, PCAP,
графики и кадры Gazebo. Точный набор зависит от режима запуска.
Проверяйте код завершения, итоговый отчёт и ограничения измерений: завершённый
полёт сам по себе не означает соблюдения порогов задержки.

Полные трассы, сцены, образы Docker и зависимости хранятся вне Git. Старые RC1
измерения относятся к прежнему стенду; их нельзя переносить на новую машину
без повторного запуска. Аппаратная проверка внешнего контроллера остаётся
отдельным этапом.

## Документация

| Документ | Содержание |
| --- | --- |
| [Текущий статус](network/STATUS.md) | Выполненные проверки, известные дефекты и ограничения |
| [Настройка рабочего места](doc/LOCAL_DEVELOPMENT.md) | Windows, WSL, Linux, окружения и перенос |
| [Руководство оператора](doc/USER_GUIDE.md) | Сценарии, НПУ, карты, источники и результаты |
| [Архитектура](doc/PRODUCT_ARCHITECTURE.md) | Владение состоянием, радиопуть и ограничения моделей |
| [Окружение и ресурсы сцен](doc/ENVIRONMENT_AND_ASSETS.md) | Зависимости, assets и пути |
| [Требования](doc/PRODUCT_REQUIREMENTS.md) | Назначение и функциональные требования стенда |
| [Матрица проверок](network/TEST_MATRIX.md) | Минимальные проверки для изменяемого компонента |
| [Область поставки RC1](doc/DELIVERY_SCOPE.md) | Историческая поставка и границы её верификации |

## Работа с изменениями

Создавайте короткую ветку от `main` под конкретную задачу, добавляйте проверку
исправляемого поведения и отправляйте PR. После слияния рабочая ветка удаляется.
Теги `test-baseline-*` сохраняйте неизменными; для нового эталона создавайте новый
тег и записывайте реально выполненные проверки в `network/STATUS.md`.
Сообщения об ошибках: [GitHub Issues](https://github.com/AFETZ/bas_v2/issues) — укажите
коммит/тег, платформу, команду запуска и относящийся к ошибке фрагмент журнала.

## Лицензия

Исходный код распространяется под [GNU GPL v3](LICENSE). Внешние сцены и зависимости
имеют собственные лицензии и не включаются в поставку автоматически.
