# Разработка на Windows / WSL и перенос на Linux

Исходники можно редактировать в Windows. Bash, PTY, ROS 2, Gazebo, ArduPilot и
ns-3 запускаются в Linux. Лёгкое окружение не заменяет интегрированный стенд.
Python — 3.10; версии библиотек для разработки закреплены отдельно от runtime.

## Windows

Из корня checkout, при установленном [uv](https://docs.astral.sh/uv/getting-started/installation/):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_dev.ps1
.\.venv\Scripts\python.exe -m pytest -q network/tests/test_serial_transport.py network/tests/test_native_wifi_sionna_reference.py
wsl -d Ubuntu-22.04 --cd /mnt/c/bas
```

В редакторе выберите `.venv/Scripts/python.exe`. Активировать окружение необязательно.
Последняя команда предполагает checkout в `C:\bas`; для другого пути замените `/mnt/c/bas`.
Windows `.venv` и Linux `.external/dev-venv` используются раздельно.

Для самостоятельных расчётов Sionna RT на GPU в Windows:

```powershell
uv venv --python 3.10 .external/sionna-windows
uv pip sync --python .external/sionna-windows/Scripts/python.exe .devcontainer/requirements-sionna-dev.lock
.\.external\sionna-windows\Scripts\python.exe
```

Это отдельное окружение компонента Sionna, не полный тракт ns-3/Gazebo.
В WSL проверяйте OptiX отдельно: доступ к CUDA не гарантирует работу ray tracing.
На данном ПК Windows CUDA/OptiX выполнил расчёт `rock_demo`; WSL OptiX не загрузился.
[Пояснение NVIDIA](https://forums.developer.nvidia.com/t/running-optix-on-wsl-2026-version/382414).

## Ubuntu 22.04 / WSL2

На новой машине установите host-инструменты:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake ninja-build pkg-config git curl \
  python3.10-venv python3-dev python3-pip python3-yaml python3-tomli \
  iproute2 socat tcpdump
bash scripts/setup_dev.sh
source .external/dev-venv/bin/activate
python -m pytest -q network/tests/test_external_endpoint.py
make test-changed
```

Docker Desktop должен иметь включённую интеграцию с этой Ubuntu; на Linux нужен
Docker Engine с NVIDIA Container Toolkit. Проверка доступа:

```bash
docker version
docker run --rm --gpus all ubuntu:22.04 nvidia-smi
```

Проверка `nvidia-smi` подтверждает доступ к GPU. Sionna/OptiX проверяется отдельно
реальным расчётом; эта команда не подтверждает готовность радиомодели.

## Полный runtime

Используйте существующий bootstrap. Встроенная сцена `rock_demo` не требует Town01:
команды `make demo-*` выполняются на Linux/WSL-хосте, где доступен Docker CLI.

```bash
make demo-preflight DEMO_SCENARIO=rock_demo DEMO_GUI=0 DEMO_BOOTSTRAP=1
make demo-rugged DEMO_GUI=0
make stop
```

Первая команда собирает Docker image, ROS workspace, устанавливает pinned ns-3
и Python target. Native C++ target собирается runner при первом запуске.
Preflight также исполняет пересечение луча CUDA/OptiX; на этом WSL ожидается
`FAIL gpu:optix`, поэтому полный запуск выполняйте на совместимом Linux/NVIDIA ПК.
Для Dev Containers сначала подготовьте этот image; затем откройте checkout в
контейнере. Рабочий путь фиксирован: `/workspace/multiagent_simulation`.

Для компиляции C++ на этом ПК после bootstrap откройте Dev Container либо shell:

```bash
docker run --rm -it -v "$PWD:/workspace/multiagent_simulation" \
  -w /workspace/multiagent_simulation multiagent_simulation:latest bash
```

Внутри контейнера можно собирать ns-3 без запуска GPU-сценария:

```bash
cd .external/ns-3-sionna-native
git config --global --add safe.directory "$PWD"
cp ../../network/ns3/scratch/upstream-sionna-tap-spike.cc scratch/
cp ../../network/ns3/scratch/native-spectrum-sources.h scratch/
export PYTHONPATH="$PWD/.tooling-py310:$PWD/.python-deps-py310"
export PATH="$PWD/.tooling-py310/bin:$PATH"
test -f cmake-cache/CMakeCache.txt || ./ns3 configure --enable-examples --enable-tests --enable-python-bindings
./ns3 build upstream-sionna-tap-spike -j 4
```

Town01 требует исходный CAVISE bundle или assets с прежнего стенда:

```bash
export CAVISE_MAPS_DIR=/absolute/path/to/bundles
make demo-preflight DEMO_GUI=0 DEMO_BOOTSTRAP=1
make prepare-customer
```

Команды оператора и описание результатов: [USER_GUIDE](USER_GUIDE.md).
Состав runtime и сцен: [ENVIRONMENT_AND_ASSETS](ENVIRONMENT_AND_ASSETS.md).

## Перенос на мощный ПК

Клонируйте `main` и перенесите изменения исходников, включая новые setup-скрипты
и lock-файлы: включите их в свой коммит или скопируйте с рабочей копией. Ветка
`release/bas-v2-rc1`, упомянутая в старой инструкции поставки, может отсутствовать
на remote. Создайте окружения заново; `.venv`, `build`, `install` не копируйте.
Для ускорения можно перенести `docker save multiagent_simulation:latest` / `docker load`.
Town01 хранится отдельно в `.external/cavise_maps/Town01`; Gazebo derivatives и
customer-сцену подготовьте повторно после переноса. Native ns-3 лучше пересобрать.
Большие assets, зависимости и результаты остаются вне Git.

Новая сборка из Dockerfile не обязана совпадать по image ID с историческим RC1:
исходные revisions и Python pins закреплены, apt-репозитории меняются.
OpenCV закреплён на 4.11.0.86, quantized-mesh-tile на 0.6.1: новые версии требуют
NumPy 2 и конфликтуют с NumPy 1.26.4 этого ROS runtime. Остальные существовавшие
версии runtime lock сохранены; добавлена зависимость Shapely для mesh tile.
Точный старый image можно восстановить из `runtime-image.tar` прежней поставки.
Замеры real-time повторяются на целевой машине.

При разработке используйте upstream API и существующие runners. Адаптеры меняют
формат, координаты и время; решения PHY/MAC, распространение и полёт остаются
за ns-3, Sionna, Gazebo и ArduPilot. Новые orchestration-сервисы не нужны.
