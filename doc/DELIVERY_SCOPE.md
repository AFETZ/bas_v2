# BAS v2 RC1: фактический объём поставки

Основа — PRODUCT_REQUIREMENTS.md и задание пользователя на RC1 от 2026-09-04.
Пороги RTF/latency/PDR в YAML — заранее заданные инженерные критерии запуска,
не новые требования заказчика. verified относится к указанной проверке и диапазону.
Полные данные находятся в ignored runs/ и локальном пакете.

| Требование | Реализация и проверка | Статус | Практическая граница |
| --- | --- | --- | --- |
| R1: пять БАС и НПУ | Пять ArduPilot SITL, Gazebo/ROS odometry, flight/LAND/auto-disarm, cleanup; customer-final-01/02 | verified | Автономная миссия БАС1, четыре удерживают позиции; не полёт внешнего FC |
| R2: десять UART, P2P/P2MP | Реальные BSF1/MAVLink bytes, exact UART/ACK correlation, 50/50 parallel one-shot; 100/100 P2P, 20 multicast roots × 5 доставок, shared uplink, stop proof | verified | Второй UART — MAVLink payload, не видео; multicast без выдуманного ACK |
| R3: native радио | ns-3.48 SpectrumWifiPhy/NistErrorRateModel, MAC/ARQ/queues и in-process Sionna; оба final run | verified | 2412 MHz/20 MHz/10 dBm/HtMcs0 iso reference; не калибровка модема |
| R4: источники помех | Native WaveformGenerator, pulse/sweep, antenna/orientation, multiple, non-overlap; 8 cases и on/off в двух полётах | verified | Sionna задаёт распространение; native ns-3 решает приём и конкуренцию |
| R5: наблюдаемость | Native decoded S/N, predecode power, derived RSSI/J/S, PER/PDR с разными знаменателями, latency/IPDV/goodput/queues/airtime, 12 heatmaps, 3 runtime map points, raw/annotated Gazebo и radiotap | verified | Границы измерений ниже; нет выдуманного PDR при outage |
| R6: внешний serial/COM/Ethernet FC | Serial, UDP/TCP, bounded queues/reassembly, reconnect/watchdog/deadlines; PTY/socket tests, настоящий SITL UART через внешний gateway/MAVProxy | blocked_external | Физического FC/USB serial нет. Windows COM bridge описан, аппаратно не испытан. R6 целиком не закрыт |
| R7: время и масштаб | 1/5 SITL, 1/5 moving/stationary и 16 radio-only STA; 3 cache profiles; native Friis/Sionna 500/1000/2000 m и explicit hybrid | verified | Измерение envelope, не hard-RT гарантия. Кэш пропускает короткое восстановление; дальние links дали outage |
| R8: customer-карта | Town01 + внешняя mesh 10×10 km, relief 188.791 m, явное 15-этажное дополнение; общие visual/collision/Sionna источники, два customer run | verified | Synthetic field/hills/tower, не геодезия. Исходные Town01 collision proxies приближённые; новая collision mesh точная |
| R9: оператор | Makefile/CLI, готовый MAVProxy через modeled path, выбор SYSID2/REQUEST_MESSAGE ACK, один SITL GUIDED/takeoff/LAND, cleanup | verified | Рабочий НПУ MAVProxy; QGroundControl не установлен и не заявляется испытанным |
| R10: поставка | Pinned image/dependencies/patches, исходники, конфиги, USER_GUIDE, результаты, чистый повтор, локальный package и feature PR | verified | Локальный RC для проверки; assets/runtime не опубликованы. Условия передачи CAVISE третьей стороне требуют подтверждения |

software_release_status=verified — программный RC в описанном Linux/NVIDIA
reference envelope, включая software часть внешнего gateway.
full_tz_status=blocked_external; hardware_validation_status=blocked_external.
Это не заявление о полном выполнении ТЗ или аппаратном flight-HIL.

R5: MonitorSnifferRx описывает decoded MPDU, SignalArrival — положительную
входную мощность до decode verdict. received_energy.csv суммирует реальные
native powers/durations с configured thermal floor; его энергетический SINR
не подменяет decoder-weighted SINR и не управляет PER. Timeline показывает
помеху даже без Wi-Fi path. Собственная TX leakage не моделируется.
Wi-Fi airtime — union native TX intervals, не CCA busy-state fraction.
PDR/goodput/IPDV относятся к уникальным application P2P/P2MP/shared deliveries;
ACK RTT — к реальным командным транзакциям. Встречные UART counters не являются
знаменателем telemetry PDR. Неатрибутируемые per-link/per-packet поля остаются
null с причиной; BLER — not_applicable_with_reason для выбранного PHY.

Результаты и неуспешные диагностики — VALIDATION_REPORT.md.
