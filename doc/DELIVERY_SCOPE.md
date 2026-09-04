# BAS v2 RC1: объём поставки

Источник **ТЗ** — `PRODUCT_REQUIREMENTS.md`; **пользователь** — задание на RC1
от 2026-09-04; **инженерный** — измеряемые критерии конкретного запуска.
`failed` до нового испытания означает отсутствие подтверждения текущей поставки,
а не отрицание исторического результата. Полные данные находятся в ignored `runs/`.

| Требование | Источник | Реализация | Проверка | Статус | Ограничение |
| --- | --- | --- | --- | --- | --- |
| R1: 5 БАС + НПУ, SITL/Gazebo, полёт и остановка | ТЗ; пользователь | Native five-UAV runner, 5 SYSID, ROS odometry | Новый полный прогон и повтор | failed | Старый v1 не доказывает новый код |
| R2: 10 UART, P2P/P2MP, реальные байты | ТЗ; пользователь | BSF1/TAP, одна multicast root, native Wi-Fi BSS | 10×5 one-shot, UART/ACK correlation, P2P/P2MP, stop proof | failed | Multicast без выдуманного ACK; payload UART не видео |
| R3: Sionna RT + штатные ns-3 PHY/MAC | Пользователь уточняет границы ответственности ТЗ | ns-3.48 SpectrumWifiPhy/NistErrorRateModel; Sionna PSD propagation | Build, фактические channel/rates, native traces | failed | 802.11n reference, не калиброванный модем и не LoRa/NR |
| R4: независимые источники помех | ТЗ; пользователь | Требуется WaveformGenerator в том же Spectrum тракте | baseline/on/off, multiple, orientation, overlap, pulse/sweep | failed | Старые JSON/PER варианты не засчитываются |
| R5: метрики, карты, реальные кадры | ТЗ; пользователь | Native traces + MonitorSnifferRx; Gazebo camera topics | Метрики с единицами/происхождением, просмотр raw кадров и heatmaps | failed | SignalNoiseDbm — выборка decoded MPDU; не total RSSI при outage |
| R6: serial/COM и Ethernet внешнего FC | ТЗ; пользователь | Существующий serial transport/TAP; требуется внешний gateway | Software reconnect/queue/watchdog + физический безопасный запрос | blocked_external | /dev/serial/by-id, ttyACM*, ttyUSB* отсутствуют; PTY не hardware HitL |
| R7: время, кэш, масштабирование, hybrid | ТЗ; пользователь | RealtimeSimulatorImpl, live-pose cache, runtime monitor | 1/5 БАС, движение, update profiles, крупный radio-only случай | failed | 20 s/10 m требует оценки ошибки; p95 не ограничивает максимум |
| R8: 10×10 km, рельеф ≤200 m, застройка | ТЗ; пользователь разрешил внешнее поле/холмы | Town01 без масштабирования + отдельное расширение | Размеры, швы, mesh/collision, customer run | failed | В ТЗ есть здания до 15 этажей; высота mesh не доказывает этажность |
| R9: операторский запуск и управление | ТЗ; пользователь | Makefile/native runner, готовый НПУ через modeled path | Запуск, выбор БАС, команды, disconnect/cleanup | failed | GUI/GCS требуется подтвердить; direct SITL запрещён |
| R10: воспроизводимая поставка | ТЗ; пользователь | Pinned dependencies/patches, configs, CLI, документация | Чистый повтор, пакет результатов, опубликованная ветка/PR | failed | Production release не допускается при незакрытых обязательствах |

`software_release_status=failed`; `full_tz_status=failed`;
`hardware_validation_status=blocked_external`. Итоговые значения обновляются
по фактическим испытаниям. Пороги latency/RTF/PDR из сценария — инженерные,
не дополнительные требования заказчика. Sionna определяет распространение;
приём, интерференция и арбитраж определяются native ns-3.
