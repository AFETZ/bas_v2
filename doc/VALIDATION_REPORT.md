# BAS v2 RC1: испытания и ограничения

Дата: 2026-09-05. Стенд и версии — ENVIRONMENT_AND_ASSETS.md.
Данные: runs/ в workspace и artifacts/runs/ в локальном delivery package.
Главные запуски: native-radio-realtime/rc1-customer-final-01 и ...-02.
Runtime HEAD: 547a536 и 2fb157b; offline отчёты дополнены на b084b9c.
Исходные report/summary сохранены в original_report/, raw/PCAP/events не менялись.
В итоговой упаковке нулевые placeholder координаты source switch заменены null;
реальное configured положение хранится отдельно в logs/native_sources.json.

| Измерение | final-01 | final-02 (чистый повтор) |
| --- | --- | --- |
| Functional checks | 20/20 | 20/20 |
| Пять flight/LAND/auto-disarm; UART | 5; 10 | 5; 10 |
| P2P unique deliveries | 100/100 | 100/100 |
| P2MP | 20 roots, 100 receiver deliveries | 20 roots, 100 receiver deliveries |
| Shared uplink | 20/20 на БАС, Jain 1 | 20/20 на БАС, Jain 1 |
| Stop-based no-bypass | 10.5 s, все пять | 10.5 s, все пять |
| Steady ns-3 lag p95 / max, ms | 0.336842 / 58.01054 | 16.076663 / 108.432918 |
| Gazebo RTF mean / p5 | 0.996636 / 0.997651 | 0.995554 / 0.997580 |
| Steady RT solve p95 / max, ms | 38.949781 / 100.792662 | 40.169727 / 60.529455 |
| Cold scene / first channel incl. scene, ms | 425.556 / 912.173 | 415.267 / 905.147 |
| Actual channel age p95 / max, s | 13.928244 / 19.879994 | 13.869567 / 19.879994 |
| Native Wi-Fi on-air interval union | 1.89813% | 1.89554% |

RTF cold minima ≈0.0006 и cold ns-3 backlog до 4.2 s не включаются в steady claim.
Lag опрашивается через .5 модельной секунды: sampled max не ограничивает каждый solve.
Эти замеры не дают hard-RT гарантии или аппаратного deadline.
Повтор выполнялся после make stop; параллельных GPU benchmarks не было.

Stationary-01/02: 50/50 parallel safe one-shot REQUEST_MESSAGE и 115/115
first-attempt операций с sequential diagnostics. Во втором aggregate RTT p95
14.010543 ms, max 16.756299 ms; exact bytes на четырёх UART/GCS границах.
Retry ACK не имеет исходного attempt ID: неопределённый attempt RTT остаётся null.

rc1-source-campaign-01: 8 native cases. Baseline/continuous/pulsed/sweep/back/nonoverlap
дали 200/200; front и multiple — 113/200 с 0 доставок в active window.
Front/back foreign received median: −54.4626/−78.9447 dBm. Это реальные native outages.
В final-01 mean decoded noise/interference baseline/on/recovery:
−93.6771/−89.2382/−93.7927 dBm; decoder-attempt PER 0/0.000688705/0.
В final-02 active decoder-attempt PER 0.003231018. Это не application loss.

rc1-native-heatmaps и rc1-pulse-map-cli: 8×8 grid, z=2 m, baseline/jammer/delta
RSSI/SINR/J/S/conditional availability. Pulse map — мгновенный срез t=104 s,
не duty-average. rc1-map-comparison: x=20/40/80,y=0,z=2; максимум |map−runtime|
0.000722 dB полезной мощности и 0.009790 dB помехи. No-path cells сохраняются.
SINR≥10 dB — инженерная условная доступность, не измеренная карта PDR.

rc1-cache-study-isolated: 56 точек, x=80,z=17,y=0..110, 2 m каждые .5 s.
20 s/10 m против .1 s/.1 m: mean/max power error 0.127222/0.381655 dB там,
где оба имеют path; 5 path-count и 4 no-path mismatches. Исчезновение задержано
на 1 s, восстановление длительностью 1 s полностью пропущено. Возраст до 1.5 s.
Средняя wall цена вызова без первого: 20.10/77.59/73.76 ms для 20s/10m,
1s/1m и .1s/.1m. Это offline solver/cache cost, не Gazebo RTF.
Первый contended cache run сохранён; его cost не основной benchmark.

rc1-native-reference-matrix: radio-only 1/5 stationary/moving доставили
100/100 и 500/500; 16 STA — 1600/1600, 11.63 wall s на 8 sim s, не 16 SITL.
Sionna/Friis при 500/1000/2000 m: −84.3808/−84.4573, −90.4655/−90.4779,
−95.7354/−96.4985 dBm; все 100 offered/0 delivered при неизменных 10 dBm.
Hybrid 1000 m совпал с Friis. Простая модель оправдана только в явно заданной
открытой высокой области; urban/terrain blocking ею не подтверждён.
Erratum в summary поясняет старую fixed propagation label исходных result JSON.

rc1-one-uav-operator-flight: один SITL, MAVProxy GUIDED/ARM/TAKEOFF/LAND,
max z=10.3705 m, ground z=.3405 m; lag p95=.494795 ms, mean RTF=.997513.
Клиент завершился до сообщения auto-disarm; однобортовой auto-disarm не заявлен.
rc1-customer-operator-02: пять SYSID, выбор vehicle2 и настоящий accepted ACK.
rc1-external-serial-sitl: внешний gateway до настоящего UART SITL, accepted ACK,
31 508/460 byte; 32 queue drops при cold/pre-readiness, 0 deadline drops.
Physical FC отсутствует. SIGSTOP native на 3 s дал heartbeat age 3.01073 s;
возобновление heartbeat наблюдалось. Tests проверили stale/drop/reconnect.

Focused tests: 40 passed; после offline energy/report изменения — 21 passed.
Логи в rc1-delivery-checks. Native C++ builds и map CLI завершились.
Failed diagnostics сохранены: white Gazebo normals (исправлено), cache map key/array
collision (исправлено), первый jammer flight с lag p95=71.84 ms (RT gate failed;
в это время был concurrent GPU diagnostic). Они не переименованы в PASS.

Customer: 10 000×10 000 m, relief 188.791 m, 6 849 внешних triangles;
1 867 seam points, нулевая высотная ошибка до float32 export. Добавлены 15 заданных
этажей, 16 плит, крыша 45.25 m над datum. Town01 не масштабирована и не заменена.
Просмотрены raw кадры взлёта/удержания, препятствия, помехи, восстановления и посадки;
overview даёт мелкие силуэты, focus и аннотации показывают соответствующие БАС.

Software RC пригоден для повторной стендовой проверки в указанном envelope.
R6/full TZ/hardware остаются blocked_external: нужны FC, serial/COM или Ethernet,
безопасный bench и отдельное разрешение на аппаратные действия.
Условия передачи исходных CAVISE assets третьей стороне пока не подтверждены.

Локальный пакет: /home/bas/bas_v2-delivery/rc1-2026-09-05-final.
Image/dependencies/scenes/source archives прочитаны, source bundle проверен.
В отдельном checkout исходники и зависимости восстановлены из пакета, ROS workspace
собран заново; bootstrap preflight: 0 failures, 0 warnings. Docker image не пересобирался.
Первичная упаковка выявила root-owned OBJ 0600; ownership генератора исправлен,
повторная подготовка и упаковка прошли. Failed packaging log сохранён.
