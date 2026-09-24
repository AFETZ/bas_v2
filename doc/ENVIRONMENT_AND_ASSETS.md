# Окружение и внешние assets

| Компонент | Закреплённая основа |
| --- | --- |
| Runtime image | `sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5` |
| ns-3.48 | `d2add90b452d600cfb4859baed8e9ea633519447` |
| ArduPilot | `3c073f9a09590307e99f49f960dd0f4dac7fc5bb`, SITL сообщает 4.8.0-dev |
| Sionna / Sionna RT | 1.2.0 / 1.2.0 |
| Mitsuba / Dr.Jit | 3.7.1 / 1.2.0 |
| NumPy / SciPy | 1.26.4 / 1.15.3 |
| pybind11 / cppyy / CMake | 2.11.1 / 3.5.0 / 3.31.6 |
| pymavlink / MAVProxy | 2.4.49 / 1.8.74 |
| Geometry preprocessing | isolated Shapely 2.1.1 / GEOS 3.13.1 |
| Измеренный стенд | Ryzen 9 9950X, 32 logical CPU; RTX 5070 Ti 16 GB; driver 595.84 |

ROS/Gazebo/ArduPilot сборка закреплена в `.devcontainer/Dockerfile`,
`ardupilot_gz_exact.repos`, `ardupilot_ros2_exact.repos`, `requirements-radio.lock`.
Native Python 3.10 target создаёт `native_demo_preflight.sh --bootstrap`.
Существующие зависимости не обновлялись; Shapely добавлен отдельно только для
построения границы/триангуляции customer-геометрии.

Применяются ровно три project compatibility/cache patches из `network/ns3/patches/`:
`mr2608-spike-compatibility.patch`, `mr2608-realtime-scene-cache.patch`,
`mr2608-spectrumwifi-phased-array-adapter.patch`. Они проверяются на фактическом
checkout. Старый ns-3.40 scheduler patch не входит в native runtime.
Исходники thin adapters копируются в native scratch перед целевой сборкой.

## Assets

Исходный CAVISE bundle предоставлен пользователем:
[папка источника](https://drive.google.com/drive/folders/1HyksBPnwaKs1Ks1g4OKbnrQuidzO3m41).
Локальный `Town01/README.md` описывает CARLA 0.9.16 Editor LOD0 → FBX → Blender 3.6
→ Sionna PLY, с уже запечённым SUMO offset; dynamic transform identity.
Копия Town01, включая Blender master, использована локально; CARLA повторно не экспортировалась.

Исходные terrain vertices: z=-60.8818817..58.6259308 m. Vegetation bounds до
220.387 m не являются высотой рельефа. Customer field: x/y=-5000..5000 m,
external z=-35.2881..127.9090 m; совокупный диапазон рельефа ≈188.791 m.
6 849 внешних треугольников, 4 458 вершин; площадь внешней сетки 99 533 822.2375 m².
1 867 точек сопряжения имеют нулевую ошибку до float32 export; геометрия export
хранит float32 точность. Это синтетический сценарный рельеф, не геодезия.
Новый tower: 15 этажей по 3 m, 16 плит, крыша 45.25 m над datum 9.3592 m;
положение [1200,-800] m. Town01 storey metadata отсутствует и не домысливается.

CAVISE README не содержит отдельного разрешения на перераспределение assets.
Пакет предназначен для проверки в имеющемся пользовательском workspace; большие
assets и runtime не публикуются в Git/PR. Для передачи этих assets третьей стороне
нужно подтвердить условия исходного bundle. Upstream исходники/пакеты сохраняют
свои LICENSE/COPYING; эта поставка не заменяет их лицензии и не назначает новую
лицензию исходным CARLA/CAVISE материалам. Синтетическое дополнение воспроизводится
из проектного скрипта; материалы ITU concrete/brick — инженерный reference.
