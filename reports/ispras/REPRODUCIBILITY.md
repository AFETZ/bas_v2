# Воспроизводимость: аудит 28.10.2026

Проверялась сохранённая рукопись `paper/build/manuscript_anonymous.pdf`, разделы 3–6; PDF и исходный текст не редактировались. Исходная версия аудита: `bb026a052a2474df29bee3603cc98af27592e207`. Новые артефакты — отдельная когорта, прежние raw неизменны.

| Компонент | Зафиксировано | Источник |
|---|---|---|
| Основная серия30 | `8e16ec223e895c11891e6e7d7c39dff8cee39cac`, seed1/run1–5 | `runs/paper-live-20261026/campaign/freeze.json` и plan |
| Чувствительность18 | `e6453c8e2a1ed4f6e4f9b16c224104e62e2df5d1`, seed1/run1–3 | `paper/evidence/followup/freeze.json` |
| ns-3 | 3.48, native fork `d2add90b452d600cfb4859baed8e9ea633519447` | каждый raw/environment.txt |
| Native binary | SHA256 `3d1c0e64ee3342acdb1c3629572fa89641971a0faee265f2a00a3ee3c89853a8` для30/18 | оба freeze |
| Runtime image | `sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5` | freeze, доступен локально |
| Gazebo / DART | gz-sim8 8.14.0-1~jammy / 6.13.2+ds1-1~osrf2~jammy | текущий pinned image, dpkg-query |
| ArduPilot | source `3c073f9a09590307e99f49f960dd0f4dac7fc5bb`, source header ArduCopter V4.8.0-dev | `runtime-version-probe.txt` |
| SITL ELF (все5 /proc/exe) | SHA256 `80081862d5e9be8064abb17b66eff4ac7c93de9dbd1835eb920723067ac24772` | текущий pinned image, actual-sitl-executables.txt; не отдельный исторический хеш |
| ОС | host Ubuntu24.04.5/kernel7.0.0-38-generic; container Ubuntu22.04.5 | текущий uname/os-release; исторический kernel отдельно не восстановлен |
| Хост | AMD Ryzen9 9950X,16cores/32threads,60GiB RAM | lscpu/free, historical host-lscpu.json |

`git describe` ArduPilot даёт `ArduPilot-4.6.0-beta1-7205-g3c073f9a09`, что не равно версии source header. Поэтому нельзя называть бинарник «стабильной4.6» только по ближайшему Git-тегу. В пяти реально работающих новых SITL `/proc/PID/exe` совпал с этим бинарником; встроенные строки `ArduCopter V4.8.0-dev (3c073f9a)` сохранены в actual-sitl-version-strings.txt. Полная привязка исторического SITL ELF к исходному dirty-tree/build manifest отсутствует; фиксированный образ помогает, но исходная сборка требует документирования.

Сеть: native Wi-Fi802.11n ad-hoc, SpectrumWifiPhy,2.412GHz/channel1/20MHz,HtMcs0,0.01W; aggregation/BAR отключены. Friis + явная MatrixPropagationLossModel 200dB для selective/totalGCS; геометрического радио-затенения нет. НПУ [5,0,2], UAV1…5 начальные [20,35,50,65,80] по x, y0,z2, реальные посадочные z≈0.3405. Полный YAML: `network/config/scenario_5uav_town01_native_product.yaml`; конечные значения — raw/configuration/{initial,final}/inputs, ns3 ConfigStore/routes/native stats. Координаты высоты — worldENU/relative-alt, не AGL.

Для парности фиксированы ns-3 RngSeed/RngRun. Отдельный фиксированный seed всех stochastic процессов SITL/планировщика не восстановлен; идентичные ns-3 seeds не означают идентичные физические траектории/host scheduling. В полных параметрах отдельное SIM_*SEED/SIM_*RAND поле не найдено.

## Что доступно и чего нет в свежем clone

`reproducibility_inventory.json` фиксирует доступность на текущем хосте. Повторный запуск проверен реальной новой инженерной пробой, не только импортом Python. Namespace/TAP/native image и Town01 assets доступны. Production DCC не останавливался. Все тяжёлые запуски последовательны под `/tmp/bas-paper-runtime.lock`.

В Git нет полных raw/PCAP (~10.2GB native файлов только main30), Town01 CAVISE meshes, mutable ns-3 build/tooling, ROS install и runtime Docker image. SHA256 без registry/archive URI не обеспечивает получение образа на другом хосте. Skills содержат абсолютные локальные ссылки. Для независимого повторения нужны распространяемый image/archive, лицензионно доступный bundle сцены, upstream source+patch/build manifest и архив исходных журналов с каталогом. CSV/графиков Git достаточно для сверки таблиц, но недостаточно для независимой проверки каждого UART/пакета.

Исторические raw: `runs/native-radio-realtime/paper-relay-*`; новая чувствительность `paper-sensitivity-*`. Новые пробы: `paper-candidate4-unavailable-*-20261028*`. Контроллер/команды/ошибки: `runs/ispras-audit-20261028/`. Не запускать прежние ID повторно.

## Команды

```bash
cd /home/bas/bas_v2-paper-live
# Новая инженерная пара: недоступен прямой ответ кандидата4, не модель зданий.
flock -n -E 75 /tmp/bas-paper-runtime.lock python3 -u reports/ispras/tools/run_candidate_probe.py --input-only
# Пересчёты используют сохранённые raw; команды алгоритма/метрик в соседних отчётах.
python3 paper/tools/check_manuscript.py --final
```

Контроллер имеет фиксированные IDs и отказывает, если raw существуют. Для независимого повтора на другом хосте выбрать свежий префикс и сохранить изменённую команду/manifest; для сверки уже выполненных запусков нужен анализ, а не повтор. Никакого force arming или ослабления сроков. Остановка своего runtime: LAND/disarm, затем `docker stop --timeout 120 bas-v2-native-radio-five-uav`.
