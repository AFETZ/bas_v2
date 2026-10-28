# Воспроизведение городской серии

Новая серия отделена от прежних main30 (`8e16ec2`), sensitivity18 (`e6453c8`)
и candidate8. Замороженный runtime: `4d96d196e29792e8fd508f97994b78251efaee12`.
Документирование и рендер после опытов не меняют эту измеренную версию.

## Среда и модель

- Интеграция: `/home/bas/bas_v2-paper-live`; ветка `paper/live-leader-20261026`.
- Image: `sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5`.
- ns-3.48 fork: `d2add90b452d600cfb4859baed8e9ea633519447`; актуальный ELF hash
  записан в `freeze.native_binary.sha256` и сверяется с environment каждого опыта.
- ArduCopter4.8.0-dev: `3c073f9a09590307e99f49f960dd0f4dac7fc5bb`;
  Gazebo8.14, DART6.13.2. Версии runtime image сохранены прежним аудитом;
  эти строки не являются новой независимой сборкой зависимостей.
- Хост: Ubuntu24.04, Ryzen9 9950X, 16cores/32threads, около60GiB RAM;
  контейнер Ubuntu22.04. GPU для измеренного радиоканала не используется.
- `geometry_logdistance`: upstream BuildingsChannelConditionModel выбирает
  LOS/NLOS; thin adapter выбирает upstream LogDistance. Потери применяются один раз.
  Native Wi-Fi802.11n ad-hoc, прямые peers, HtMcs0,2.412GHz/channel1/20MHz,
  10dBm/isotropic0dBi; native PHY/MAC/PER. Sionna RT не инициализируется.
- d0=1m, L0=40.09532929124565dB, n_LOS=2; n_NLOS=3, sensitivity2.7/3.3.
  Это некалиброванные гипотезы. Модель не рассчитывает лучи, дифракцию и стены.
- Общая для Gazebo/ns-3 геометрия: A[40..70,20..65,0..30],
  B[95..125,20..65,0..25]; НПУ[0,0,2]. Это новая идеализированная сцена, не Town01.
  Ground surfaceZ=0, fixed model-origin referenceZ=.195m. Высота worldENU не AGL.

## Запуск и остановка

На подготовленном хосте для отдельного нового диагностического опыта:

```bash
cd /home/bas/bas_v2-paper-live
python3 scripts/product/run_urban_case.py --run-id NEW_UNIQUE_ID \
  --config network/config/urban_north_inspection.json --policy failover \
  --duration 240 --rng-run 12 --pilot --parameters
```

Контроллер сам держит `/tmp/bas-paper-runtime.lock` до teardown и публикации.
Не оборачивать его повторным flock. Не использовать существующий run ID.
`--build` нужен только до freeze, если qualified ELF ещё не подготовлен.
Обычная остановка — LAND, наблюдённые ground/disarm, затем остановка своего runtime:

```bash
docker stop --timeout 120 bas-v2-native-radio-five-uav
```

Остановка контейнера сама по себе не доказывает посадку. В городской fixed-политике
полное измерительное окно может завершиться без доступного пути LAND; это failed
teardown, который не объявляется штатным выполнением миссии.

Для уже замороженной серии использовалось:

```bash
python3 scripts/product/run_frozen_urban_study.py \
  --freeze runs/urban-ns3-20261028/freeze.json
```

Эта команда сохраняет существующие попытки, а не повторяет их. Настоящий новый
повтор требует нового study ID и freeze, прежней версии исходников и новых каталогов.
Исходный freeze хранит40main+12sensitivity+4boundary, порядок, ns-3seed1/run1..10,
RNG/order seeds, native ELF hash, configs и исходное окружение. RngRun12 — пилоты.
240model s — окно после готовности пяти; full parameter snapshots с5workers — вне
окна, post-window LAND timeout60s. Все пороги объявлены до начала серии.

## Расчёт по сохранённым данным

```bash
python3 scripts/product/analyze_urban_experiment.py run RAW_RUN --output NEW_EXPORT
python3 scripts/product/analyze_urban_study.py \
  --freeze runs/urban-ns3-20261028/freeze.json \
  --analysis-root runs/urban-ns3-20261028/analysis \
  --controllers-root runs/urban-ns3-20261028/controllers \
  --runs-root runs/native-radio-realtime \
  --output NEW_AGGREGATE
```

Полные raw: `runs/native-radio-realtime/urban20261028-*`; controls, execution history,
freeze и per-run exports: `runs/urban-ns3-20261028/`. Пилоты и failed development
остались в `runs/urban-ns3-dev/` и отдельных raw ID. Старые raw не изменялись.
Не объединять административное цензурирование, crashed window и отсутствие trigger.
Физические позы, radio model time, SITL time_boot_ms и host-monotonic — разные часы.

## Независимый clone

CLI измеренной версии сначала читает исторический base freeze даже при наличии
нового `--freeze`; это требование входных данных, а не требование повторить main30.
Подлинные байтовые копии обоих freeze и плана сохранены в `reproduction/` вместе
с исходными путями/хэшами. [Восстановление отсутствующего входа](reproduction/README.md).

В Git включаются компактные таблицы, freeze и рисунки; full PCAP/raw, runtime image,
ROS/native build остаются локально. Image ID без registry/archive URI не позволяет
скачать образ. Для независимого повтора нужны image archive либо полный build bundle,
модели Iris/плагины, pinned native fork и архив исходных журналов. Идеализированные
корпуса генерируются из JSON; лицензированные Town01 meshes новому сценарию не нужны.
Sionna-пакеты остаются установленными в старом образе/fork: standalone stock-ns-3
сборка без этих build-зависимостей не проверена. Параметры random всех процессов
SITL/планировщика не объявляются одинаковыми только на основании ns-3 seed.
