# Архитектура RC1

Основной путь: `MAVProxy/сценарий → BSF1 → GCS TAP → native Wi-Fi AP/STA →
UAV TAP → BSF1 → настоящий UART ArduPilot`. Обратные ACK и телеметрия идут
тем же путём. У каждого SITL свои control/payload UART, SYSID и ROS odometry.
Payload UART является вторым MAVLink UART, не видео.

Gazebo моделирует движение и коллизии. ArduPilot исполняет команды и автономную
миссию. Position tracker атомарно передаёт настоящую одометрию в MobilityModel.
`SionnaRtSpectrumPropagationLossModel` рассчитывает received PSD. Стандартные
SpectrumWifiPhy, NistErrorRateModel, Wi-Fi association, MAC/ARQ, QoS queues и
конкуренция за эфир ns-3 определяют приём. JSON используется для входов/журналов,
а custom JSON/PER provider не участвует в этом тракте.

## Общая радиосеть

Одна BSS: НПУ — AP, БАС — STA. Межбортовой P2P проходит реальные штатные BSS hops.
P2MP создаёт один application multicast root; каждый получатель фиксирует свою
уникальную доставку. Это не пять application unicast sends; multicast не имеет
выдуманного MAC ACK. Одновременный uplink использует тот же эфир.

IEEE 802.11n reference: 2412 MHz, channel 1, 20 MHz, 10 dBm, HtMcs0,
control/basic ErpOfdmRate6Mbps, один изотропный элемент. Калибровки изделия нет.
Конфигурация: `network/config/native_wifi_80211n_spectrum_product.yaml`.

## Источники и альтернативное распространение

`native-spectrum-sources.h` только связывает WaveformGenerator/антенну/позицию
с public phased-array API. Перед каждой передачей выбирается ровно одна native
propagation model; потери двух моделей не складываются. Параметры источника:
position, orientation, frequency/bandwidth, rectangular PSD, feed gain, power,
iso/tr38901 antenna, start/stop, period/duty, continuous/pulsed/sweep.
Частотные сегменты имеют отдельные модели нужной частоты. События излучения
планирует ns-3; TTL геометрии не откладывает включение или перестройку.

По умолчанию всегда Sionna, без fallback при ошибке. Явный
`BAS_NATIVE_PROPAGATION_PROFILE=friis` выбирает штатный FriisSpectrumPropagationLossModel.
`hybrid` применяет Friis только когда оба конца имеют x>2000 m и z≥200 m,
а расстояние ≥500 m; остальные links использует Sionna. Это инженерное правило
для открытой высокой трассы вне Town01, не автоматический детектор LOS.
Для Friis разрешены только изотропные элементы. Экранирование холмами, городской
клаттер, направленные антенны и наземные трассы этим простым профилем не подтверждены.
Возле границы возможен скачок между моделями. В статистике выбор записан явно.

## Время и отказ

Gazebo RTF=1; ns-3 RealtimeSimulatorImpl работает по wall clock. Модельное время
ns-3, ROS/Gazebo и host monotonic хранятся раздельно. RTT — разность host monotonic.
Синхронный RT solve может задержать обработку; p95 не ограничивает редкий максимум.
Кэш 20 s/10 m с jitter .5 досрочно распределяет обновления пар; это аппроксимация,
с измеренной ошибкой/задержкой возле препятствия. Частые координаты не означают
частый пересчёт канала. Отчёт вычисляет возраст из native generated time.

Устаревание odometry более 1.5 s останавливает native процесс. Python/Sionna
exception не включает упрощённую модель. Heartbeat native scheduler обновляется
каждые 0.5 модельной секунды; внешний gateway прекращает передачу при его устаревании.
BSF1 host timestamp и deadline исключают воспроизведение старых команд после stall.
Замедление Gazebo не объявляется пригодным для аппаратного HIL.

## Внешний контроллер и НПУ

`external_endpoint.py` использует тот же BSF1. Serial 8N1, UDP и TCP client имеют
ограниченные очереди/реассемблирование, deadline, reconnect и watchdog.
Radio socket создаётся в ams-uav1, внешний Ethernet socket — в host namespace;
дополнительный сетевой маршрут между НПУ и FC не создаётся. Serial подключается
через конфигурацию. Это эмулятор приёмопередатчика, не flight-HIL с датчиками.

`native_operator_bridge.py` раскрывает пять local UDP MAVLink links для готового
MAVProxy внутри ams-gcs. Он передаёт исходные байты через BSF1, без synthetic ACK.
Сценарий и оператор выбираются существующим runner; новый orchestrator не введён.

## Геометрия и наблюдения

Канонические Town01 PLY не изменяются. Gazebo visual OBJ содержит те же вершины и
треугольники с вычисленными normals/materials. Исходные Town01 collision proxies
являются упрощёнными bounding boxes; это не точный interior mesh зданий.
Customer добавляет constrained-triangulated поле 10×10 km, высотно непрерывное
сопряжение с внешним контуром исходных поверхностей, холмы и здание с заданными
этажами. Новые visual/collision/Sionna meshes имеют единый PLY-источник.

MonitorSnifferRx даёт полезную мощность и combined noise/interference только
декодированных MPDU; SignalArrival даёт мощность сигнала до решения о приёме.
RxOk/RxError — decoder attempts; PhyRxEnd — нейтральное окончание сигнала.
Native queue traces наблюдают AC occupancy и время до удаления MPDU, включая
backoff/retry. Полный raw Wi-Fi capture — radiotap DLT127; Ethernet/TAP PCAP —
производное представление. Карты — offline native PSD predictions без live overhead.
Raw кадры получены с камер текущего Gazebo и связаны с odometry и временем.
